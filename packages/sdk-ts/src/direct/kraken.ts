// Direct Kraken tagging (06-venue-cex.md §1.5, §7.6, §8; CEX-504). Kraken has no official client, so the caller signs the
// prepared parameters with the Kraken sign module. `broker` is inside the signed bytes: prepare first, then sign.
export type KrakenParams = ReadonlyArray<readonly [string, string]>
export type KrakenProduct = 'spot' | 'futures'

const SPOT_BYTES = 18
const FUTURES_BYTES = 100
// IIBAN: `XXXX YYYY ZZZZ WWWW` or `XXXXYYYYZZZZWWWW` (futures send-order page).
const BROKER = /^[0-9A-Za-z]{4}( ?)[0-9A-Za-z]{4}\1[0-9A-Za-z]{4}\1[0-9A-Za-z]{4}$/
const ASCII = /^[\x20-\x7e]*$/
const UUID = /^[0-9a-f]{8}(-?)[0-9a-f]{4}\1[0-9a-f]{4}\1[0-9a-f]{4}\1[0-9a-f]{12}$/i
const SPOT_ORDER = new Set(['/0/private/AddOrder', '/0/private/AddOrderBatch'])
const FUTURES_ORDER = new Set(['/derivatives/api/v3/sendorder', '/derivatives/api/v3/batchorder'])

export interface KrakenDirectOptions {
  /** Our partner IIBAN from config, sent only when `programLive` is true (CEX-504). */
  readonly broker: string
  readonly programLive: boolean
  /** Default true. */
  readonly prefixClientIds?: boolean
  /** Neutral string from `venues.kraken.sdk_client_prefix`; default 'a1'. */
  readonly clientPrefix?: string
  readonly warn?: (message: string) => void
}

/** Prefix + the bot's id, cut to the venue's length. UUID-shaped spot ids are left alone: a prefix would break them. */
export function krakenClientId(product: KrakenProduct, prefix: string, id: string): string {
  const max = product === 'spot' ? SPOT_BYTES : FUTURES_BYTES
  if (!ASCII.test(id) || !ASCII.test(prefix)) throw new TypeError('invalid Kraken client id')
  if (product === 'spot' && UUID.test(id)) return id
  return (id.startsWith(prefix) ? id : prefix + id).slice(0, max)
}

export class KrakenDirect {
  readonly venue = 'kraken' as const
  readonly #prefix: string | null
  readonly #warn: (message: string) => void
  #warned = false

  constructor(readonly options: KrakenDirectOptions) {
    if (options.programLive && !BROKER.test(options.broker)) throw new TypeError('invalid Kraken broker')
    const prefix = options.clientPrefix ?? 'a1'
    if (!ASCII.test(prefix) || prefix.length > SPOT_BYTES - 1) throw new TypeError('invalid Kraken client prefix')
    this.#prefix = options.prefixClientIds === false ? null : prefix
    this.#warn = options.warn ?? ((message) => console.warn(message))
  }

  tag() {
    return {
      venue: 'kraken',
      kind: 'body_field',
      name: 'broker',
      value: this.options.programLive ? this.options.broker : '',
      signed: true,
    } as const
  }

  /**
   * Spot: pass the form parameters (without nonce) of AddOrder or AddOrderBatch. Futures: the query parameters of
   * sendorder or batchorder. Other paths are returned unchanged. While the program is live our `broker` replaces one the
   * bot set (with a one-time notice); while it is not, the bot's own `broker` is left as it is. AddOrderBatch goes to
   * Kraken as a JSON body: pass `orders` as its JSON text and put the parsed array back into that body.
   */
  prepare(product: KrakenProduct, path: string, params: KrakenParams): KrakenParams {
    const orders = product === 'spot' ? SPOT_ORDER : FUTURES_ORDER
    if (!orders.has(path)) return params
    const prefix = this.#prefix
    const live = this.options.programLive
    const replaced = params.some(([name, value]) => name === 'broker' && value !== this.options.broker)
    if (live && replaced && !this.#warned) {
      this.#warned = true
      this.#warn('Kraken: the bot set its own broker; the SDK sends its own partner IIBAN instead.')
    }
    const out: Array<readonly [string, string]> = params
      .filter(([name]) => !live || name !== 'broker')
      .map(([name, value]): readonly [string, string] => {
        if (prefix === null) return [name, value]
        if (product === 'spot' && name === 'cl_ord_id') return [name, krakenClientId('spot', prefix, value)]
        if (product === 'spot' && name === 'orders') return [name, this.#batch(value, prefix, 'spot', 'cl_ord_id')]
        if (product === 'futures' && name === 'cliOrdId') return [name, krakenClientId('futures', prefix, value)]
        if (product === 'futures' && name === 'json') return [name, this.#futuresBatch(value, prefix)]
        return [name, value]
      })
    if (live) out.push(['broker', this.options.broker])
    return out
  }

  #batch(value: string, prefix: string, product: KrakenProduct, key: string): string {
    const list: unknown = JSON.parse(value)
    if (!Array.isArray(list)) return value
    let changed = false
    const out = list.map((order: unknown) => {
      const id = (order as Record<string, unknown> | null)?.[key]
      if (typeof id !== 'string') return order
      const next = krakenClientId(product, prefix, id)
      if (next === id) return order
      changed = true
      return { ...(order as object), [key]: next }
    })
    return changed ? JSON.stringify(out) : value
  }

  #futuresBatch(value: string, prefix: string): string {
    const parsed: unknown = JSON.parse(value)
    const list = (parsed as { batchOrder?: unknown } | null)?.batchOrder
    if (!Array.isArray(list)) return value
    let changed = false
    const batchOrder = list.map((entry: unknown) => {
      const e = entry as Record<string, unknown> | null
      if (e?.order !== 'send' || typeof e.cliOrdId !== 'string') return entry
      const next = krakenClientId('futures', prefix, e.cliOrdId)
      if (next === e.cliOrdId) return entry
      changed = true
      return { ...e, cliOrdId: next }
    })
    return changed ? JSON.stringify({ ...(parsed as object), batchOrder }) : value
  }
}
