import { setTimeout as delay } from 'node:timers/promises'
import type { DataOf, OperationId } from '@atr/api-types'
import { type Clock, type Dec, dec, formatInstant, systemClock, type UnixMs } from '@atr/core'
import {
  type CompiledStrategy,
  compile,
  type Decision,
  ENGINE_VERSION,
  entrySize,
  exitPrices,
  type HoldingView,
  type SignalBar,
  type StrategySpec,
  step,
} from '@atr/strategy'
import type { Answer, Client, PlaceOrderInput } from '../../src/client.ts'
import { ApiError, object } from '../../src/transport.ts'
import { type HoldingState, type PendingOrder, type RunnerState, readState, writeState } from './state.ts'

export type StopPolicy = 'leave_positions' | 'close_positions'
export type Outcome =
  | { readonly state: 'accepted'; readonly venueOrderId: string | null }
  | { readonly state: 'refused'; readonly code: string }
  | { readonly state: 'unknown' }
export interface DirectVenue {
  readonly accountId: string
  deriveVenueClientId(clientOrderId: string): string | null
  place(order: PlaceOrderInput): Promise<Outcome>
  lookup(clientOrderId: string, venueClientId: string | null): Promise<Outcome>
  positions(): Promise<DataOf<'listPositions'>>
}
export type RunnerEvent =
  | { readonly kind: 'decision'; readonly at: number; readonly instrument: string; readonly action: string }
  | { readonly kind: 'order'; readonly clientOrderId: string; readonly outcome: Outcome['state'] }
  | { readonly kind: 'paused'; readonly reason: string }
  | { readonly kind: 'heartbeat_failed'; readonly consecutive: number }
  | { readonly kind: 'stopped'; readonly policy: StopPolicy }
export interface RunnerOptions {
  readonly client: Client
  readonly runId: string
  readonly statePath: string
  readonly direct?: DirectVenue | null
  readonly clock?: Clock
  readonly onEvent?: (event: RunnerEvent) => void
}
export interface Runner {
  readonly mode: 'direct' | 'gateway'
  start(): Promise<void>
  stop(policy: StopPolicy): Promise<void>
}

type Detail = DataOf<'getStrategyRun'>
type Heartbeat = DataOf<'heartbeatStrategyRun'>
type Instrument = DataOf<'getInstrument'>
type Holding = Detail['holdings'][number]
type OutcomeIntent = 'open' | 'add' | 'close' | 'exit_time' | 'exit_rule' | 'exit_tp' | 'exit_sl' | 'stop_close'
type LiveSpec = Extract<StrategySpec, { kind: 'rules' | 'schedule' }>
const frameMs = {
  '1s': 1000,
  '1m': 60_000,
  '5m': 300_000,
  '15m': 900_000,
  '1h': 3_600_000,
  '4h': 14_400_000,
  '1d': 86_400_000,
}
const unknown: Outcome = { state: 'unknown' }

function data<O extends OperationId>(answer: Answer<O>): DataOf<O> {
  if (!answer.ok) throw new ApiError(answer.message, { code: answer.code, requestId: answer.requestId })
  return answer.data
}

type OrderAnswer =
  | { readonly ok: true; readonly data: Pick<DataOf<'getOrder'>, 'state' | 'venueOrderId'> }
  | { readonly ok: false; readonly retryable: boolean; readonly code: string }

/** A strategy never sends `dryRun`; a `DryRunResult` in an order's place is an outcome we cannot read: unknown. */
function orderReply(reply: { readonly ok: boolean }): OrderAnswer {
  const answer = reply as { readonly ok: boolean; readonly data?: object }
  if (answer.ok && answer.data !== undefined && 'wouldPlace' in answer.data)
    return { ok: false, retryable: false, code: 'unknown' }
  return reply as OrderAnswer
}

function outcome(reply: OrderAnswer): Outcome {
  if (!reply.ok) {
    if (reply.retryable || ['unknown_outcome', 'unknown', 'unavailable'].includes(reply.code)) return unknown
    return { state: 'refused', code: reply.code }
  }
  const order = reply.data
  if (order.state === 'unknown' || order.state === 'pending') return unknown
  if (order.state === 'rejected') return { state: 'refused', code: 'venue_rejected' }
  return { state: 'accepted', venueOrderId: order.venueOrderId }
}

interface RunnerBar {
  readonly signal: SignalBar
  readonly reference: Dec
}
function candle(raw: unknown, frame: number): RunnerBar | null {
  const row = object(raw)
  if (
    row === null ||
    row.closed !== true ||
    typeof row.t !== 'number' ||
    !Number.isSafeInteger(row.t) ||
    row.t < 0 ||
    row.t % frame !== 0
  )
    return null
  const values = [row.open, row.high, row.low, row.close, row.volume].map((value) =>
    typeof value === 'string' ? dec.tryParse(value) : null,
  )
  const [open, high, low, close, volume] = values
  if (
    open == null ||
    high == null ||
    low == null ||
    close == null ||
    volume == null ||
    low <= 0n ||
    low > dec.min(open, close) ||
    high < dec.max(open, close) ||
    volume < 0n
  )
    return null
  return {
    reference: close,
    signal: {
      closeAt: (row.t + frame) as UnixMs,
      open: Number(dec.format(open)),
      high: Number(dec.format(high)),
      low: Number(dec.format(low)),
      close: Number(dec.format(close)),
      volume: Number(dec.format(volume)),
      series: {},
      features: {},
      inputAgeMs: {},
    },
  }
}

class LiveRunner implements Runner {
  readonly mode: Runner['mode']
  readonly #client: Client
  readonly #direct: DirectVenue | null
  readonly #account: string
  readonly #path: Readonly<{ id: string }>
  readonly #prefix: string
  readonly #clock: Clock
  readonly #emit: NonNullable<RunnerOptions['onEvent']>
  readonly #frame: number
  readonly #instruments = new Map<string, Instrument>()
  readonly #batches = new Map<number, Map<string, RunnerBar>>()
  readonly #shutdown = new AbortController()
  #active = false
  #heartbeat: Heartbeat | null = null
  #failures = 0
  #period = 15_000
  #status: Detail['run']['status']
  #policy: StopPolicy | null = null
  #localStop = false
  #locked: Promise<void> = Promise.resolve()

  constructor(
    readonly options: RunnerOptions,
    detail: Detail,
    readonly compiled: CompiledStrategy,
    readonly spec: LiveSpec,
    readonly state: RunnerState,
  ) {
    const account = detail.run.venueAccountId
    const mode = detail.run.mode
    this.#direct = options.direct ?? null
    if (account === null || mode === 'paper' || !/^r[a-z2-7]{7}$/u.test(detail.cloidPrefix))
      throw new Error('A supported live run and account are required')
    if (
      (mode === 'direct') !== (this.#direct !== null) ||
      (this.#direct !== null && this.#direct.accountId !== account)
    )
      throw new Error('The direct adapter must match the run account')
    this.mode = mode
    this.#account = account
    this.#prefix = detail.cloidPrefix
    this.#path = { id: options.runId }
    this.#client = options.client
    this.#clock = options.clock ?? systemClock
    this.#emit = options.onEvent ?? (() => undefined)
    this.#frame = frameMs[compiled.timeframe]
    this.#status = detail.run.status
    this.#policy = detail.run.stopPolicy
  }

  #save(): Promise<void> {
    return writeState(this.options.statePath, this.state)
  }

  #exclusive(work: () => Promise<void>): Promise<void> {
    const task = this.#locked.then(work)
    this.#locked = task.catch(() => undefined)
    return task
  }

  async #detail(): Promise<Detail> {
    return data(await this.#client.request('getStrategyRun', { path: this.#path }))
  }

  async #pulse(): Promise<void> {
    try {
      const detail = await this.#detail()
      const result = data(
        await this.#client.request('heartbeatStrategyRun', {
          path: this.#path,
          body: {
            runnerLang: 'ts',
            runnerVersion: ENGINE_VERSION,
            specSha256: this.state.specHash,
            state: this.state.openingPaused ? 'paused_by_runner' : 'running',
            lastDecisionAt: this.state.lastClose === null ? null : formatInstant(this.state.lastClose as UnixMs),
            holdings: detail.holdings.map(({ instrument, side, size }) => ({ instrument, side, size })),
            note: null,
          },
        }),
      )
      this.#heartbeat = result
      this.#failures = 0
      this.#status = result.status
      this.#period = result.nextHeartbeatInMs
      if (!this.#localStop) this.#policy = result.stopPolicy
      if (result.status === 'stopped' || result.status === 'failed') this.#shutdown.abort()
    } catch (error) {
      if (error instanceof ApiError && error.code === 'strategy_run_version_mismatch') throw error
      this.#failures++
      this.#emit({ kind: 'heartbeat_failed', consecutive: this.#failures })
    }
    await this.#exclusive(async () => {
      await this.#reconcile()
      if (this.#policy !== 'close_positions' || this.#shutdown.signal.aborted) return
      let detail: Detail
      try {
        detail = await this.#detail()
      } catch {
        return
      }
      for (const position of detail.holdings) await this.#close(position, 'stop_close', this.#clock.now())
    })
  }

  async #report(pending: PendingOrder, result: Outcome): Promise<boolean> {
    const common = { at: formatInstant(pending.at as UnixMs) }
    const event: import('@atr/api-types').BodyOf<'reportStrategyRunEvents'>['events'][number] =
      result.state === 'accepted'
        ? { ...common, kind: 'order_reported', data: { clientOrderId: pending.id, venueOrderId: result.venueOrderId } }
        : result.state === 'refused'
          ? { ...common, kind: 'order_refused', data: { clientOrderId: pending.id, code: result.code } }
          : { ...common, kind: 'unknown_outcome', data: { clientOrderId: pending.id } }
    if (result.state !== 'accepted' && !pending.reduceOnly) {
      this.state.openingPaused = true
      await this.#save()
      this.#emit({ kind: 'paused', reason: event.kind })
    }
    try {
      const response = await this.#client.request('reportStrategyRunEvents', {
        path: this.#path,
        body: { events: [event] },
      })
      this.#emit({ kind: 'order', clientOrderId: pending.id, outcome: result.state })
      return response.ok && response.data.accepted === 1 && result.state !== 'unknown'
    } catch {
      return false
    }
  }

  async #reconcile(): Promise<void> {
    for (const pending of this.state.pending.slice()) {
      let state = unknown
      try {
        if (this.#direct !== null) state = await this.#direct.lookup(pending.id, pending.nativeId)
        else {
          const matching = await this.#client.orders.get(this.#account, pending.id)
          if (matching.ok) state = outcome(matching)
        }
      } catch {
        continue
      }
      if (state.state !== 'unknown' && (await this.#report(pending, state))) {
        this.state.pending = this.state.pending.filter(({ id }) => id !== pending.id)
        await this.#save()
      }
    }
  }

  async #send(order: PlaceOrderInput, intent: OutcomeIntent, at: UnixMs): Promise<void> {
    if (
      order.reduceOnly !== true &&
      (this.#shutdown.signal.aborted || this.#failures >= 3 || this.#policy !== null || this.state.openingPaused)
    )
      return
    if (this.state.pending.some(({ instrument }) => instrument === order.instrumentId)) return
    const sequence = BigInt(this.state.sequence) + 1n
    const suffix = sequence.toString(36)
    if (suffix.length > 20) throw new Error('Runner order sequence exhausted')
    const id = `${this.#prefix}-${suffix}`
    const nativeId = this.#direct?.deriveVenueClientId(id) ?? null
    this.state.sequence = sequence.toString()
    await this.#save()
    const pending = { id, nativeId, instrument: order.instrumentId, reduceOnly: order.reduceOnly === true, at }
    try {
      const reported = await this.#client.request('reportStrategyRunOrders', {
        path: this.#path,
        body: {
          reports: [
            {
              clientOrderId: id,
              venueClientId: nativeId,
              intent,
              instrument: order.instrumentId,
              side: order.side,
              reduceOnly: pending.reduceOnly,
              decidedAt: formatInstant(at),
            },
          ],
        },
      })
      if (!reported.ok || reported.data.accepted + reported.data.duplicates !== 1) return
    } catch {
      return
    }
    this.state.pending.push(pending)
    await this.#save()
    let result = unknown
    try {
      result =
        this.#direct === null
          ? outcome(
              orderReply(
                await this.#client.orders.place({ ...order, clientOrderId: id, strategyRunId: this.#path.id }).result,
              ),
            )
          : await this.#direct.place({ ...order, clientOrderId: id })
    } catch {
      result = unknown
    }
    if (await this.#report(pending, result)) {
      this.state.pending = this.state.pending.filter((row) => row.id !== id)
      await this.#save()
    }
  }

  async #foreign(detail: Detail): Promise<Set<string> | null> {
    try {
      const rows =
        this.#direct === null
          ? data(await this.#client.positions({ accountId: [this.#account] }))
          : await this.#direct.positions()
      const account = rows.find(({ accountId }) => accountId === this.#account)
      if (account === undefined || !account.read.ok) return null
      const own = new Map(detail.holdings.map((row) => [row.instrument, row]))
      return new Set(
        account.read.value.flatMap(({ instrumentId, side, size }) => {
          const attributed = own.get(instrumentId)
          return attributed !== undefined && attributed.side === side && dec.parse(attributed.size) === dec.parse(size)
            ? []
            : [instrumentId]
        }),
      )
    } catch {
      return null
    }
  }

  #track(holdings: readonly Holding[], bars: ReadonlyMap<string, RunnerBar>): readonly HoldingView[] {
    const tracked: Record<string, HoldingState> = {}
    const output = holdings.map(({ instrument, side, entryPx }) => {
      const prior = this.state.holdings[instrument]
      const current = (prior?.side === side ? prior : undefined) ?? { side, bars: 0, extreme: entryPx }
      const bar = bars.get(instrument)
      const extreme = Number(current.extreme)
      const next =
        bar === undefined
          ? extreme
          : side === 'long'
            ? Math.max(extreme, bar.signal.high)
            : Math.min(extreme, bar.signal.low)
      tracked[instrument] = { side, bars: current.bars + 1, extreme: String(next) }
      return { instrument, side, barsHeld: current.bars + 1, entryClose: Number(entryPx), extremeSinceEntry: next }
    })
    this.state.holdings = tracked
    return output
  }

  #opening(
    detail: Detail,
    decision: Decision,
    bar: RunnerBar,
    limits: { orders: number; positions: number; notional: Dec },
  ) {
    const hb = this.#heartbeat
    const stats = detail.run.stats
    if (
      this.#status !== 'live' ||
      detail.run.status !== 'live' ||
      this.#failures >= 3 ||
      this.state.openingPaused ||
      hb === null ||
      hb.capsState.dailyLossHit ||
      stats === null ||
      !stats.marksFresh
    )
      return null
    const instrument = this.#instruments.get(decision.instrument)
    if (instrument === undefined) return null
    const caps = hb.caps
    const allowed =
      'venue' in caps.instruments
        ? caps.instruments.venue === instrument.venue && caps.instruments.cls === instrument.cls
        : caps.instruments.includes(decision.instrument)
    if (!allowed || limits.orders >= caps.maxOrdersPerDay || limits.positions >= caps.maxOpenPositions) return null
    const execution = this.spec.execution
    if (execution.leverage > caps.maxLeverage || execution.leverage > (instrument.maxLeverage ?? 1)) return null
    const side = ['open_long', 'buy_scheduled'].includes(decision.action) ? 'buy' : 'sell'
    const ref = bar.reference
    const equity = dec.parse(stats.equity)
    if (equity <= 0n || ref <= 0n) return null
    const tick = dec.parse(instrument.tick)
    const only = (raw: string | null) => (raw === null ? null : dec.parse(raw))
    const sizing =
      this.spec.kind === 'rules'
        ? this.spec.sizing
        : { kind: 'fixed_notional' as const, notional: this.spec.schedule.notional }
    const size = entrySize(sizing, execution.leverage, equity, ref, {
      tick,
      lot: dec.parse(instrument.lot),
      minSize: only(instrument.minSize),
      minNotional: only(instrument.minNotional),
    })
    if (size === null) return null
    const isLimit = execution.entry.kind === 'limit_offset'
    let price = ref
    if (execution.entry.kind === 'limit_offset') {
      const offset = dec.parse(execution.entry.offset)
      const factor = side === 'buy' ? dec.sub(dec.one, offset) : dec.add(dec.one, offset)
      price = dec.quantize(
        dec.mul(ref, factor, side === 'buy' ? 'floor' : 'ceil'),
        tick,
        side === 'buy' ? 'floor' : 'ceil',
      )
    }
    const added = dec.mul(size, dec.max(ref, price), 'ceil')
    if (
      price <= 0n ||
      added > dec.parse(caps.maxOrderNotional) ||
      dec.add(limits.notional, added) > dec.parse(caps.maxOpenNotional)
    )
      return null
    const exits = execution.restingExits
      ? exitPrices(side === 'buy' ? 'long' : 'short', price, this.spec.exit, tick)
      : null
    const order: PlaceOrderInput = {
      accountId: this.#account,
      instrumentId: decision.instrument,
      side,
      type: isLimit ? 'limit' : 'market',
      size,
      ...(isLimit ? { price } : {}),
      tif: isLimit ? execution.tif : 'ioc',
      ...(exits?.tp == null ? {} : { tp: { triggerPrice: exits.tp } }),
      ...(exits?.sl == null ? {} : { sl: { triggerPrice: exits.sl } }),
    }
    return { order, added }
  }

  #close(position: Holding, intent: OutcomeIntent, at: UnixMs): Promise<void> {
    return this.#send(
      {
        accountId: this.#account,
        instrumentId: position.instrument,
        side: position.side === 'long' ? 'sell' : 'buy',
        type: 'market',
        size: position.size,
        tif: 'ioc',
        reduceOnly: true,
      },
      intent,
      at,
    )
  }

  async #decision(at: UnixMs, bars: Map<string, RunnerBar>): Promise<void> {
    if (this.state.lastClose !== null && at <= this.state.lastClose) return
    let detail: Detail
    try {
      detail = await this.#detail()
    } catch {
      return
    }
    const foreign = await this.#foreign(detail)
    const holdings = this.#track(detail.holdings, bars)
    const own = new Map(detail.holdings.map((hold) => [hold.instrument, hold]))
    const inputForeign = foreign ?? new Set(this.compiled.instruments.filter((ref) => !own.has(ref)))
    const result = step(this.compiled, this.state.engine, {
      holdings,
      foreign: inputForeign,
      candidates: null,
      closes: new Map([...bars].map(([instrument, bar]) => [instrument, bar.signal])),
    })
    this.state.engine = result.state
    this.state.lastClose = at
    await this.#save()
    if (this.#policy !== null) return
    const limits = {
      orders: this.#heartbeat?.capsState.ordersToday ?? 0,
      positions: Math.max(own.size, detail.run.stats?.openPositions ?? 0),
      notional: dec.parse(this.#heartbeat?.capsState.openNotional ?? '0'),
    }
    for (const decision of result.decisions) {
      const { instrument, action } = decision
      this.#emit({ kind: 'decision', at, instrument, action })
      const holding = own.get(instrument)
      if (action === 'close') {
        if (holding !== undefined)
          await this.#close(
            holding,
            decision.reason === 'time' ? 'exit_time' : decision.reason === 'rule' ? 'exit_rule' : 'close',
            at,
          )
        continue
      }
      if (foreign === null || foreign.has(instrument)) continue
      const bar = bars.get(instrument)
      if (bar === undefined) continue
      const ready = this.#opening(detail, decision, bar, limits)
      if (ready === null) continue
      await this.#send(ready.order, holding === undefined ? 'open' : 'add', at)
      limits.orders++
      limits.positions += Number(holding === undefined)
      limits.notional = dec.add(limits.notional, ready.added)
    }
  }

  #market(channel: string, value: unknown): void {
    const name = channel.slice('candles:'.length, -(this.compiled.timeframe.length + 1))
    if (channel !== `candles:${name}:${this.compiled.timeframe}` || !this.#instruments.has(name)) return
    const bar = candle(value, this.#frame)
    if (bar === null) return
    const at = bar.signal.closeAt
    if (at > this.#clock.now() + 1000 || (this.state.lastClose !== null && at <= this.state.lastClose)) return
    if (this.#batches.size >= 4 && !this.#batches.has(at)) this.#batches.delete(Math.min(...this.#batches.keys()))
    const rows = this.#batches.get(at) ?? new Map<string, RunnerBar>()
    rows.set(name, bar)
    this.#batches.set(at, rows)
  }

  async #decisions(signal: AbortSignal): Promise<void> {
    while (!signal.aborted) {
      const at = Math.min(...this.#batches.keys()) as UnixMs
      const bars = this.#batches.get(at)
      if (bars !== undefined && (bars.size === this.#instruments.size || this.#clock.now() >= at + 10_000)) {
        this.#batches.delete(at)
        await this.#exclusive(async () => this.#decision(at, bars))
      } else await delay(200, undefined, { signal })
    }
  }

  async #heartbeats(signal: AbortSignal): Promise<void> {
    while (!signal.aborted) {
      await delay(this.#period, undefined, { signal })
      await this.#pulse()
    }
  }

  async start(): Promise<void> {
    if (this.#active) throw new Error('This strategy runner is already active')
    this.#active = true
    const local = new AbortController()
    const signal = AbortSignal.any([this.#shutdown.signal, local.signal])
    let stream: ReturnType<Client['stream']> | null = null
    const loops: Promise<void>[] = []
    try {
      for (const name of this.compiled.instruments)
        this.#instruments.set(name, data(await this.#client.instruments.get(name)))
      await this.#pulse()
      if (signal.aborted) return
      stream = this.#client.stream({
        channels: this.compiled.instruments.map((id) => `candles:${id}:${this.compiled.timeframe}`),
        onEvent: () => undefined,
        onReset: () => undefined,
        onMarket: (channel, value) => this.#market(channel, value),
      })
      loops.push(this.#heartbeats(signal), this.#decisions(signal), stream.done)
      await Promise.race(loops)
    } catch (error) {
      if (!signal.aborted) throw error
    } finally {
      local.abort()
      await stream?.close()
      await Promise.allSettled(loops)
      this.#active = false
    }
  }

  async stop(policy: StopPolicy): Promise<void> {
    const result = data(await this.#client.request('stopStrategyRun', { path: this.#path, body: { policy } }))
    this.#localStop = true
    this.#policy = policy
    this.#status = result.status
    this.#emit({ kind: 'stopped', policy })
    if (this.#status === 'stopped' || policy === 'leave_positions') this.#shutdown.abort()
  }
}

export async function runner(options: RunnerOptions): Promise<Runner> {
  const { client, runId } = options
  const detail = data(await client.request('getStrategyRun', { path: { id: runId } }))
  if (detail.run.id !== runId) throw new Error('The API returned a different strategy run')
  const version = data(await client.request('getStrategyVersion', { path: { id: detail.run.versionId } }))
  const spec = version.spec as StrategySpec | undefined
  if (version.id !== detail.run.versionId || version.engineVersion !== ENGINE_VERSION || spec === undefined)
    throw new ApiError('Strategy version is unavailable', { code: 'strategy_run_version_mismatch' })
  if ((spec.kind !== 'rules' && spec.kind !== 'schedule') || spec.timeframe === '1s')
    throw new Error('This runner requires a rules or schedule version with a live candle timeframe')
  const compiled = compile(spec)
  if (Buffer.from(compiled.sha256).toString('hex') !== version.specSha256)
    throw new ApiError('Strategy version hash differs', { code: 'strategy_run_version_mismatch' })
  return new LiveRunner(options, detail, compiled, spec, await readState(options.statePath, runId, compiled))
}
