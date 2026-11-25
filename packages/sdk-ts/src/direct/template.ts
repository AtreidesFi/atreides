import type { FeeField, OrderType, TimeInForce } from '@atr/contract'
import { AppError, dec, type InstrumentClass, parseInstant, type Rate, type VenueId } from '@atr/core'
import type { Client } from '../client.ts'

export interface DirectContext {
  readonly client: Pick<Client, 'accounts' | 'fees'>
  readonly accountRef: string
}

interface PublishedRate {
  readonly cls: InstrumentClass
  readonly liquidity: 'maker' | 'taker'
  readonly rate: Rate
  readonly encoded: string
}

export interface DirectStart {
  readonly venue: VenueId
  readonly rates: readonly PublishedRate[]
  readonly approvedCap: Rate | null
  readonly classCaps: Readonly<Partial<Record<InstrumentClass, Rate>>>
  readonly builderRef: string | null
  readonly validUntil: number
}

export interface DirectModule {
  readonly venue: VenueId
  start(ctx: DirectContext): Promise<DirectStart>
  feeFields(cls: InstrumentClass, type: OrderType, tif: TimeInForce, reduceOnly: boolean): FeeField
}

export interface DirectFeeOptions {
  readonly venue: VenueId
  readonly requiresApproval: boolean
  /** Venue account spelling, e.g. lowercased EVM addresses. */
  readonly normalizeAccount?: (account: string) => string
  readonly capFor?: (cls: InstrumentClass) => Rate
  readonly encode: (rate: PublishedRate, builderRef: string | null, type: OrderType, tif: TimeInForce) => FeeField
  readonly now?: () => number
}

function refused(reduceOnly: boolean, code: 'fee_approval_missing' | 'fee_above_cap' | 'fee_code_missing'): FeeField {
  if (reduceOnly) return { kind: 'none', reason: 'close_without_approval' }
  throw new AppError(code)
}

/** Shared SDK policy. The venue supplies the field encoder and calls refresh before signing. */
export function directFees(options: DirectFeeOptions): DirectModule & { refresh(): Promise<DirectStart> } {
  const now = options.now ?? Date.now
  const normalize = options.normalizeAccount ?? ((reference: string) => reference)
  let context: DirectContext | null = null
  let published: DirectStart | null = null
  let pending: { context: DirectContext; result: Promise<DirectStart> } | null = null

  async function read(ctx: DirectContext): Promise<DirectStart> {
    const accounts = await ctx.client.accounts.list()
    if (!accounts.ok) throw new AppError('account_not_bound')
    const matches = accounts.data.filter(
      (account) =>
        account.venue === options.venue && normalize(account.subAccountRef ?? account.accountRef) === ctx.accountRef,
    )
    const account = matches.length === 1 ? matches[0] : undefined
    if (account === undefined) throw new AppError('account_not_bound')
    const answer = await ctx.client.fees.schedule({ venue: options.venue, accountId: account.accountId })
    if (!answer.ok) throw new AppError('unavailable')
    const schedules = answer.data.filter(({ venue }) => venue === options.venue)
    const schedule = schedules.length === 1 ? schedules[0] : undefined
    if (schedule === undefined) throw new AppError('fee_code_missing')
    const at = now()
    const validUntil = parseInstant(schedule.validUntil)
    const step = dec.parse(schedule.step)
    if (validUntil <= at || step < 0n) throw new AppError('fee_code_missing')
    const rates: PublishedRate[] = []
    const classCaps: Partial<Record<InstrumentClass, Rate>> = {}
    const keys = new Set<string>()
    for (const row of schedule.rates) {
      if (row.originClass !== 'algo' || (row.liquidity !== 'maker' && row.liquidity !== 'taker')) continue
      const key = `${row.cls}/${row.liquidity}`
      if (keys.has(key) || parseInstant(row.validFrom) > at || parseInstant(row.validUntil) < validUntil) {
        throw new AppError('fee_code_missing')
      }
      keys.add(key)
      const rate = dec.parse(row.rate) as Rate
      if (rate < 0n || (step > 0n && rate % step !== 0n)) throw new AppError('fee_code_missing')
      rates.push(Object.freeze({ cls: row.cls, liquidity: row.liquidity, rate, encoded: row.encodedRate }))
      const raw = schedule.classCaps[row.cls]
      if (raw === undefined) continue
      const cap = dec.parse(raw) as Rate
      const local = options.capFor?.(row.cls)
      classCaps[row.cls] = (local === undefined ? cap : dec.min(local, cap)) as Rate
    }
    const approval = schedule.approval
    const approvedCap =
      approval?.state === 'approved' && approval.cap !== null ? (dec.parse(approval.cap) as Rate) : null
    return Object.freeze({
      venue: options.venue,
      approvedCap,
      builderRef: schedule.builderRef,
      validUntil,
      rates: Object.freeze(rates),
      classCaps: Object.freeze(classCaps),
    })
  }

  function refresh(): Promise<DirectStart> {
    const ctx = context
    if (ctx === null) return Promise.reject(new AppError('fee_code_missing'))
    if (published !== null && published.validUntil > now()) return Promise.resolve(published)
    if (pending?.context === ctx) return pending.result
    const result = read(ctx)
      .then((snapshot) => {
        if (context === ctx) published = snapshot
        return snapshot
      })
      .finally(() => {
        if (pending?.context === ctx) pending = null
      })
    pending = { context: ctx, result }
    return result
  }

  return {
    venue: options.venue,
    start(ctx) {
      context = { ...ctx, accountRef: normalize(ctx.accountRef) }
      published = null
      return refresh()
    },
    refresh,
    feeFields(cls, type, tif, reduceOnly) {
      const current = published
      if (current === null || current.validUntil <= now()) return refused(reduceOnly, 'fee_code_missing')
      if (options.requiresApproval && (current.approvedCap === null || current.approvedCap <= 0n)) {
        return refused(reduceOnly, 'fee_approval_missing')
      }
      const liquidity = type === 'limit' && tif === 'post_only' ? 'maker' : 'taker'
      const rate = current.rates.find((row) => row.cls === cls && row.liquidity === liquidity)
      const cap = current.classCaps[cls]
      if (rate === undefined || cap === undefined) return refused(reduceOnly, 'fee_code_missing')
      if (rate.rate > cap || (current.approvedCap !== null && rate.rate > current.approvedCap)) {
        return refused(reduceOnly, 'fee_above_cap')
      }
      let field: FeeField
      try {
        field = options.encode(rate, current.builderRef, type, tif)
      } catch (error) {
        if (reduceOnly) return refused(true, 'fee_code_missing')
        throw error
      }
      if ('rate' in field && (field.rate < 0n || field.rate > rate.rate)) return refused(reduceOnly, 'fee_above_cap')
      return field
    },
  }
}
