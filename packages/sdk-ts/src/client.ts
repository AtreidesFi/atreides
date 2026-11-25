import {
  type BodyOf,
  type DataOf,
  OPERATIONS,
  type OperationId,
  type paths,
  type QueryOf,
  REFUSALS,
  type RefusalOf,
} from '@atr/api-types'
import { clientOrderId, type Dec } from '@atr/core'
import openApi, { type Client as OpenApiClient } from 'openapi-fetch'
import { type ApiKeyFile, boundKey } from './keys/index.ts'
import { openReplay, type ReplayHandle } from './replay.ts'
import {
  executeRoute,
  type PlaceLeg,
  type RouteAnswer,
  type RouteExecuteInput,
  type RouteInput,
  type RoutePlaceInput,
} from './route-execution.ts'
import { browserSocket, type SocketFactory, type StreamHandle, type StreamOptions, StreamSession } from './stream.ts'
import {
  ApiError,
  encodeBody,
  type Key,
  object,
  operationId,
  orderId,
  responseObject,
  Transport,
  type TransportOptions,
} from './transport.ts'

type Detail = string | number | boolean | null
type Money = Dec | string
type PageItem<O extends OperationId> = DataOf<O> extends readonly (infer Item)[] ? Item : never
export type RawClient = OpenApiClient<paths>

export interface ClientOptions extends Omit<TransportOptions, 'key'> {
  readonly key?: Key | ApiKeyFile
  readonly socket?: SocketFactory
}

export type Answer<O extends OperationId> =
  | { readonly ok: true; readonly data: DataOf<O>; readonly nextCursor?: string | null; readonly requestId: string }
  | {
      readonly ok: false
      readonly code: RefusalOf<O> | 'unknown'
      readonly message: string
      readonly retryable: boolean
      readonly details?: Readonly<Record<string, Detail>>
      readonly requestId: string
    }

type PlaceRelay = Extract<BodyOf<'placeOrder'>, { readonly relay: unknown }>
type CancelRelay = BodyOf<'cancelOrder'> & { readonly signed: NonNullable<BodyOf<'cancelOrder'>['signed']> }
type ModifyRelay = Omit<BodyOf<'modifyOrder'>, 'price' | 'size'> & {
  readonly signed: NonNullable<BodyOf<'modifyOrder'>['signed']>
  readonly price?: Money
  readonly size?: Money
}

export interface OrderTriggerInput {
  readonly triggerPrice: Money
  readonly limitPrice?: Money
}

export interface PlaceOrderInput {
  readonly accountId: string
  readonly instrumentId: string
  readonly side: 'buy' | 'sell'
  readonly type: 'limit' | 'market'
  readonly price?: Money
  readonly size?: Money
  readonly notional?: Money
  readonly reduceOnly?: boolean
  readonly tif?: 'gtc' | 'ioc' | 'fok' | 'post_only' | 'gtd'
  readonly goodTil?: string
  readonly tp?: OrderTriggerInput
  readonly sl?: OrderTriggerInput
  readonly slippageBps?: number
  readonly clientOrderId?: string
  readonly strategyRunId?: string
  readonly routeId?: string
}

export interface WalletProof {
  readonly signature: string
  readonly scheme: 'eip191' | 'solana' | 'adr36'
}

export interface KeepAliveHandle {
  readonly lastError: () => ApiError | null
  stop(): Promise<void>
}

interface Arguments {
  readonly path?: Readonly<Record<string, string>>
  readonly query?: object | undefined
  readonly body?: unknown
}

function requestUrl(origin: string, path: string, args: Arguments): string {
  const target = path.replace(/\{([a-zA-Z0-9]+)\}/g, (_match, name: string) => {
    const value = args.path?.[name]
    if (value === undefined) throw new TypeError(`Missing path parameter ${name}`)
    return encodeURIComponent(value)
  })
  const params = new URLSearchParams()
  for (const [name, value] of Object.entries(args.query ?? {})) {
    const values = Array.isArray(value) ? value : [value]
    for (const part of values) if (part != null) params.append(name, String(part))
  }
  return `${origin}${target}${params.size === 0 ? '' : `?${params}`}`
}

function details(value: unknown): Record<string, Detail> | undefined {
  const row = object(value)
  if (row === null) return undefined
  const filtered: Record<string, Detail> = {}
  for (const [name, part] of Object.entries(row)) {
    if (part === null || typeof part === 'string' || typeof part === 'number' || typeof part === 'boolean') {
      filtered[name] = part
    }
  }
  return filtered
}

export async function readAnswer<O extends OperationId>(response: Response, id: O): Promise<Answer<O>> {
  const row = await responseObject(response)
  const requestId =
    response.headers.get('X-Request-Id') ??
    response.headers.get('X-Atr-Request-Id') ??
    (typeof row?.requestId === 'string' ? row.requestId : '')
  if (
    response.ok &&
    row?.ok === true &&
    'data' in row &&
    (row.nextCursor === undefined || row.nextCursor === null || typeof row.nextCursor === 'string')
  ) {
    return {
      ok: true,
      data: row.data as DataOf<O>,
      requestId,
      ...(row.nextCursor === undefined ? {} : { nextCursor: row.nextCursor }),
    }
  }
  if (
    row?.ok === false &&
    typeof row.code === 'string' &&
    typeof row.message === 'string' &&
    typeof row.retryable === 'boolean'
  ) {
    const known = (REFUSALS as Partial<Record<OperationId, readonly string[]>>)[id]?.includes(row.code) === true
    const refusalDetails = details(row.details)
    return {
      ok: false,
      code: known ? (row.code as RefusalOf<O>) : 'unknown',
      message: known ? row.message : 'The API returned an unknown refusal',
      retryable: known && row.retryable,
      requestId,
      ...(known && refusalDetails !== undefined ? { details: refusalDetails } : {}),
    }
  }
  throw new ApiError('Malformed API response', { status: response.status, requestId })
}

function refusalError<O extends OperationId>(row: Extract<Answer<O>, { ok: false }>): ApiError {
  return new ApiError(row.message, { code: row.code, requestId: row.requestId })
}

function triggers<T extends { readonly tp?: OrderTriggerInput; readonly sl?: OrderTriggerInput }>(input: T) {
  const { tp, sl, ...body } = input
  return {
    ...body,
    ...(tp === undefined ? {} : { tp: { ...tp, kind: 'tp', limitPrice: tp.limitPrice ?? null } }),
    ...(sl === undefined ? {} : { sl: { ...sl, kind: 'sl', limitPrice: sl.limitPrice ?? null } }),
  }
}

export const orderTriggerBody = triggers

export function createClient(options: ClientOptions) {
  const { key: suppliedKey, ...rest } = options
  const key = suppliedKey !== undefined && 'v' in suppliedKey ? boundKey(options.baseUrl, suppliedKey) : suppliedKey
  const transport = new Transport({ ...rest, ...(key === undefined ? {} : { key }) })
  const requests = new WeakMap<Request, OperationId>()
  const raw: RawClient = openApi<paths>({
    baseUrl: transport.origin,
    credentials: 'same-origin',
    bodySerializer: encodeBody,
    fetch: (request) => {
      const id = requests.get(request)
      if (id === undefined) throw new TypeError('The raw request did not match an SDK operation')
      return transport.dispatch(request, id)
    },
  })
  raw.use({
    onRequest({ request, schemaPath }) {
      const id = operationId(request.method, schemaPath)
      if (id === undefined) throw new TypeError('Unknown OpenAPI operation')
      requests.set(request, id)
    },
  })

  async function call<O extends OperationId>(id: O, args: Arguments = {}): Promise<Answer<O>> {
    const { path, method } = OPERATIONS[id]
    const body = args.body === undefined ? null : encodeBody(args.body)
    const request = new Request(requestUrl(transport.origin, path, args), {
      method,
      ...(body === null ? {} : { body, headers: { 'Content-Type': 'application/json' } }),
    })
    return readAnswer(await transport.dispatch(request, id), id)
  }

  // The single-order SDK methods keep their original response type as the shared endpoint adds a routed member.
  async function placeSingle(body: PlaceRelay | (ReturnType<typeof triggers> & { readonly clientOrderId: string })) {
    const reply = await call('placeOrder', { body })
    if (!reply.ok) return reply
    if ('clientRouteId' in reply.data)
      throw new ApiError('A single-order request returned a route', { requestId: reply.requestId })
    return { ...reply, data: reply.data }
  }

  async function* pages<O extends 'listInstruments' | 'listOrders' | 'listFills' | 'listRoutes'>(
    id: O,
    query: QueryOf<O> | undefined,
  ): AsyncGenerator<PageItem<O>> {
    const current = { ...query } as { cursor?: string | null }
    const seen = new Set(current.cursor == null ? [] : [current.cursor])
    while (true) {
      const result = await call(id, { query: current })
      if (!result.ok) throw refusalError(result)
      if (!Array.isArray(result.data))
        throw new ApiError('Paginated API data must be a list', { requestId: result.requestId })
      const next = result.nextCursor
      if (next != null && seen.has(next))
        throw new ApiError('API pagination repeated a cursor', { requestId: result.requestId })
      yield* result.data as readonly PageItem<O>[]
      if (next == null) return
      seen.add(next)
      current.cursor = next
    }
  }

  const newRouteId = (supplied?: string) => clientOrderId(supplied ?? orderId(transport.now()))
  const getRoute = (routeId: string) => call('getRoute', { path: { routeId } })
  const quoteRoute = (input: RouteInput | { readonly routeId: string }) =>
    call('quoteRoute', { body: 'routeId' in input ? input : triggers(input) })
  async function registerRoute(input: RoutePlaceInput, replan?: number): Promise<RouteAnswer> {
    const reply = await call('placeOrder', { body: { ...triggers(input), route: 'best', replan } })
    if (!reply.ok) return reply
    if (!('clientRouteId' in reply.data))
      throw new ApiError('A routed request returned a single order', { requestId: reply.requestId })
    if (reply.data.clientRouteId !== input.clientRouteId)
      throw new ApiError('The route response has a different client route ID', { requestId: reply.requestId })
    return { ...reply, data: reply.data }
  }

  let currentStream: StreamSession | null = null
  function stream(settings: StreamOptions): StreamHandle {
    if (transport.signed && options.socket === undefined) {
      throw new ApiError('An API-key stream needs a WebSocket implementation that supports upgrade headers')
    }
    const next = new StreamSession(transport, settings, options.socket ?? browserSocket)
    void currentStream?.close()
    currentStream = next
    return next
  }

  function deadmanArm(accountId: string, timeoutMs: number) {
    if (!Number.isInteger(timeoutMs) || timeoutMs < 5000 || timeoutMs > 600000) {
      throw new RangeError('Dead-man timeout must be from 5000 to 600000 milliseconds')
    }
    return call('setDeadMan', { body: { accountId, timeoutMs } })
  }

  function keepAlive(accountIds: readonly string[], timeoutMs: number): KeepAliveHandle {
    if (!Number.isInteger(timeoutMs) || timeoutMs < 5000 || timeoutMs > 600000) {
      throw new RangeError('Dead-man timeout must be from 5000 to 600000 milliseconds')
    }
    const accounts = [...new Set(accountIds)]
    if (accounts.length === 0 || accounts.some((id) => id === '')) throw new TypeError('A heartbeat needs account IDs')
    let failure: ApiError | null = null
    let stopped = false
    let timer: ReturnType<typeof setTimeout> | undefined
    let pending: Promise<void>
    let armed = false

    const refresh = async () => {
      const ack =
        armed && currentStream !== null
          ? await currentStream.heartbeat(accounts, Math.min(2000, timeoutMs / 6))
          : new Set<string>()
      failure = null
      for (const accountId of accounts) {
        if (stopped) return
        if (ack.has(accountId)) continue
        try {
          const result = await deadmanArm(accountId, timeoutMs)
          if (!result.ok) failure = refusalError(result)
        } catch (error) {
          failure = error instanceof ApiError ? error : new ApiError('Dead-man refresh failed', { cause: error })
        }
      }
      armed = failure === null
      if (!stopped)
        timer = setTimeout(() => {
          pending = refresh()
        }, timeoutMs / 3)
    }
    pending = refresh()
    return {
      lastError: () => failure,
      async stop() {
        stopped = true
        clearTimeout(timer)
        await pending
      },
    }
  }

  /**
   * `hopMs` measures the round trip of a signed API read. `venueAckMs` measures API arrival to venue acknowledgment
   * from execution reports for the bot's key. With fewer than 20 orders the second number is null with `tooFewOrders`.
   */
  async function botLatency(botId: string, options: { readonly samples?: number; readonly hours?: number } = {}) {
    const samples = options.samples ?? 20
    if (!Number.isInteger(samples) || samples < 1 || samples > 200) throw new RangeError('samples is 1 to 200')
    const bot = await call('getBot', { path: { botId } })
    if (!bot.ok) throw refusalError(bot)
    const took: number[] = []
    for (let i = 0; i < samples; i += 1) {
      const start = performance.now()
      const answer = await call('whoami')
      if (!answer.ok) throw refusalError(answer)
      took.push(performance.now() - start)
    }
    took.sort((a, b) => a - b)
    const at = (q: number) =>
      Math.round((took[Math.min(took.length - 1, Math.ceil(q * took.length) - 1)] ?? 0) * 10) / 10
    let venueAckMs: { p50: number | null; p95: number | null; orders: number } | null = null
    if (bot.data.keyId !== null) {
      const to = transport.now()
      const stats = await call('getExecutionStats', {
        query: {
          from: new Date(to - (options.hours ?? 24) * 3_600_000).toISOString(),
          to: new Date(to).toISOString(),
          groupBy: 'key',
          keyId: bot.data.keyId,
        } as never,
      })
      if (stats.ok) {
        const rows = ((stats.data as { rows?: readonly unknown[] }).rows ?? []) as readonly {
          orders: number
          report?: { msToAck?: { p50: number | null; p95: number | null; count: number } }
        }[]
        const ack = rows[0]?.report?.msToAck
        venueAckMs = ack === undefined ? null : { p50: ack.p50, p95: ack.p95, orders: ack.count }
      }
    }
    const measured = venueAckMs !== null && venueAckMs.orders >= 20
    return {
      hopMs: { p50: at(0.5), p95: at(0.95), samples },
      venueAckMs: measured ? venueAckMs : null,
      tooFewOrders: !measured,
    }
  }

  return {
    raw,
    request: call,
    venues: {
      list: () => call('listVenues'),
      get: (venue: string) => call('getVenue', { path: { venue } }),
      budget: (venue: string, accountId: string) => call('getVenueBudget', { path: { venue }, query: { accountId } }),
    },
    instruments: {
      list: (query?: QueryOf<'listInstruments'>) => pages('listInstruments', query),
      get: (instrumentId: string) => call('getInstrument', { path: { instrumentId } }),
    },
    fees: {
      schedule: (query?: QueryOf<'getFees'>) => call('getFees', { query }),
      quote: (body: BodyOf<'quoteFee'>) => call('quoteFee', { body }),
    },
    accounts: {
      list: () => call('listAccounts'),
      async link(venue: string, accountRef: string, sign: (message: string) => Promise<WalletProof>) {
        const fields = { venue, accountRef }
        const challenge = await call('createAccountLinkChallenge', { body: fields })
        if (!challenge.ok) return challenge
        const message = challenge.data.message
        return call('linkAccount', { body: { ...fields, message, ...(await sign(message)) } })
      },
    },
    orders: {
      place(input: PlaceOrderInput) {
        const id = clientOrderId(input.clientOrderId ?? orderId(transport.now()))
        const result = placeSingle({ ...triggers(input), clientOrderId: id })
        return { clientOrderId: id, result }
      },
      get: (accountId: string, id: string) => call('getOrder', { path: { clientOrderId: id }, query: { accountId } }),
      list: (query?: QueryOf<'listOrders'>) => pages('listOrders', query),
      submitTx: (accountId: string, id: string, payload: BodyOf<'submitOrderTx'>['payload']) =>
        call('submitOrderTx', { path: { clientOrderId: id }, body: { accountId, payload } }),
      cancel: (accountId: string, id: string) =>
        call('cancelOrder', { path: { clientOrderId: id }, body: { accountId } }),
      modify: (accountId: string, id: string, change: { readonly price?: Money; readonly size?: Money }) =>
        call('modifyOrder', { path: { clientOrderId: id }, body: { accountId, ...change } }),
      cancelAll: (accountId: string, instrumentId?: string) => call('cancelAll', { body: { accountId, instrumentId } }),
    },
    route: {
      quote: quoteRoute,
      /** An indicative quote over live books with no account: nothing in the answer can be executed. */
      preview: (body: BodyOf<'previewRoute'>) => call('previewRoute', { body }),
      place(input: RoutePlaceInput) {
        const clientRouteId = newRouteId(input.clientRouteId)
        return { clientRouteId, result: registerRoute({ ...input, clientRouteId }) }
      },
      execute(input: RouteExecuteInput, placeLeg?: PlaceLeg) {
        const clientRouteId = newRouteId(input.clientRouteId)
        const result = executeRoute(
          { ...input, clientRouteId },
          {
            get: getRoute,
            quote: quoteRoute,
            register: registerRoute,
            newId: newRouteId,
            ...(placeLeg === undefined ? {} : { placeLeg }),
          },
        )
        return { clientRouteId, result }
      },
      get: getRoute,
      list: (query?: QueryOf<'listRoutes'>) => pages('listRoutes', query),
      cancel: (routeId: string) => call('cancelRoute', { path: { routeId } }),
    },
    relay: {
      clientOrderId: (supplied?: string) => clientOrderId(supplied ?? orderId(transport.now())),
      place: (body: PlaceRelay) => placeSingle(body),
      cancel: (id: string, body: CancelRelay) => call('cancelOrder', { path: { clientOrderId: id }, body }),
      modify: (id: string, body: ModifyRelay) => call('modifyOrder', { path: { clientOrderId: id }, body }),
    },
    deadman: {
      arm: deadmanArm,
      clear: (accountId: string) => call('setDeadMan', { body: { accountId, timeoutMs: 0 } }),
      keepAlive,
    },
    bots: {
      templates: () => call('listBotTemplates'),
      list: () => call('listBots'),
      get: (botId: string) => call('getBot', { path: { botId } }),
      start: (botId: string) => call('startBot', { path: { botId }, body: {} }),
      stop: (botId: string) => call('stopBot', { path: { botId }, body: {} }),
      restart: (botId: string) => call('restartBot', { path: { botId }, body: {} }),
      logs: (botId: string, query?: QueryOf<'getBotLogs'>) => call('getBotLogs', { path: { botId }, query }),
      latency: (botId: string, options?: { readonly samples?: number; readonly hours?: number }) =>
        botLatency(botId, options),
    },
    positions: (query?: QueryOf<'listPositions'>) => call('listPositions', { query }),
    balances: (query?: QueryOf<'listBalances'>) => call('listBalances', { query }),
    fills: (query?: QueryOf<'listFills'>) => pages('listFills', query),
    equity: (query?: QueryOf<'getEquity'>) => call('getEquity', { query }),
    strategy: {
      replay(input: Parameters<typeof openReplay>[2], onFrame: Parameters<typeof openReplay>[3]): ReplayHandle {
        if (transport.signed && options.socket === undefined)
          throw new ApiError('A signed replay needs a WebSocket implementation that supports upgrade headers')
        return openReplay(transport, options.socket ?? browserSocket, input, onFrame)
      },
    },
    stream,
  }
}

export type Client = ReturnType<typeof createClient>
