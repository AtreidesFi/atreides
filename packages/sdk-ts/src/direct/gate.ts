// Direct Gate tagging (06-venue-cex.md §1.5, §6.6, §8; CEX-403, CEX-406). The caller owns the HMAC secret and signs the
// prepared request with the Gate sign module after `prepare`: the header is not signed, `text` is part of the body hash.
export const GATE_CHANNEL_HEADER = 'X-Gate-Channel-Id'
const ORDER_PATHS = new Set([
  '/api/v4/spot/orders',
  '/api/v4/spot/batch_orders',
  '/api/v4/futures/usdt/orders',
  '/api/v4/futures/usdt/batch_orders',
])
const TEXT_BYTES = 28
const TEXT_CHARS = /^[0-9A-Za-z_.-]*$/
const WS_WARNING =
  'Gate WebSocket orders carry no channel id until Gate confirms the header there; this order is untagged.'

export interface GateDirectOptions {
  /** Our channel id from config; '' means the program is not live and nothing is sent. */
  readonly channelId: string
  /** Default true. Adds the client id prefix to `text` on order paths. */
  readonly prefixClientIds?: boolean
  /** Neutral string from `venues.gate.sdk_client_prefix`; default 'a1'. */
  readonly clientPrefix?: string
  readonly warn?: (message: string) => void
}

export interface GateOutgoing {
  readonly method: 'GET' | 'POST' | 'PUT' | 'PATCH' | 'DELETE'
  /** Resource path including /api/v4. */
  readonly path: string
  readonly body?: string
}

export interface GatePrepared {
  readonly body: string | undefined
  /** Unsigned extra headers; merge with the signature headers. */
  readonly headers: Readonly<Record<string, string>>
}

/** `t-` + prefix + the bot's id, cut to 28 bytes after `t-`. */
export function gateText(prefix: string, id: string): string {
  const own = id.startsWith('t-') ? id.slice(2) : id
  if (!TEXT_CHARS.test(prefix) || !TEXT_CHARS.test(own)) throw new TypeError('invalid Gate text')
  const joined = own.startsWith(prefix) ? own : prefix + own
  return `t-${joined.slice(0, TEXT_BYTES)}`
}

export class GateDirect {
  readonly venue = 'gate' as const
  #warned = false
  readonly #prefix: string | null
  readonly #warn: (message: string) => void

  constructor(readonly options: GateDirectOptions) {
    if (options.channelId !== '' && /[^!-~]/.test(options.channelId)) throw new TypeError('invalid Gate channel id')
    const prefix = options.clientPrefix ?? 'a1'
    if (!TEXT_CHARS.test(prefix) || prefix.length > TEXT_BYTES) throw new TypeError('invalid Gate client prefix')
    this.#prefix = options.prefixClientIds === false ? null : prefix
    this.#warn = options.warn ?? ((message) => console.warn(message))
  }

  tag() {
    return {
      venue: 'gate',
      kind: 'header',
      name: GATE_CHANNEL_HEADER,
      value: this.options.channelId,
      signed: false,
    } as const
  }

  /** Tags one REST request. Only order-create paths change; no other field of the bot's request is touched. */
  prepare(request: GateOutgoing): GatePrepared {
    if (request.method !== 'POST' || !ORDER_PATHS.has(request.path)) return { body: request.body, headers: {} }
    const headers = this.options.channelId === '' ? {} : { [GATE_CHANNEL_HEADER]: this.options.channelId }
    if (this.#prefix === null || request.body === undefined) return { body: request.body, headers }
    const prefix = this.#prefix
    const parsed: unknown = JSON.parse(request.body)
    let changed = false
    const rewrite = (order: unknown): unknown => {
      if (typeof order !== 'object' || order === null || Array.isArray(order)) return order
      const text = (order as { text?: unknown }).text
      if (typeof text !== 'string') return order
      const tagged = gateText(prefix, text)
      if (tagged === text) return order
      changed = true
      return { ...order, text: tagged }
    }
    const out = Array.isArray(parsed) ? parsed.map(rewrite) : rewrite(parsed)
    // An unchanged body keeps the bot's exact bytes (number formatting, spacing).
    return { body: changed ? JSON.stringify(out) : request.body, headers }
  }

  /** WebSocket order channels: never tagged. Warns once per instance. */
  wsOrder(): { readonly tagged: false } {
    if (!this.#warned) {
      this.#warned = true
      this.#warn(WS_WARNING)
    }
    return { tagged: false }
  }
}
