// Direct OKX tag injection (06 CEX §1.5, §8; CEX-102). The caller keeps its own OKX client and signing.
import type { FeeField, OrderType, TimeInForce } from '@atr/contract'
import { AppError, type InstrumentClass, parseInstant } from '@atr/core'
import type { DirectContext, DirectModule, DirectStart } from './template.ts'

const TAG = /^[A-Za-z0-9]{1,16}$/
const NOTICE = 'OKX tag replaced by the platform tag; move your own label to clOrdId'

/** REST paths whose request body carries `tag`. close-position is tagged here although the gateway never calls it (X10). */
export const OKX_TAGGED_PATHS: ReadonlySet<string> = new Set([
  '/api/v5/trade/order',
  '/api/v5/trade/batch-orders',
  '/api/v5/trade/close-position',
  '/api/v5/trade/order-algo',
  '/api/v5/tradingBot/grid/order-algo',
  '/api/v5/tradingBot/recurring/order-algo',
  '/api/v5/sprd/order',
  '/api/v5/rfq/create-rfq',
  '/api/v5/rfq/create-quote',
])
const WS_TAGGED_OPS: ReadonlySet<string> = new Set(['order', 'batch-orders', 'sprd-order'])

export interface OkxDirectOptions {
  /** Default true: a bot's own `tag` is replaced by ours. */
  readonly overrideExistingTag?: boolean
  /** Default console.warn. Called once per replaced tag. */
  readonly onNotice?: (message: string) => void
  readonly now?: () => number
}

type Body = Readonly<Record<string, unknown>>

export class OkxDirect implements DirectModule {
  readonly venue = 'okx' as const
  private readonly override: boolean
  private readonly notice: (message: string) => void
  private readonly now: () => number
  private context: DirectContext | null = null
  private published: DirectStart | null = null
  private code: string | null = null

  constructor(options: OkxDirectOptions = {}) {
    this.override = options.overrideExistingTag ?? true
    this.notice = options.onNotice ?? ((message) => console.warn(message))
    this.now = options.now ?? Date.now
  }

  async start(ctx: DirectContext): Promise<DirectStart> {
    this.context = ctx
    this.published = null
    return this.refresh()
  }

  /** Rereads GET /v1/fees once the published snapshot has expired. */
  async refresh(): Promise<DirectStart> {
    const ctx = this.context
    if (ctx === null) throw new AppError('fee_code_missing')
    if (this.published !== null && this.published.validUntil > this.now()) return this.published
    const accounts = await ctx.client.accounts.list()
    if (!accounts.ok) throw new AppError('account_not_bound')
    const matches = accounts.data.filter(
      (account) => account.venue === 'okx' && (account.subAccountRef ?? account.accountRef) === ctx.accountRef,
    )
    const account = matches.length === 1 ? matches[0] : undefined
    if (account === undefined) throw new AppError('account_not_bound')
    const answer = await ctx.client.fees.schedule({ venue: 'okx', accountId: account.accountId })
    if (!answer.ok) throw new AppError('unavailable')
    const schedules = answer.data.filter(({ venue }) => venue === 'okx')
    const schedule = schedules.length === 1 ? schedules[0] : undefined
    if (schedule === undefined) throw new AppError('fee_code_missing')
    const validUntil = parseInstant(schedule.validUntil)
    if (validUntil <= this.now()) throw new AppError('fee_code_missing')
    const code = schedule.builderRef === null || schedule.builderRef === '' ? null : schedule.builderRef
    if (code !== null && !TAG.test(code)) throw new AppError('fee_code_missing')
    this.code = code
    // OKX pays a program share; no rate of ours exists, so no rate, approval or class cap applies (06 §1.6).
    this.published = Object.freeze({
      venue: 'okx' as const,
      rates: Object.freeze([]),
      approvedCap: null,
      classCaps: Object.freeze({}) as Readonly<Partial<Record<InstrumentClass, never>>>,
      builderRef: code,
      validUntil,
    })
    return this.published
  }

  /** The tag, or null while the program code is not live (nothing is sent then). */
  tag(): string | null {
    return this.code
  }

  feeFields(_cls: InstrumentClass, _type: OrderType, _tif: TimeInForce, _reduceOnly: boolean): FeeField {
    return this.code === null
      ? { kind: 'none', reason: 'venue_has_no_fee_path' }
      : { kind: 'broker_tag', tag: this.code }
  }

  /** One order, or a batch item. Returns a copy; every other field is untouched. */
  tagOrder<T extends Body>(order: T): T {
    const code = this.code
    if (code === null) return order
    const existing = order.tag
    if (existing !== undefined && existing !== null && existing !== '' && existing !== code) {
      if (!this.override) return order
      this.notice(NOTICE)
    }
    return { ...order, tag: code }
  }

  /** Tags the JSON body for a REST path; other paths and query strings pass through unchanged. */
  tagRestBody(path: string, body: Body | readonly Body[]): Body | readonly Body[] {
    if (!OKX_TAGGED_PATHS.has(path.split('?')[0] ?? path)) return body
    return Array.isArray(body) ? body.map((item: Body) => this.tagOrder(item)) : this.tagOrder(body as Body)
  }

  /** Tags the args of a WebSocket `order`, `batch-orders` or `sprd-order` frame. */
  tagWsFrame<T extends { readonly op: string; readonly args?: readonly Body[] }>(frame: T): T {
    if (!WS_TAGGED_OPS.has(frame.op) || frame.args === undefined) return frame
    return { ...frame, args: frame.args.map((arg) => this.tagOrder(arg)) }
  }
}
