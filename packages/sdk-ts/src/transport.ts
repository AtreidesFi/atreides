import { OPERATIONS, type OperationId } from '@atr/api-types'
import { parseKeyText, signRequest } from '@atr/api-types/auth'
import { clientOrderId, type Dec, dec } from '@atr/core'
import manifest from '../package.json' with { type: 'json' }

const SDK_HEADER = `sdk-ts/${manifest.version}`
const ORDER_CHARS = '0123456789ABCDEFGHJKMNPQRSTVWXYZ'
const encoder = new TextEncoder()
const decoder = new TextDecoder()
const moneyField =
  /^(?:(?:limit|trigger|avg|filled|reference|remaining|total|min|max|order|quote|base)?(?:price|size|notional|rate|amount)|px|sz|qty)$/i
const retryableStatus = new Set([502, 503, 504])
const operationByPath = new Map(
  (Object.keys(OPERATIONS) as OperationId[]).map((id) => {
    const { method, path } = OPERATIONS[id]
    return [`${method} ${path}`, id]
  }),
)

export interface Key {
  readonly keyText: string
  readonly seed: Uint8Array
}

export interface TransportOptions {
  readonly baseUrl: string
  readonly key?: Key
  readonly fetch?: typeof globalThis.fetch
  readonly now?: () => number
  readonly timeoutMs?: number
  readonly retry?: { readonly attempts: number; readonly baseDelayMs: number }
}

export class ApiError extends Error {
  override readonly name = 'ApiError'
  readonly status: number | null
  readonly code: string | null
  readonly requestId: string | null
  readonly clientOrderId: string | null

  constructor(
    message: string,
    context: {
      readonly status?: number
      readonly code?: string | null
      readonly requestId?: string | null
      readonly clientOrderId?: string | null
      readonly cause?: unknown
    } = {},
  ) {
    super(message, { cause: context.cause })
    this.status = context.status ?? null
    this.code = context.code ?? null
    this.requestId = context.requestId ?? null
    this.clientOrderId = context.clientOrderId ?? null
  }
}

export function apiOrigin(value: string): string {
  const url = new URL(value)
  if (
    !['http:', 'https:'].includes(url.protocol) ||
    url.username !== '' ||
    url.password !== '' ||
    url.pathname !== '/' ||
    url.search !== '' ||
    url.hash !== ''
  ) {
    throw new TypeError('Expected an HTTP API origin')
  }
  return url.origin
}

function nonce(): string {
  return btoa(String.fromCharCode(...crypto.getRandomValues(new Uint8Array(16))))
    .replaceAll('+', '-')
    .replaceAll('/', '_')
    .replace(/=+$/, '')
}

export function orderId(now: number): string {
  let value = BigInt(now) << 80n
  const random = crypto.getRandomValues(new Uint8Array(10))
  for (const [index, byte] of random.entries()) value |= BigInt(byte) << BigInt((9 - index) * 8)
  let out = ''
  for (let shift = 120n; shift >= 0; shift -= 5n) out += ORDER_CHARS.charAt(Number((value >> shift) & 31n))
  return `c${out}`
}

export function object(value: unknown): Record<string, unknown> | null {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : null
}

function serializeNumber(key: string, value: unknown): unknown {
  if (moneyField.test(key)) {
    if (typeof value === 'number') throw new TypeError(`${key} must be a decimal string`)
    if (typeof value === 'bigint') return dec.format(value as Dec)
  }
  return value
}

export function encodeBody(value: unknown): string {
  return JSON.stringify(value, serializeNumber)
}

export function operationId(method: string, path: string): OperationId | undefined {
  return operationByPath.get(`${method} ${path}`)
}

export async function responseObject(response: Response): Promise<Record<string, unknown> | null> {
  try {
    return object(await response.json())
  } catch {
    return null
  }
}

function requestCode(row: Record<string, unknown> | null): string | null {
  return typeof row?.code === 'string' ? row.code : null
}

function retryAfter(header: string | null, now: number, fallback: number): number {
  if (header === null) return fallback
  if (/^[0-9]+$/.test(header)) return Number(header) * 1000
  const date = Date.parse(header)
  return Number.isFinite(date) ? Math.max(0, date - now) : fallback
}

async function pause(ms: number, signal: AbortSignal): Promise<void> {
  signal.throwIfAborted()
  if (ms <= 0) return
  await new Promise<void>((resolve, reject) => {
    const timer = setTimeout(
      () => {
        signal.removeEventListener('abort', abort)
        resolve()
      },
      Math.min(ms, 2_147_483_647),
    )
    const abort = () => {
      clearTimeout(timer)
      reject(signal.reason)
    }
    signal.addEventListener('abort', abort, { once: true })
  })
}

export class Transport {
  readonly origin: string
  readonly now: () => number
  readonly signed: boolean
  #fetch: typeof globalThis.fetch
  #key: Key | undefined
  #attempts: number
  #baseDelay: number
  #timeout: number
  #skew = 0

  constructor(options: TransportOptions) {
    this.origin = apiOrigin(options.baseUrl)
    this.now = options.now ?? Date.now
    this.#fetch = options.fetch ?? globalThis.fetch.bind(globalThis)
    this.#attempts = options.retry?.attempts ?? 3
    this.#baseDelay = options.retry?.baseDelayMs ?? 250
    this.#timeout = options.timeoutMs ?? 10_000
    if (
      !Number.isSafeInteger(this.#attempts) ||
      this.#attempts < 1 ||
      !Number.isFinite(this.#baseDelay) ||
      this.#baseDelay < 0 ||
      !Number.isSafeInteger(this.#timeout) ||
      this.#timeout <= 0
    ) {
      throw new RangeError('Invalid SDK retry or timeout settings')
    }
    const key = options.key
    if (key !== undefined) {
      if (parseKeyText(key.keyText, 'ed25519') === null || key.seed.length !== 32) {
        throw new TypeError('An API key needs its key text and 32-byte seed')
      }
      this.#key = { keyText: key.keyText, seed: key.seed.slice() }
    }
    this.signed = key !== undefined
  }

  headers(method: string, target: string, body: Uint8Array): Record<string, string> {
    const headers: Record<string, string> = { 'X-Api-Client': SDK_HEADER }
    if (this.#key === undefined) return headers
    const request = {
      method,
      target,
      body,
      keyText: this.#key.keyText,
      nonce: nonce(),
      timestampMs: String(this.now() + this.#skew),
    }
    return {
      ...headers,
      'X-Api-Key': request.keyText,
      'X-Api-Timestamp': request.timestampMs,
      'X-Api-Nonce': request.nonce,
      'X-Api-Signature': signRequest(request, this.#key.seed),
    }
  }

  async dispatch(request: Request, id: OperationId): Promise<Response> {
    const url = new URL(request.url)
    if (url.origin !== this.origin) throw new TypeError('An SDK request must stay on its configured origin')
    const { idempotency } = OPERATIONS[id]
    const target = `${url.pathname}${url.search}`
    const headers = new Headers(request.headers)
    if (idempotency === 'header' && !headers.has('Idempotency-Key')) {
      headers.set('Idempotency-Key', crypto.randomUUID())
    }
    let body = new Uint8Array(await request.arrayBuffer())
    let stableOrderId: string | undefined
    if (id === 'placeOrder') {
      const input = object(JSON.parse(decoder.decode(body)))
      if (input === null) throw new TypeError('Placing an order needs an object body')
      const field = input.route === 'best' ? 'clientRouteId' : 'clientOrderId'
      const current = input[field]
      if (current === undefined) {
        stableOrderId = orderId(this.now())
        body = encoder.encode(encodeBody({ ...input, [field]: stableOrderId }))
      } else {
        if (typeof current !== 'string') throw new TypeError(`${field} must be a string`)
        stableOrderId = clientOrderId(current)
      }
    }

    let resynced = false
    let attempt = 0
    const safe = request.method === 'GET' || idempotency !== 'none'
    while (true) {
      const currentHeaders = new Headers(headers)
      for (const [name, value] of Object.entries(this.headers(request.method, target, body))) {
        currentHeaders.set(name, value)
      }
      const sentAt = this.now()
      const signal = AbortSignal.any([request.signal, AbortSignal.timeout(this.#timeout)])
      let response: Response
      try {
        response = await this.#fetch(
          new Request(url, {
            method: request.method,
            headers: currentHeaders,
            body: body.length === 0 ? null : body,
            redirect: 'manual',
            credentials: request.credentials,
            signal,
          }),
        )
      } catch (error) {
        if (!safe || request.signal.aborted || attempt + 1 >= this.#attempts) {
          throw new ApiError('API transport failed', { cause: error, clientOrderId: stableOrderId ?? null })
        }
        await pause(this.#backoff(attempt++), request.signal)
        continue
      }

      const canRetry = attempt + 1 < this.#attempts
      const status = response.status
      const row = status === 401 || status === 409 ? await responseObject(response.clone()) : null
      const code = requestCode(row)
      if (status === 401 && code === 'timestamp_out_of_window' && !resynced && this.signed) {
        const date = Date.parse(response.headers.get('Date') ?? '')
        if (Number.isFinite(date)) {
          this.#skew = date - Math.floor((sentAt + this.now()) / 2)
          resynced = true
          continue
        }
      }
      if (canRetry && status === 429) {
        await pause(
          retryAfter(response.headers.get('Retry-After'), this.now(), this.#backoff(attempt++)),
          request.signal,
        )
        continue
      }
      if (canRetry && status === 409 && code === 'idempotency_in_progress' && idempotency === 'header') {
        const detail = object(row?.details)?.retryAfterMs
        await pause(
          typeof detail === 'number' && Number.isFinite(detail) && detail >= 0 ? detail : this.#backoff(attempt),
          request.signal,
        )
        attempt++
        continue
      }
      if (canRetry && safe && retryableStatus.has(status)) {
        await pause(this.#backoff(attempt++), request.signal)
        continue
      }
      if ([401, 403, 429].includes(status) || status >= 500 || (status >= 300 && status < 400) || status === 0) {
        const refused = row ?? (await responseObject(response))
        throw new ApiError(`API request failed with HTTP ${status}`, {
          status,
          code: requestCode(refused),
          requestId: response.headers.get('X-Request-Id'),
          clientOrderId: stableOrderId ?? null,
        })
      }
      return response
    }
  }

  #backoff(attempt: number): number {
    return this.#baseDelay * 2 ** attempt * (0.5 + Math.random())
  }
}
