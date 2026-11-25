import { AppError, dec, parseInstant, type Rate } from '@atr/core'
import {
  type Address,
  approveBuilderFeeTypedData,
  type Hex,
  type HlBuilder,
  type HlClass,
  type HlGrouping,
  type HlInstrument,
  type HlNetwork,
  type HlOrderAction,
  type HlOrderInput,
  type HlOrderWire,
  orderAction,
  orderWire,
  type PrivateKeySigner,
  recoverUserSigned,
  signL1,
  tenthBpToPercent,
} from '@atr/venue-hyperliquid/sign'
import type { Client } from '../client.ts'

const VENUE = 'hyperliquid'
const ADDRESS = /^0x[0-9a-fA-F]{40}$/
const STEP = dec.parse('0.00001')
const HARD_CAP = { perp: 20, hip3_perp: 20, spot: 20, outcome: 50 } as const satisfies Record<HlClass, number>

type Liquidity = 'maker' | 'taker'
type FeeRefusal = 'fee_approval_missing' | 'fee_above_cap'
type FeeField =
  | { readonly kind: 'none'; readonly reason: 'close_without_approval' }
  | { readonly kind: 'order_field'; readonly builder: Address; readonly encodedRate: string; readonly rate: Rate }

export interface HyperliquidDirectContext {
  readonly client: Pick<Client, 'accounts' | 'fees'>
  readonly accountRef: string
}

export interface HyperliquidDirectStart {
  readonly venue: typeof VENUE
  readonly rates: readonly {
    readonly cls: HlClass
    readonly liquidity: Liquidity
    readonly rate: Rate
    readonly encoded: string
  }[]
  readonly approvedCap: Rate | null
  readonly classCaps: Readonly<Partial<Record<HlClass, Rate>>>
  readonly builderRef: Address
  readonly validUntil: number
}

export interface DirectOrder {
  readonly instrument: HlInstrument
  readonly order: HlOrderInput
}

interface ApprovalAction {
  readonly type: 'approveBuilderFee'
  readonly maxFeeRate: `${string}%`
  readonly builder: Address
  readonly nonce: number
  readonly signatureChainId: Hex
  readonly hyperliquidChain: 'Mainnet' | 'Testnet'
}

export interface ExchangeRequest<Action = HlOrderAction | ApprovalAction> {
  readonly action: Action
  readonly nonce: number
  readonly signature: { readonly r: Hex; readonly s: Hex; readonly v: 27 | 28 }
  readonly vaultAddress: Address | null
  readonly expiresAfter: number | null
}

export interface HyperliquidDirectOptions<Reply> {
  readonly signer: PrivateKeySigner
  readonly network: HlNetwork
  /** Submit exactly one request. The caller owns transport and reconciliation after an ambiguous response. */
  readonly send: (request: ExchangeRequest) => Promise<Reply>
  readonly vaultAddress?: Address
  /** Master account for a separately authorised order signer. */
  readonly accountRef?: Address
  readonly now?: () => number
}

type BatchResult<Reply> =
  | { readonly indices: readonly number[]; readonly status: 'sent'; readonly response: Reply }
  | { readonly indices: readonly number[]; readonly status: 'refused'; readonly code: FeeRefusal | 'invalid_request' }
  | { readonly indices: readonly number[]; readonly status: 'unknown'; readonly error: unknown }

function address(input: string): Address {
  if (!ADDRESS.test(input)) throw new TypeError('invalid Hyperliquid address')
  return input.toLowerCase() as Address
}

function validClass(input: string): input is HlClass {
  return Object.hasOwn(HARD_CAP, input)
}

function assetClass(id: number): HlClass {
  if (!Number.isSafeInteger(id) || id < 0) throw new RangeError('invalid Hyperliquid asset id')
  if (id < 10_000) return 'perp'
  if (id < 100_000) return 'spot'
  if (id < 100_000_000) return 'hip3_perp'
  return 'outcome'
}

function feeRefusal(reduceOnly: boolean, code: FeeRefusal): FeeField {
  if (reduceOnly) return { kind: 'none', reason: 'close_without_approval' }
  throw new AppError(code)
}

export function linkAddress(
  client: Pick<Client, 'accounts'>,
  accountRef: string,
  sign: (message: string) => Promise<string>,
): ReturnType<Client['accounts']['link']> {
  return client.accounts.link(VENUE, address(accountRef), async (message) => ({
    scheme: 'eip191',
    signature: await sign(message),
  }))
}

/** Disable the built-in zero-fee Nautilus builder before using a separate direct order path. */
export const nautilusHyperliquidOptions = { include_builder_attribution: false } as const

export class HyperliquidDirect<Reply> {
  readonly venue = VENUE
  #context: HyperliquidDirectContext | null = null
  #schedule: HyperliquidDirectStart | null = null
  #nonce = -1
  #queue: Promise<void> = Promise.resolve()
  readonly #now: () => number
  readonly #vault: Address | null
  readonly #account: Address

  constructor(readonly options: HyperliquidDirectOptions<Reply>) {
    this.#now = options.now ?? Date.now
    this.#vault = options.vaultAddress === undefined ? null : address(options.vaultAddress)
    this.#account = address(options.accountRef ?? options.signer.address)
  }

  #serial<T>(work: () => Promise<T>): Promise<T> {
    const next = this.#queue.then(work)
    this.#queue = next.then(
      () => {},
      () => {},
    )
    return next
  }

  #nextNonce(): number {
    const now = this.#now()
    if (!Number.isSafeInteger(now) || now < 0) throw new TypeError('invalid Hyperliquid clock')
    this.#nonce = Math.max(now, this.#nonce + 1)
    return this.#nonce
  }

  start(ctx: HyperliquidDirectContext): Promise<HyperliquidDirectStart> {
    return this.#serial(async () => {
      if (address(ctx.accountRef) !== this.#account) throw new AppError('account_not_bound')
      this.#context = { ...ctx, accountRef: this.#account }
      return this.#read()
    })
  }

  async #read(): Promise<HyperliquidDirectStart> {
    const ctx = this.#context
    this.#schedule = null
    if (ctx === null) throw new AppError('fee_approval_missing')
    const accounts = await ctx.client.accounts.list()
    if (!accounts.ok) throw new AppError('account_not_bound')
    const bound = accounts.data.filter(
      (account) =>
        account.venue === VENUE &&
        account.accountRef.toLowerCase() === ctx.accountRef &&
        (account.subAccountRef?.toLowerCase() ?? null) === this.#vault,
    )
    const account = bound.length === 1 ? bound[0] : undefined
    if (account === undefined) throw new AppError('account_not_bound')
    const reply = await ctx.client.fees.schedule({ venue: VENUE, accountId: account.accountId })
    if (!reply.ok) throw new AppError('unavailable')
    const matching = reply.data.filter((schedule) => schedule.venue === VENUE)
    const schedule = matching.length === 1 ? matching[0] : undefined
    if (schedule?.builderRef == null || schedule.unit !== 'tenth_bp' || dec.parse(schedule.step) !== STEP) {
      throw new AppError('fee_approval_missing')
    }
    const validUntil = parseInstant(schedule.validUntil)
    if (validUntil <= this.#now()) throw new AppError('fee_approval_missing')

    const classCaps: Partial<Record<HlClass, Rate>> = {}
    for (const [cls, cap] of Object.entries(schedule.classCaps)) {
      if (!validClass(cls) || cap === undefined) continue
      const local = (BigInt(HARD_CAP[cls]) * STEP) as Rate
      classCaps[cls] = dec.min(dec.parse(cap), local) as Rate
    }
    const rates: HyperliquidDirectStart['rates'][number][] = []
    const seen = new Set<string>()
    for (const row of schedule.rates) {
      if (
        !validClass(row.cls) ||
        row.originClass !== 'algo' ||
        (row.liquidity !== 'maker' && row.liquidity !== 'taker')
      )
        continue
      const key = `${row.cls}/${row.liquidity}`
      const amount = dec.parse(row.rate) as Rate
      if (
        !/^[1-9][0-9]{0,3}$/.test(row.encodedRate) ||
        amount !== BigInt(row.encodedRate) * STEP ||
        parseInstant(row.validFrom) > this.#now() ||
        parseInstant(row.validUntil) < validUntil ||
        seen.has(key)
      ) {
        throw new AppError('fee_approval_missing')
      }
      seen.add(key)
      rates.push({ cls: row.cls, liquidity: row.liquidity, rate: amount, encoded: row.encodedRate })
    }
    const approvedCap =
      schedule.approval?.state === 'approved' && schedule.approval.cap !== null
        ? (dec.parse(schedule.approval.cap) as Rate)
        : null
    const current = {
      venue: VENUE,
      rates,
      approvedCap,
      classCaps,
      builderRef: address(schedule.builderRef),
      validUntil,
    } as const
    for (const row of rates) Object.freeze(row)
    Object.freeze(rates)
    Object.freeze(classCaps)
    this.#schedule = Object.freeze(current)
    return current
  }

  feeFields(
    cls: HlClass,
    type: 'limit' | 'market' | 'trigger',
    tif: 'Alo' | 'Ioc' | 'Gtc',
    reduceOnly: boolean,
  ): FeeField {
    const current = this.#schedule
    if (
      current === null ||
      current.validUntil <= this.#now() ||
      current.approvedCap === null ||
      current.approvedCap <= 0n
    )
      return feeRefusal(reduceOnly, 'fee_approval_missing')
    const liquidity = type === 'limit' && tif === 'Alo' ? 'maker' : 'taker'
    const row = current.rates.find((rate) => rate.cls === cls && rate.liquidity === liquidity)
    const cap = current.classCaps[cls]
    if (row === undefined || cap === undefined) return feeRefusal(reduceOnly, 'fee_approval_missing')
    if (row.rate > cap || row.rate > current.approvedCap) return feeRefusal(reduceOnly, 'fee_above_cap')
    return { kind: 'order_field', builder: current.builderRef, encodedRate: row.encoded, rate: row.rate }
  }

  placeBatch(
    inputs: readonly DirectOrder[],
    grouping: HlGrouping = 'na',
    expiresAfter: number | null = null,
  ): Promise<readonly BatchResult<Reply>[]> {
    return this.#serial(async () => {
      if (
        !['na', 'normalTpsl', 'positionTpsl'].includes(grouping) ||
        (expiresAfter !== null && (!Number.isSafeInteger(expiresAfter) || expiresAfter <= this.#now()))
      ) {
        throw new AppError('invalid_request')
      }
      if (this.#schedule === null || this.#schedule.validUntil <= this.#now()) await this.#read().catch(() => {})
      const planned: { wire: HlOrderWire; builder: HlBuilder | null; index: number }[] = []
      const result: BatchResult<Reply>[] = []
      for (const [index, { instrument, order }] of inputs.entries()) {
        let wire: HlOrderWire
        let fee: FeeField
        try {
          if (
            assetClass(order.assetId) !== instrument.cls ||
            ('limit' in order.type && !['Alo', 'Ioc', 'Gtc'].includes(order.type.limit.tif))
          ) {
            throw new AppError('invalid_request')
          }
          wire = orderWire(order, instrument)
          fee =
            'limit' in order.type
              ? this.feeFields(instrument.cls, 'limit', order.type.limit.tif, order.reduceOnly)
              : this.feeFields(instrument.cls, 'trigger', 'Gtc', order.reduceOnly)
        } catch (error) {
          const code =
            error instanceof AppError && (error.code === 'fee_above_cap' || error.code === 'fee_approval_missing')
              ? error.code
              : 'invalid_request'
          result.push({ indices: [index], status: 'refused', code })
          continue
        }
        const builder = fee.kind === 'order_field' ? { b: fee.builder, f: Number(fee.encodedRate) } : null
        planned.push({ wire, builder, index })
      }
      const groups: (typeof planned)[] = []
      for (const row of planned) {
        const tail = groups.at(-1)
        const prior = tail?.[0]
        if (tail !== undefined && prior?.builder?.f === row.builder?.f) tail.push(row)
        else groups.push([row])
      }
      if (grouping !== 'na' && (groups.length !== 1 || result.length > 0)) {
        if (result.length === 0 && planned.length > 0 && planned.every(({ wire }) => wire.r)) {
          groups.splice(
            0,
            groups.length,
            planned.map((row) => ({ ...row, builder: null })),
          )
        } else {
          return [{ indices: inputs.map((_, i) => i), status: 'refused', code: 'invalid_request' }]
        }
      }
      // Sign every group before the first send: a signer failure must not hide orders that already went out.
      const signed: {
        indices: number[]
        action: HlOrderAction
        nonce: number
        signature: Awaited<ReturnType<typeof signL1>>
      }[] = []
      for (const rows of groups) {
        const indices = rows.map(({ index }) => index)
        const action = orderAction(
          rows.map(({ wire }) => wire),
          grouping,
          rows[0]?.builder ?? null,
        )
        const nonce = this.#nextNonce()
        const signature = await signL1(
          this.options.signer,
          action,
          this.#vault,
          nonce,
          expiresAfter,
          this.options.network,
        )
        signed.push({ indices, action, nonce, signature })
      }
      for (const { indices, action, nonce, signature } of signed) {
        try {
          const response = await this.options.send({
            action,
            nonce,
            signature,
            vaultAddress: this.#vault,
            expiresAfter,
          })
          result.push({ indices, status: 'sent', response })
        } catch (error) {
          result.push({ indices, status: 'unknown', error })
        }
      }
      return result.sort((a, b) => (a.indices[0] ?? 0) - (b.indices[0] ?? 0))
    })
  }

  /** The caller supplies the master wallet and a venue read-back. A failed post still receives one read-back. */
  approveBuilderFee(
    owner: PrivateKeySigner,
    signatureChainId: Hex,
    capTenthBp: 0 | 20 | 50,
    readCap: (account: Address, builder: Address) => Promise<number>,
  ): Promise<number> {
    return this.#serial(async () => {
      const account = this.#context?.accountRef
      if (account === undefined || address(owner.address) !== account || this.#vault !== null)
        throw new AppError('signer_mismatch')
      const schedule =
        this.#schedule !== null && this.#schedule.validUntil > this.#now() ? this.#schedule : await this.#read()
      const { builderRef: builder } = schedule
      if (capTenthBp !== 0 && capTenthBp !== 20 && capTenthBp !== 50) throw new AppError('fee_above_cap')
      const maxFeeRate = tenthBpToPercent(capTenthBp)
      const nonce = this.#nextNonce()
      const network = this.options.network
      const data = approveBuilderFeeTypedData({ builder, maxFeeRate, nonce, signatureChainId, network })
      const raw = await owner.signTypedData(data)
      if (!/^0x[0-9a-fA-F]{130}$/.test(raw)) throw new AppError('signer_mismatch')
      const recovery = Number.parseInt(raw.slice(130), 16)
      const v = recovery < 27 ? recovery + 27 : recovery
      if (v !== 27 && v !== 28) throw new AppError('signer_mismatch')
      const signature = { r: `0x${raw.slice(2, 66)}` as Hex, s: `0x${raw.slice(66, 130)}` as Hex, v } as const
      if (address(await recoverUserSigned(data, signature)) !== account) throw new AppError('signer_mismatch')
      this.#schedule = null
      const action: ApprovalAction = {
        maxFeeRate,
        builder,
        nonce,
        type: 'approveBuilderFee',
        signatureChainId,
        hyperliquidChain: network === 'mainnet' ? 'Mainnet' : 'Testnet',
      }
      await this.options.send({ action, nonce, signature, vaultAddress: null, expiresAfter: null }).catch(() => {})
      const observed = await readCap(address(account), builder)
      if (!Number.isSafeInteger(observed) || (capTenthBp === 0 ? observed !== 0 : observed < capTenthBp)) {
        throw new AppError('unknown_outcome')
      }
      return observed
    })
  }
}
