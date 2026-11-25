import { type BodyOf, type DataOf, OPERATIONS, type QueryOf } from '@atr/api-types'
import { clientOrderId, type Dec, isId, newId, VENUE_IDS, type VenueId } from '@atr/core'
import { type Answer, type KeepAliveHandle, orderTriggerBody, type PlaceOrderInput, readAnswer } from './client.ts'
import { ApiError, apiOrigin, encodeBody, orderId } from './transport.ts'

const routes = {
  placeOrder: '/internal/orders',
  cancelOrder: '/internal/cancel',
  modifyOrder: '/internal/modify',
  cancelAll: '/internal/cancel-all',
  setDeadMan: '/internal/deadman',
} as const

type Mutation = keyof typeof routes
type LocalOperation = Mutation | 'getOrder'
type Money = Dec | string
type LocalOrder = Extract<DataOf<'placeOrder'>, { readonly orderId: string }>

export interface LocalGatewayOptions {
  readonly gatewayUrl: string
  readonly hmacSecret: string | Uint8Array
  readonly userId: string
  readonly venue: VenueId
  readonly fetch?: typeof globalThis.fetch
  readonly now?: () => number
  readonly timeoutMs?: number
}

const encoder = new TextEncoder()
function base64(value: Uint8Array): string {
  let text = ''
  for (const byte of value) text += String.fromCharCode(byte)
  return btoa(text)
}
const hex = (bytes: ArrayBuffer): string =>
  Array.from(new Uint8Array(bytes), (byte) => byte.toString(16).padStart(2, '0')).join('')

function clientOrderTarget(operation: Mutation, id: string | undefined): string {
  return id === undefined
    ? routes[operation]
    : routes[operation] +
        '?' +
        new URLSearchParams({
          originalTarget: OPERATIONS[operation].path.replace('{clientOrderId}', encodeURIComponent(id)),
        })
}

export function createLocalGatewayClient(options: LocalGatewayOptions) {
  const origin = apiOrigin(options.gatewayUrl)
  const url = new URL(origin)
  if (!['127.0.0.1', '[::1]', 'localhost'].includes(url.hostname) && url.protocol !== 'https:')
    throw new TypeError('Use HTTPS to reach a remote desk gateway')
  if (!VENUE_IDS.includes(options.venue)) throw new TypeError('Expected a supported desk venue')
  if (!isId(options.userId)) throw new TypeError('Expected the desk user ID')
  const bytes = typeof options.hmacSecret === 'string' ? encoder.encode(options.hmacSecret) : options.hmacSecret
  if (bytes.byteLength === 0) throw new TypeError('The desk HMAC secret is required')
  const key = crypto.subtle.importKey('raw', new Uint8Array(bytes), { name: 'HMAC', hash: 'SHA-256' }, false, ['sign'])
  const now = options.now ?? Date.now
  const send = options.fetch ?? globalThis.fetch
  const timeout = options.timeoutMs ?? 10_000
  if (!Number.isSafeInteger(timeout) || timeout < 1 || timeout > 2_147_483_647)
    throw new RangeError('Invalid desk gateway timeout')
  const { userId, venue } = options

  async function request<O extends LocalOperation>(
    operation: O,
    accountId: string,
    target: string,
    body?: object,
    order?: string,
  ): Promise<Answer<O>> {
    if (!isId(accountId)) throw new TypeError('Expected the desk venue account ID')
    const method = body === undefined ? 'GET' : 'POST'
    const requestId = newId<'request'>()
    const payload =
      body === undefined
        ? undefined
        : JSON.stringify({
            requestId,
            operation,
            userId,
            venueAccountId: accountId,
            venue,
            mode: 'gateway',
            keyPlace: 'signer',
            origin: 'sdk',
            caller: { kind: 'local' },
            rawBody: base64(encoder.encode(encodeBody(body))),
            clientIp: '127.0.0.1',
            fee: null,
            leg: null,
          })
    const timestamp = String(now())
    const digest = hex(await crypto.subtle.digest('SHA-256', encoder.encode(payload ?? '')))
    const signature = hex(
      await crypto.subtle.sign('HMAC', await key, encoder.encode(`${method}\n${target}\n${digest}\n${timestamp}`)),
    )
    let response: Response
    try {
      response = await send(origin + target, {
        method,
        redirect: 'error',
        credentials: 'omit',
        signal: AbortSignal.timeout(timeout),
        headers: {
          'X-Atr-Caller': 'local',
          'X-Atr-Request-Id': requestId,
          'X-Atr-Timestamp': timestamp,
          'X-Atr-Signature': signature,
          ...(payload === undefined ? {} : { 'Content-Type': 'application/json' }),
        },
        ...(payload === undefined ? {} : { body: payload }),
      })
    } catch (cause) {
      throw new ApiError('Desk gateway transport failed', {
        cause,
        requestId,
        ...(order === undefined ? {} : { clientOrderId: order }),
      })
    }
    if (response.status === 401 || response.status === 403) {
      throw new ApiError('Desk gateway authentication failed', { status: response.status, requestId })
    }
    return readAnswer(response, operation)
  }

  const mutate = <O extends Mutation>(operation: O, accountId: string, body: object, order?: string) =>
    request(
      operation,
      accountId,
      clientOrderTarget(operation, operation === 'cancelOrder' || operation === 'modifyOrder' ? order : undefined),
      body,
      order,
    )

  function arm(accountId: string, timeoutMs: number) {
    if (!Number.isInteger(timeoutMs) || timeoutMs < 5000 || timeoutMs > 600000)
      throw new RangeError('Dead-man timeout must be from 5000 to 600000 milliseconds')
    return mutate('setDeadMan', accountId, { accountId, timeoutMs })
  }

  function keepAlive(accountIds: readonly string[], timeoutMs: number): KeepAliveHandle {
    if (!Number.isInteger(timeoutMs) || timeoutMs < 5000 || timeoutMs > 600000)
      throw new RangeError('Dead-man timeout must be from 5000 to 600000 milliseconds')
    const accounts = [...new Set(accountIds)]
    if (accounts.length === 0 || !accounts.every(isId))
      throw new TypeError('Expected one or more desk venue account IDs')
    let lastError: ApiError | null = null
    let timer: ReturnType<typeof setTimeout> | undefined
    let stopped = false
    async function refresh() {
      lastError = null
      for (const accountId of accounts) {
        if (stopped) return
        try {
          const reply = await arm(accountId, timeoutMs)
          if (!reply.ok) lastError = new ApiError(reply.message, { code: reply.code, requestId: reply.requestId })
        } catch (cause) {
          lastError = cause instanceof ApiError ? cause : new ApiError('Desk dead-man refresh failed', { cause })
        }
      }
      if (!stopped)
        timer = setTimeout(() => {
          pending = refresh()
        }, timeoutMs / 3)
    }
    let pending = refresh()
    return {
      lastError: () => lastError,
      async stop() {
        stopped = true
        clearTimeout(timer)
        await pending
      },
    }
  }

  return {
    orders: {
      place(input: PlaceOrderInput) {
        const id = clientOrderId(input.clientOrderId ?? orderId(now()))
        const result = mutate(
          'placeOrder',
          input.accountId,
          { ...orderTriggerBody(input), clientOrderId: id },
          id,
        ).then((reply) => {
          if (!reply.ok) return reply
          if (reply.data === null || typeof reply.data !== 'object' || !('orderId' in reply.data))
            throw new ApiError('The desk gateway returned no order ID', {
              requestId: reply.requestId,
              clientOrderId: id,
            })
          return { ...reply, data: reply.data as LocalOrder }
        })
        return { clientOrderId: id, result }
      },
      async get(accountId: string, id: string) {
        const target =
          '/internal/orders/' + encodeURIComponent(clientOrderId(id)) + '?' + new URLSearchParams({ accountId, userId })
        const reply = await request('getOrder', accountId, target)
        if (reply.ok && reply.data.accountId !== accountId)
          throw new ApiError('Desk order belongs to another account', { requestId: reply.requestId })
        return reply
      },
      cancel: (accountId: string, id: string) => mutate('cancelOrder', accountId, { accountId }, clientOrderId(id)),
      modify: (accountId: string, id: string, change: { readonly price?: Money; readonly size?: Money }) =>
        mutate('modifyOrder', accountId, { accountId, ...change }, clientOrderId(id)),
      cancelAll: (accountId: string, instrumentId?: string) =>
        mutate('cancelAll', accountId, { accountId, instrumentId }),
      list(_query?: QueryOf<'listOrders'>): AsyncGenerator<DataOf<'getOrder'>> {
        throw new ApiError('Desk gateway order listing is unavailable', { code: 'feature_unavailable' })
      },
      async submitTx(
        _accountId: string,
        _id: string,
        _payload: BodyOf<'submitOrderTx'>['payload'],
      ): Promise<Answer<'submitOrderTx'>> {
        throw new ApiError('Desk gateway cannot submit wallet transactions', { code: 'feature_unavailable' })
      },
    },
    deadman: {
      arm,
      clear: (accountId: string) => mutate('setDeadMan', accountId, { accountId, timeoutMs: 0 }),
      keepAlive,
    },
  }
}

export type LocalGatewayClient = ReturnType<typeof createLocalGatewayClient>
