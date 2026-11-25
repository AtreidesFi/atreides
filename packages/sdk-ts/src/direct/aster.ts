// Direct Aster Futures/Spot v3 attribution and exact Message signing (06 §1.5–1.6).
import type { FeeField, OrderType, TimeInForce } from '@atr/contract'
import { AppError, dec, type InstrumentClass, type Rate, rate } from '@atr/core'
import { signMessageRequest } from '@atr/venue-aster/sign'
import { type DirectContext, type DirectModule, type DirectStart, directFees } from './template.ts'

export type AsterProduct = 'futures' | 'spot'
export type AsterNetwork = 'mainnet' | 'testnet'
export type AsterParams = ReadonlyArray<readonly [string, string | bigint | boolean | number]>

export interface AsterSignedMessage {
  readonly msg: string
  readonly signature: `0x${string}`
  readonly chainId: number
  readonly body: string
}

export interface AsterBuilderRequest {
  readonly method: 'GET'
  readonly path: '/fapi/v3/builder' | '/api/v3/builder'
  readonly user: string
}

export interface AsterDirectOptions {
  readonly product: AsterProduct
  /** Use the caller's authorised API wallet to send this native GET and return its original JSON body. */
  readonly readBuilders: (request: AsterBuilderRequest) => Promise<string>
  readonly now?: () => number
}

const CLASS_CAP = rate.fromBps('2')
const STEP = rate.fromBps('0.1')
const ZERO = '0x0000000000000000000000000000000000000000'
const VENUE_CAP = { futures: rate.fromBps('10'), spot: rate.fromBps('100') } as const
const NO_FEE = { kind: 'none', reason: 'close_without_approval' } as const

function address(raw: unknown): string | null {
  return typeof raw === 'string' && /^0x[0-9a-fA-F]{40}$/.test(raw) && raw !== ZERO ? raw.toLowerCase() : null
}

function exactCap(raw: unknown): Rate | null {
  if (typeof raw !== 'string' || raw.length > 64) return null
  const direct = dec.tryParse(raw)
  if (direct !== null) return direct as Rate
  const match = /^([0-9]+)(?:\.([0-9]+))?[eE]([+-]?[0-9]{1,3})$/.exec(raw)
  if (match === null) return null
  const [, whole = '', fraction = '', exponent = ''] = match
  const decimals = fraction.length - Number(exponent)
  if (decimals < 0 || decimals > 18 || whole.length + fraction.length > 38) return null
  return dec.fromUnits(BigInt(whole + fraction), decimals, 'down') as Rate
}

function venueCap(body: string, user: string, builder: string, product: AsterProduct): Rate | null {
  if (body.length > 1_000_000) return null
  const rows: unknown = JSON.parse(body, (key: string, value: unknown, ctx?: { source?: string }) =>
    key === 'maxFeeRate' && typeof value === 'number' ? ctx?.source : value,
  )
  if (!Array.isArray(rows)) return null
  const matches = rows.filter((entry: unknown) => {
    if (entry === null || typeof entry !== 'object') return false
    const row = entry as Readonly<Record<string, unknown>>
    return address(row.userAddress) === user && address(row.builderAddress) === builder
  })
  if (matches.length !== 1) return null
  const value = exactCap((matches[0] as Readonly<Record<string, unknown>>).maxFeeRate)
  return value !== null && value > 0n && value <= VENUE_CAP[product] ? value : null
}

export function signAsterMessage(key: Uint8Array, params: AsterParams, network: AsterNetwork): AsterSignedMessage {
  return signMessageRequest(key, params, network)
}

export class AsterDirect implements DirectModule {
  readonly venue = 'aster'
  readonly #fees: ReturnType<typeof directFees>
  readonly #now: () => number
  #context: DirectContext | null = null
  #snapshot: DirectStart | null = null
  #pending: { readonly context: DirectContext; readonly result: Promise<DirectStart> } | null = null

  constructor(private readonly options: AsterDirectOptions) {
    this.#now = options.now ?? Date.now
    this.#fees = directFees({
      venue: 'aster',
      requiresApproval: true,
      capFor: () => CLASS_CAP,
      normalizeAccount: (value) => address(value) ?? '',
      now: this.#now,
      encode(row, builderRef) {
        const builder = address(builderRef)
        if (builder === null || row.rate <= 0n || row.rate % STEP !== 0n || dec.tryParse(row.encoded) !== row.rate) {
          throw new AppError('fee_code_missing')
        }
        return { kind: 'order_field', builder, rate: row.rate, encodedRate: dec.format(row.rate) }
      },
    })
  }

  start(context: DirectContext): Promise<DirectStart> {
    this.#context = null
    this.#snapshot = null
    const accountRef = address(context.accountRef)
    if (accountRef === null) return Promise.reject(new AppError('account_not_bound'))
    this.#context = { ...context, accountRef }
    return this.#load(this.#context, this.#fees.start(this.#context))
  }

  refresh(): Promise<DirectStart> {
    const context = this.#context
    if (context === null) return Promise.reject(new AppError('fee_code_missing'))
    if (this.#snapshot !== null && this.#snapshot.validUntil > this.#now()) return Promise.resolve(this.#snapshot)
    if (this.#pending?.context === context) return this.#pending.result
    return this.#load(context, this.#fees.refresh())
  }

  #load(context: DirectContext, schedule: Promise<DirectStart>): Promise<DirectStart> {
    const result = schedule
      .then(async (snapshot) => {
        const builder = address(snapshot.builderRef)
        let approvedCap: Rate | null = null
        if (builder !== null && snapshot.approvedCap !== null && snapshot.approvedCap > 0n) {
          try {
            const path = this.options.product === 'spot' ? '/api/v3/builder' : '/fapi/v3/builder'
            const body = await this.options.readBuilders({ method: 'GET', path, user: context.accountRef })
            const verified = venueCap(body, context.accountRef, builder, this.options.product)
            if (verified !== null) approvedCap = dec.min(verified, snapshot.approvedCap) as Rate
          } catch {
            // An unavailable or malformed native read cannot prove approval.
          }
        }
        const joined = Object.freeze({ ...snapshot, approvedCap })
        if (this.#context === context) this.#snapshot = joined
        return joined
      })
      .finally(() => {
        if (this.#pending?.context === context) this.#pending = null
      })
    this.#pending = { context, result }
    return result
  }

  feeFields(cls: InstrumentClass, type: OrderType, tif: TimeInForce, reduceOnly: boolean): FeeField {
    const correct = this.options.product === 'spot' ? cls === 'spot' : cls === 'perp' || cls === 'rwa_perp'
    if (!correct) throw new AppError('invalid_request', { issue: 'Aster class is outside the configured product' })
    const snapshot = this.#snapshot
    if (snapshot === null || snapshot.validUntil <= this.#now()) {
      if (reduceOnly) return NO_FEE
      throw new AppError('fee_code_missing')
    }
    const cap = snapshot.approvedCap
    if (cap === null) {
      if (reduceOnly) return NO_FEE
      throw new AppError('fee_approval_missing')
    }
    const field = this.#fees.feeFields(cls, type, tif, reduceOnly)
    if (field.kind !== 'order_field') return field
    const maker = snapshot.rates.find((row) => row.cls === cls && row.liquidity === 'maker')
    const taker = snapshot.rates.find((row) => row.cls === cls && row.liquidity === 'taker')
    if (
      maker === undefined ||
      taker === undefined ||
      maker.rate !== taker.rate ||
      dec.tryParse(maker.encoded) !== maker.rate ||
      dec.tryParse(taker.encoded) !== taker.rate
    ) {
      if (reduceOnly) return NO_FEE
      throw new AppError('fee_code_missing')
    }
    if (field.rate > cap) {
      if (reduceOnly) return NO_FEE
      throw new AppError('fee_above_cap')
    }
    return field
  }

  orderFields(
    cls: InstrumentClass,
    type: OrderType,
    tif: TimeInForce,
    reduceOnly: boolean,
  ): Readonly<Record<string, string>> {
    const field = this.feeFields(cls, type, tif, reduceOnly)
    return field.kind === 'order_field' ? { builder: field.builder, feeRate: field.encodedRate } : {}
  }
}
