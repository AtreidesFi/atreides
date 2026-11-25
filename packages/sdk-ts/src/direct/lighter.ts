// Direct Lighter signing with a caller-owned lighter-ts-sdk WasmSignerClient (06 §2.5–2.7).
import type { FeeField, OrderType, TimeInForce } from '@atr/contract'
import { AppError, dec, type InstrumentClass, rate } from '@atr/core'
import { type DirectContext, type DirectModule, type DirectStart, directFees } from './template.ts'

export type LighterClass = 'perp' | 'rwa_perp' | 'spot' | 'stock_token'
export type LighterTier = 'standard' | 'plus' | 'premium'
export const MAX_LIGHTER_UINT48 = 2 ** 48 - 1

export interface LighterAttribution {
  readonly integratorAccountIndex: number
  // money-ok: native Lighter taker fee in exact integer parts per million.
  readonly integratorTakerFee: number
  // money-ok: native Lighter maker fee in exact integer parts per million.
  readonly integratorMakerFee: number
}
export interface LighterApproval {
  readonly integratorIndex: number
  readonly expiresAt: number
  // money-ok: native Lighter cap in exact integer parts per million.
  readonly maxPerpsTakerFee: number
  // money-ok: native Lighter cap in exact integer parts per million.
  readonly maxPerpsMakerFee: number
  // money-ok: native Lighter cap in exact integer parts per million.
  readonly maxSpotTakerFee: number
  // money-ok: native Lighter cap in exact integer parts per million.
  readonly maxSpotMakerFee: number
}
export interface LighterAccount {
  readonly accountIndex: number
  readonly tier: LighterTier
  readonly approvals: readonly LighterApproval[]
}
export interface LighterOrder {
  readonly marketIndex: number
  readonly clientOrderIndex: number
  // money-ok: native Lighter amount as an exact integer in the instrument's base units.
  readonly baseAmount: number
  // money-ok: native Lighter price as an exact integer in the instrument's price units.
  readonly price: number
  readonly isAsk: 0 | 1
  readonly orderType: number
  readonly timeInForce: number
  readonly reduceOnly: 0 | 1
  // money-ok: native Lighter trigger as an exact integer in the instrument's price units.
  readonly triggerPrice: number
  readonly orderExpiry: number
}
export interface LighterSelfTrade {
  readonly selfTradeBehaviorMode?: number
  readonly selfTradeEqualityMode?: number
}
export interface LighterModification {
  readonly marketIndex: number
  readonly index: number
  // money-ok: native Lighter amount as an exact integer in the instrument's base units.
  readonly baseAmount: number
  // money-ok: native Lighter price as an exact integer in the instrument's price units.
  readonly price: number
  // money-ok: native Lighter trigger as an exact integer in the instrument's price units.
  readonly triggerPrice: number
  readonly orderVersion?: number
  /** Venue readback from the original order. Lighter binds these three attributes on modify. */
  readonly originalAttribution: LighterAttribution
}
interface SigningLane {
  readonly accountIndex: number
  readonly apiKeyIndex: number
  readonly nonce: number
}
export interface LighterSigned {
  readonly txType: number
  readonly txInfo: string
  readonly txHash: string
}
export interface LighterFeeApprovalDraft extends LighterSigned {
  readonly messageToSign: string
  readonly approval: LighterApproval
}
interface NativeSigned extends Partial<LighterSigned> {
  readonly error?: string
  readonly messageToSign?: string
}
/** Sign-only methods of a caller-owned Lighter signer. Adapt lighter-ts-sdk 1.1.0 with `withLighterWasmModify`. */
export interface LighterNativeSigner {
  signCreateOrder(input: LighterOrder & LighterAttribution & LighterSelfTrade & SigningLane): Promise<NativeSigned>
  signModifyOrder(
    input: Omit<LighterModification, 'originalAttribution'> & LighterAttribution & LighterSelfTrade & SigningLane,
  ): Promise<NativeSigned>
  signCreateGroupedOrders(
    input: { groupingType: number; orders: LighterOrder[] } & LighterAttribution & LighterSelfTrade & SigningLane,
  ): Promise<NativeSigned>
  signApproveIntegrator(
    input: Omit<LighterApproval, 'expiresAt'> & { approvalExpiry: number } & SigningLane,
  ): Promise<NativeSigned>
}

export type LighterWasmModify = (
  marketIndex: number,
  orderIndex: number,
  // money-ok: uint48 amount in native base units, exactly representable in JS.
  baseAmount: number,
  // money-ok: uint32 limit price in native units, exactly representable in JS.
  price: number,
  // money-ok: uint32 trigger price in native units, exactly representable in JS.
  triggerPrice: number,
  integratorIndex: number,
  takerPpm: number,
  makerPpm: number,
  selfTradeBehavior: number,
  selfTradeEquality: number,
  skipNonce: 0,
  nonce: number,
  orderVersion: number,
  apiKeyIndex: number,
  accountIndex: number,
) => NativeSigned

/** Pass the SignModifyOrder export from the same pinned lighter-go v1.0.12 instance loaded by the client. */
export function withLighterWasmModify(
  signer: Omit<LighterNativeSigner, 'signModifyOrder'>,
  signModify: LighterWasmModify,
): LighterNativeSigner {
  return {
    signCreateOrder: (input) => signer.signCreateOrder(input),
    signCreateGroupedOrders: (input) => signer.signCreateGroupedOrders(input),
    signApproveIntegrator: (input) => signer.signApproveIntegrator(input),
    // lighter-ts-sdk 1.1.0 passes orderVersion before skipNonce and nonce.
    async signModifyOrder(input) {
      return signModify(
        input.marketIndex,
        input.index,
        input.baseAmount,
        input.price,
        input.triggerPrice,
        input.integratorAccountIndex,
        input.integratorTakerFee,
        input.integratorMakerFee,
        input.selfTradeBehaviorMode ?? 0,
        input.selfTradeEqualityMode ?? 0,
        0,
        input.nonce,
        input.orderVersion ?? 0,
        input.apiKeyIndex,
        input.accountIndex,
      )
    },
  }
}

export interface LighterDirectOptions {
  readonly signer: LighterNativeSigner
  readonly accountIndex: number
  readonly apiKeyIndex: number
  readonly markets: ReadonlyMap<number, LighterClass>
  /** Read accountLimits and approved_integrators from the caller's Lighter connection. */
  readonly readAccount: () => Promise<LighterAccount>
  readonly standardTracking?: true
  readonly now?: () => number
}
export interface LighterDirectStart extends DirectStart {
  readonly tier: LighterTier
  readonly approval: LighterApproval | null
}

const NO_ATTRIBUTION: LighterAttribution = Object.freeze({
  integratorAccountIndex: 0,
  integratorTakerFee: 0,
  integratorMakerFee: 0,
})
const NO_FEE: FeeField = { kind: 'none', reason: 'close_without_approval' }
const HOUR = 3_600_000
const MAX_UINT32 = 2 ** 32 - 1
const caps = { perp: 1000, rwa_perp: 1000, spot: 10_000, stock_token: 10_000 } as const
const isLighterClass = (cls: string): cls is LighterClass => Object.hasOwn(caps, cls)
const isInt = (n: unknown, max = MAX_LIGHTER_UINT48): n is number =>
  typeof n === 'number' && Number.isSafeInteger(n) && n >= 0 && n <= max
const spot = (cls: LighterClass) => cls === 'spot' || cls === 'stock_token'
function builderIndex(value: string | null): number {
  if (value === null || !/^[1-9][0-9]{0,14}$/u.test(value) || !isInt(Number(value))) {
    throw new AppError('fee_code_missing')
  }
  return Number(value)
}

function checkApprovalCaps(cls: LighterClass, approval: LighterApproval, attrs: LighterAttribution): void {
  const [takerCap, makerCap] = spot(cls)
    ? [approval.maxSpotTakerFee, approval.maxSpotMakerFee]
    : [approval.maxPerpsTakerFee, approval.maxPerpsMakerFee]
  if (
    !isInt(takerCap, caps[cls]) ||
    !isInt(makerCap, caps[cls]) ||
    attrs.integratorTakerFee > takerCap ||
    attrs.integratorMakerFee > makerCap
  ) {
    throw new AppError('fee_above_cap')
  }
}

function checkedOrder(order: LighterOrder): LighterOrder {
  const {
    marketIndex,
    clientOrderIndex,
    baseAmount,
    price,
    isAsk,
    orderType,
    timeInForce,
    reduceOnly,
    triggerPrice,
    orderExpiry,
  } = order
  if (
    !isInt(marketIndex) ||
    !isInt(clientOrderIndex) ||
    !isInt(baseAmount) ||
    !isInt(price, MAX_UINT32) ||
    price === 0 ||
    !isInt(isAsk, 1) ||
    !isInt(orderType, 6) ||
    !isInt(timeInForce, 2) ||
    !isInt(reduceOnly, 1) ||
    !isInt(triggerPrice, MAX_UINT32) ||
    !isInt(orderExpiry, Number.MAX_SAFE_INTEGER)
  ) {
    throw new AppError('invalid_request', { issue: 'invalid Lighter order wire units' })
  }
  return {
    marketIndex,
    clientOrderIndex,
    baseAmount,
    price,
    isAsk,
    orderType,
    timeInForce,
    reduceOnly,
    triggerPrice,
    orderExpiry,
  }
}

function selfTradeFields({ selfTradeBehaviorMode = 0, selfTradeEqualityMode = 0 }: LighterSelfTrade) {
  if (!isInt(selfTradeBehaviorMode, 3) || !isInt(selfTradeEqualityMode, 1)) {
    throw new AppError('invalid_request', { issue: 'invalid Lighter self-trade mode' })
  }
  return { selfTradeBehaviorMode, selfTradeEqualityMode }
}

function object(value: unknown): Readonly<Record<string, unknown>> {
  return value !== null && typeof value === 'object' && !Array.isArray(value)
    ? (value as Readonly<Record<string, unknown>>)
    : {}
}

async function checkedSignature(
  result: Promise<NativeSigned>,
  type: number,
  lane: SigningLane,
  attrs: (LighterAttribution & LighterSelfTrade & { readonly orderVersion?: number }) | LighterApproval,
  ownerApproval = false,
): Promise<LighterSigned & { readonly messageToSign?: string }> {
  try {
    const { txType, txInfo, txHash, error, messageToSign } = await result
    if (
      error ||
      (type !== 45 && messageToSign) ||
      (ownerApproval && (!messageToSign || messageToSign.length > 4096)) ||
      txType !== type ||
      typeof txInfo !== 'string' ||
      typeof txHash !== 'string' ||
      txHash.length === 0
    ) {
      throw new Error('Lighter native signature was refused')
    }
    const row = object(JSON.parse(txInfo) as unknown)
    if (
      row.AccountIndex !== lane.accountIndex ||
      row.ApiKeyIndex !== lane.apiKeyIndex ||
      row.Nonce !== lane.nonce ||
      typeof row.Sig !== 'string' ||
      row.Sig.length === 0
    )
      throw new Error('Lighter signature lane mismatch')
    if ('integratorAccountIndex' in attrs) {
      const wire = object(row.L2TxAttributes)
      if (
        (wire['1'] ?? 0) !== attrs.integratorAccountIndex ||
        (wire['2'] ?? 0) !== attrs.integratorTakerFee ||
        (wire['3'] ?? 0) !== attrs.integratorMakerFee ||
        (wire['6'] ?? 0) !== (attrs.selfTradeBehaviorMode ?? 0) ||
        (wire['7'] ?? 0) !== (attrs.selfTradeEqualityMode ?? 0) ||
        (wire['8'] ?? 0) !== (attrs.orderVersion ?? 0)
      )
        throw new Error('Lighter signer changed signed attributes')
    } else if (
      (row.L1Sig !== undefined && row.L1Sig !== '') ||
      row.IntegratorAccountIndex !== attrs.integratorIndex ||
      row.ApprovalExpiry !== attrs.expiresAt ||
      row.MaxPerpsTakerFee !== attrs.maxPerpsTakerFee ||
      row.MaxPerpsMakerFee !== attrs.maxPerpsMakerFee ||
      row.MaxSpotTakerFee !== attrs.maxSpotTakerFee ||
      row.MaxSpotMakerFee !== attrs.maxSpotMakerFee
    ) {
      throw new Error('Lighter signer changed approval')
    }
    return ownerApproval && messageToSign ? { txType, txInfo, txHash, messageToSign } : { txType, txInfo, txHash }
  } catch {
    throw new AppError('signature_invalid')
  }
}

export class LighterDirect implements DirectModule {
  readonly venue = 'lighter' as const
  readonly #now: () => number
  readonly #fees: ReturnType<typeof directFees>
  #quote: DirectStart | null = null
  #account: { readonly value: LighterAccount; readonly readAt: number } | null = null
  #reading: Promise<LighterAccount> | null = null
  #startGeneration = 0

  constructor(readonly options: LighterDirectOptions) {
    if (!isInt(options.accountIndex) || !isInt(options.apiKeyIndex, 254)) throw new AppError('invalid_request')
    this.#now = options.now ?? Date.now
    this.#fees = directFees({
      venue: this.venue,
      requiresApproval: false,
      now: this.#now,
      capFor: (cls) => rate.fromFraction(isLighterClass(cls) && spot(cls) ? '0.01' : '0.001'),
      encode(row, builder) {
        const units = dec.toUnits(row.rate, 6, 'down')
        if (dec.fromUnits(units, 6, 'down') !== row.rate || row.encoded !== String(units)) {
          throw new AppError('fee_code_missing')
        }
        return {
          kind: 'order_field',
          builder: String(builderIndex(builder)),
          encodedRate: String(units),
          rate: row.rate,
        }
      },
    })
  }

  async refreshAccount(): Promise<LighterAccount> {
    if (this.#reading !== null) return this.#reading
    const pending = Promise.resolve()
      .then(() => this.options.readAccount())
      .then((account) => {
        if (account.accountIndex !== this.options.accountIndex) throw new AppError('account_not_bound')
        if (account.tier !== 'standard' && account.tier !== 'plus' && account.tier !== 'premium') {
          throw new AppError('fee_code_missing')
        }
        const value = Object.freeze({
          ...account,
          approvals: Object.freeze(account.approvals.map((row) => Object.freeze({ ...row }))),
        })
        this.#account = { value, readAt: this.#now() }
        return value
      })
      .finally(() => {
        this.#reading = null
      })
    this.#reading = pending
    return pending
  }

  async start(ctx: DirectContext): Promise<LighterDirectStart> {
    if (ctx.accountRef !== String(this.options.accountIndex)) throw new AppError('account_not_bound')
    const generation = ++this.#startGeneration
    this.#quote = null
    this.#account = null
    const [quote, account] = await Promise.all([this.#fees.start(ctx), this.refreshAccount()])
    if (generation !== this.#startGeneration) throw new AppError('account_not_bound')
    this.#quote = quote
    const index = quote.builderRef === null ? null : builderIndex(quote.builderRef)
    return { ...quote, tier: account.tier, approval: index === null ? null : this.#approval(account, index) }
  }

  #approval(account: LighterAccount, index: number): LighterApproval | null {
    const rows = account.approvals.filter(
      ({ integratorIndex, expiresAt }) => integratorIndex === index && isInt(expiresAt) && expiresAt > this.#now(),
    )
    return rows.length === 1 ? (rows[0] ?? null) : null
  }

  #policy(cls: LighterClass, selfTrade: LighterSelfTrade): LighterAttribution {
    const account = this.#account?.value
    const quote = this.#quote
    if (account === undefined || quote === null || quote.validUntil <= this.#now())
      throw new AppError('fee_code_missing')
    if (account.tier === 'standard' && this.options.standardTracking !== true) return NO_ATTRIBUTION
    const index = builderIndex(quote.builderRef)
    const approval = this.#approval(account, index)
    if (approval === null) {
      if (account.tier === 'standard') return NO_ATTRIBUTION
      throw new AppError('fee_approval_missing')
    }
    if (account.tier === 'standard') return { ...NO_ATTRIBUTION, integratorAccountIndex: index }
    const paidApproval = spot(cls)
      ? approval.maxSpotTakerFee > 0 && approval.maxSpotMakerFee > 0
      : approval.maxPerpsTakerFee > 0 && approval.maxPerpsMakerFee > 0
    if (!paidApproval) throw new AppError('fee_approval_missing')
    if (selfTrade.selfTradeBehaviorMode || selfTrade.selfTradeEqualityMode) {
      return { ...NO_ATTRIBUTION, integratorAccountIndex: index }
    }
    const taker = this.#fees.feeFields(cls, 'market', 'ioc', false)
    const maker = this.#fees.feeFields(cls, 'limit', 'post_only', false)
    if (taker.kind !== 'order_field' || maker.kind !== 'order_field') throw new AppError('fee_code_missing')
    const attrs = {
      integratorAccountIndex: index,
      integratorTakerFee: Number(taker.encodedRate),
      integratorMakerFee: Number(maker.encodedRate),
    }
    checkApprovalCaps(cls, approval, attrs)
    return attrs
  }

  feeFields(cls: InstrumentClass, type: OrderType, tif: TimeInForce, reduceOnly: boolean): FeeField {
    try {
      if (!isLighterClass(cls)) throw new AppError('invalid_request', { issue: 'unknown Lighter instrument class' })
      const attrs = this.#policy(cls, {})
      if (attrs.integratorAccountIndex === 0) return { kind: 'none', reason: 'venue_has_no_fee_path' }
      const units = type === 'limit' && tif === 'post_only' ? attrs.integratorMakerFee : attrs.integratorTakerFee
      return {
        kind: 'order_field',
        builder: String(attrs.integratorAccountIndex),
        encodedRate: String(units),
        rate: rate.fromFraction(dec.format(dec.fromUnits(BigInt(units), 6, 'down'))),
      }
    } catch (error) {
      if (reduceOnly) return NO_FEE
      throw error
    }
  }

  #lane(nonce: number): SigningLane {
    if (!isInt(nonce)) throw new AppError('invalid_request', { issue: 'Lighter nonce must be uint48' })
    return { accountIndex: this.options.accountIndex, apiKeyIndex: this.options.apiKeyIndex, nonce }
  }

  async #refresh(): Promise<void> {
    const generation = this.#startGeneration
    const quote = await this.#fees.refresh()
    if (this.#account === null || this.#now() - this.#account.readAt >= HOUR) await this.refreshAccount()
    if (generation !== this.#startGeneration) throw new AppError('account_not_bound')
    this.#quote = quote
  }

  async #attribution(
    marketIndex: number,
    reduceOnly: boolean,
    selfTrade: LighterSelfTrade,
  ): Promise<LighterAttribution> {
    try {
      const cls = this.options.markets.get(marketIndex)
      if (cls === undefined || !isLighterClass(cls))
        throw new AppError('invalid_request', { issue: 'unknown Lighter market' })
      await this.#refresh()
      return this.#policy(cls, selfTrade)
    } catch (error) {
      if (reduceOnly) return NO_ATTRIBUTION
      throw error
    }
  }

  async signCreateOrder(order: LighterOrder, nonce: number, selfTrade: LighterSelfTrade = {}): Promise<LighterSigned> {
    const lane = this.#lane(nonce)
    const input = checkedOrder(order)
    const modes = selfTradeFields(selfTrade)
    const attrs = await this.#attribution(input.marketIndex, input.reduceOnly === 1, modes)
    return checkedSignature(this.options.signer.signCreateOrder({ ...input, ...attrs, ...modes, ...lane }), 14, lane, {
      ...attrs,
      ...modes,
    })
  }

  async signModifyOrder(
    order: LighterModification,
    nonce: number,
    selfTrade: LighterSelfTrade = {},
  ): Promise<LighterSigned> {
    const lane = this.#lane(nonce)
    const { originalAttribution, marketIndex, index, baseAmount, price, triggerPrice, orderVersion } = order
    const modes = selfTradeFields(selfTrade)
    if (
      !isInt(marketIndex) ||
      !isInt(index, Number.MAX_SAFE_INTEGER) ||
      index === 0 ||
      !isInt(baseAmount) ||
      !isInt(price, MAX_UINT32) ||
      price === 0 ||
      !isInt(triggerPrice, MAX_UINT32) ||
      (orderVersion !== undefined && !isInt(orderVersion))
    )
      throw new AppError('invalid_request')
    const { integratorAccountIndex, integratorTakerFee, integratorMakerFee } = originalAttribution
    if (!isInt(integratorAccountIndex) || !isInt(integratorTakerFee, 10_000) || !isInt(integratorMakerFee, 10_000)) {
      throw new AppError('fee_code_missing')
    }
    const attrs = { integratorAccountIndex, integratorTakerFee, integratorMakerFee }
    if (integratorAccountIndex !== 0 || integratorTakerFee !== 0 || integratorMakerFee !== 0) {
      const cls = this.options.markets.get(marketIndex)
      if (cls === undefined || !isLighterClass(cls))
        throw new AppError('invalid_request', { issue: 'unknown Lighter market' })
      const generation = this.#startGeneration
      if (integratorAccountIndex !== builderIndex(this.#quote?.builderRef ?? null)) throw new AppError('fee_above_cap')
      // Modify keeps the order's signed attribution. The live venue approval governs the old rates.
      const account = await this.refreshAccount()
      if (generation !== this.#startGeneration) throw new AppError('account_not_bound')
      const approval = this.#approval(account, integratorAccountIndex)
      if (approval === null) throw new AppError('fee_approval_missing')
      if (
        (integratorTakerFee !== 0 || integratorMakerFee !== 0) &&
        (account.tier === 'standard' || modes.selfTradeBehaviorMode || modes.selfTradeEqualityMode)
      )
        throw new AppError('fee_above_cap')
      checkApprovalCaps(cls, approval, attrs)
    }
    const request = {
      marketIndex,
      index,
      baseAmount,
      price,
      triggerPrice,
      ...attrs,
      ...modes,
      ...lane,
      ...(orderVersion === undefined ? {} : { orderVersion }),
    }
    return checkedSignature(this.options.signer.signModifyOrder(request), 17, lane, request)
  }

  async signCreateGroupedOrders(
    groupingType: 1 | 2 | 3,
    orders: readonly LighterOrder[],
    nonce: number,
    selfTrade: LighterSelfTrade = {},
  ): Promise<LighterSigned> {
    const lane = this.#lane(nonce)
    const input = orders.map(checkedOrder)
    const first = input[0]
    const modes = selfTradeFields(selfTrade)
    if (
      first === undefined ||
      ![1, 2, 3].includes(groupingType) ||
      input.length !== (groupingType === 3 ? 3 : 2) ||
      input.some(({ marketIndex }) => marketIndex !== first.marketIndex)
    )
      throw new AppError('invalid_request')
    const attrs = await this.#attribution(
      first.marketIndex,
      input.every(({ reduceOnly }) => reduceOnly === 1),
      modes,
    )
    return checkedSignature(
      this.options.signer.signCreateGroupedOrders({ groupingType, orders: input, ...attrs, ...modes, ...lane }),
      28,
      lane,
      { ...attrs, ...modes },
    )
  }

  /** Prepare the exact Plus/Premium approval with the caller-owned bot signer. */
  async prepareFeeApproval(nonce: number, expiresAt: number): Promise<LighterFeeApprovalDraft> {
    const lane = this.#lane(nonce)
    await this.#refresh()
    const account = await this.refreshAccount()
    if (account.tier === 'standard' || !isInt(expiresAt) || expiresAt <= this.#now()) {
      throw new AppError('invalid_request')
    }
    const quote = this.#quote
    if (quote === null || quote.validUntil <= this.#now()) throw new AppError('fee_code_missing')
    const integratorIndex = builderIndex(quote.builderRef)
    const cap = (primary: 'perp' | 'spot', secondary: 'rwa_perp' | 'stock_token') => {
      const limits = [quote.classCaps[primary], quote.classCaps[secondary]].filter((value) => value !== undefined)
      if (limits.length === 0) return 0
      const limit = limits.reduce((left, right) => (left < right ? left : right))
      const units = Number(dec.toUnits(limit, 6, 'down'))
      if (!isInt(units, 1_000_000) || units === 0) throw new AppError('fee_code_missing')
      return units
    }
    const perps = cap('perp', 'rwa_perp')
    const spot = cap('spot', 'stock_token')
    const approval: LighterApproval = {
      integratorIndex,
      expiresAt,
      maxPerpsTakerFee: perps,
      maxPerpsMakerFee: perps,
      maxSpotTakerFee: spot,
      maxSpotMakerFee: spot,
    }
    if (perps === 0 && spot === 0) throw new AppError('fee_code_missing')
    const { expiresAt: approvalExpiry, ...caps } = approval
    const signed = await checkedSignature(
      this.options.signer.signApproveIntegrator({ ...caps, approvalExpiry, ...lane }),
      45,
      lane,
      approval,
      true,
    )
    if (signed.messageToSign === undefined) throw new AppError('signature_invalid')
    return { ...signed, messageToSign: signed.messageToSign, approval }
  }

  /** User opt-in on a Standard account. This never signs a non-zero cap or uses an L1 signing key. */
  async signApproveTracking(nonce: number, expiresAt: number): Promise<LighterSigned> {
    const lane = this.#lane(nonce)
    if (this.options.standardTracking !== true) throw new AppError('fee_approval_missing')
    await this.#refresh()
    if ((await this.refreshAccount()).tier !== 'standard' || !isInt(expiresAt) || expiresAt <= this.#now()) {
      throw new AppError('invalid_request')
    }
    const integratorIndex = builderIndex(this.#quote?.builderRef ?? null)
    const attrs = {
      integratorIndex,
      expiresAt,
      maxPerpsTakerFee: 0,
      maxPerpsMakerFee: 0,
      maxSpotTakerFee: 0,
      maxSpotMakerFee: 0,
    }
    const { expiresAt: approvalExpiry, ...caps } = attrs
    return checkedSignature(
      this.options.signer.signApproveIntegrator({ ...caps, approvalExpiry, ...lane }),
      45,
      lane,
      attrs,
    )
  }
}
