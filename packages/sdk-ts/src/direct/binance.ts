// Direct Binance attribution (06 Binance §2.6.1, §2.7.2, §2.7.5, F3; BNC-010, BNC-011, BNC-013, BNC-014).
// The caller keeps its own Binance client and signing; this module only writes our `x-` code into client ids.
import type { FeeField, OrderType, TimeInForce } from '@atr/contract'
import { AppError, type InstrumentClass, parseInstant } from '@atr/core'
import type { DirectContext, DirectModule, DirectStart } from './template.ts'

export type BinanceProduct = 'spot' | 'usdm'
type Params = Readonly<Record<string, string>>

const PREFIX = /^x-[A-Za-z0-9_-]{1,18}$/
const CODE = /^[A-Za-z0-9_-]{1,18}$/
const ID_CHARS = /^[A-Za-z0-9_-]+$/
const CUSTOMER_ID = /^[A-Za-z0-9]{8,32}$/
const BASE62 = '0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz'
const MAX_ID = 36
/** Spot amend (PUT): without `newClientOrderId` Binance gives the order a random id, which drops our prefix. */
const AMEND = '/api/v3/order/amend/keepPriority'

/** Client id fields a POST must carry, per product and path. */
const REQUIRED: Readonly<Record<BinanceProduct, Readonly<Record<string, readonly string[]>>>> = {
  spot: {
    '/api/v3/order': ['newClientOrderId'],
    '/api/v3/order/test': ['newClientOrderId'],
    '/api/v3/order/cancelReplace': ['newClientOrderId'],
    '/api/v3/sor/order': ['newClientOrderId'],
    '/api/v3/sor/order/test': ['newClientOrderId'],
    '/api/v3/order/oco': ['listClientOrderId', 'limitClientOrderId', 'stopClientOrderId'],
    '/api/v3/orderList/oco': ['listClientOrderId', 'aboveClientOrderId', 'belowClientOrderId'],
    '/api/v3/orderList/oto': ['listClientOrderId', 'workingClientOrderId', 'pendingClientOrderId'],
    '/api/v3/orderList/otoco': [
      'listClientOrderId',
      'workingClientOrderId',
      'pendingAboveClientOrderId',
      'pendingBelowClientOrderId',
    ],
    '/api/v3/orderList/opo': ['listClientOrderId', 'workingClientOrderId', 'pendingClientOrderId'],
    '/api/v3/orderList/opoco': [
      'listClientOrderId',
      'workingClientOrderId',
      'pendingAboveClientOrderId',
      'pendingBelowClientOrderId',
    ],
  },
  usdm: {
    '/fapi/v1/order': ['newClientOrderId'],
    '/fapi/v1/algoOrder': ['clientAlgoId'],
    '/fapi/v1/batchOrders': [],
  },
}

/** CCXT's `options.broker` keys that decide the id prefix (ccxt binance.ts, generateClientOrderId). */
export const CCXT_BROKER_KEYS = ['spot', 'margin', 'future', 'swap'] as const

export interface BinanceKeypair {
  /** SPKI PEM: the text the user pastes into API Management → Self-generated. */
  readonly publicKeyPem: string
  /** PKCS#8 PEM: stays with the bot and is never sent anywhere. */
  readonly privateKeyPem: string
}

function pem(label: string, der: ArrayBuffer): string {
  const body = btoa(String.fromCharCode(...new Uint8Array(der))).replace(/.{1,64}/g, '$&\n')
  return `-----BEGIN ${label}-----\n${body}-----END ${label}-----\n`
}

/** A fresh Ed25519 key pair, generated locally at call time (preferred direct key, binance.md §2a step 1). */
export async function keygen(): Promise<BinanceKeypair> {
  const pair = (await globalThis.crypto.subtle.generateKey({ name: 'Ed25519' }, true, [
    'sign',
    'verify',
  ])) as CryptoKeyPair
  return {
    publicKeyPem: pem('PUBLIC KEY', await globalThis.crypto.subtle.exportKey('spki', pair.publicKey)),
    privateKeyPem: pem('PRIVATE KEY', await globalThis.crypto.subtle.exportKey('pkcs8', pair.privateKey)),
  }
}

function base62(bytes: Uint8Array): string {
  let n = bytes.reduce((acc, byte) => (acc << 8n) | BigInt(byte), 0n)
  let out = ''
  do {
    out = BASE62[Number(n % 62n)] + out
    n /= 62n
  } while (n > 0n)
  return out
}

/** CCXT's one-line broker option: `new ccxt.binance({ options: { ...mine, ...ccxtBrokerOptions(codes, probe) } })`. */
export function ccxtBrokerOptions(
  codes: { readonly spot: string | null; readonly usdm: string | null },
  /** Pass `exchange.options.broker` from a built CCXT instance to check that the option names still exist. */
  known?: Readonly<Record<string, unknown>>,
): { readonly broker: Readonly<Record<string, string>> } {
  const broker: Record<string, string> = {}
  for (const [key, code] of [
    ['spot', codes.spot],
    ['margin', codes.spot],
    ['future', codes.usdm],
    ['swap', codes.usdm],
  ] as const) {
    if (code === null) continue
    if (!CODE.test(code)) throw new AppError('fee_code_missing')
    if (known !== undefined && !(key in known)) throw new AppError('invalid_request', { field: `broker.${key}` })
    broker[key] = `x-${code}`
  }
  return { broker }
}

export interface BinanceDirectOptions {
  readonly now?: () => number
}

export class BinanceDirect implements DirectModule {
  readonly venue = 'binance' as const
  private readonly now: () => number
  private context: DirectContext | null = null
  private published: DirectStart | null = null
  private prefix: string | null = null
  private readonly ids = new Map<string, string>()

  constructor(
    readonly product: BinanceProduct,
    options: BinanceDirectOptions = {},
  ) {
    this.now = options.now ?? Date.now
  }

  async start(ctx: DirectContext): Promise<DirectStart> {
    this.context = ctx
    this.published = null
    return this.refresh()
  }

  /** Rereads GET /v1/fees once the published snapshot has expired. `builderRef` is `x-<code>` or null (BNC-013). */
  async refresh(): Promise<DirectStart> {
    const ctx = this.context
    if (ctx === null) throw new AppError('fee_code_missing')
    if (this.published !== null && this.published.validUntil > this.now()) return this.published
    const accounts = await ctx.client.accounts.list()
    if (!accounts.ok) throw new AppError('account_not_bound')
    const matches = accounts.data.filter(
      (account) => account.venue === 'binance' && (account.subAccountRef ?? account.accountRef) === ctx.accountRef,
    )
    const account = matches.length === 1 ? matches[0] : undefined
    if (account === undefined) throw new AppError('account_not_bound')
    const answer = await ctx.client.fees.schedule({ venue: 'binance', accountId: account.accountId })
    if (!answer.ok) throw new AppError('unavailable')
    const schedules = answer.data.filter(({ venue }) => venue === 'binance')
    const schedule = schedules.length === 1 ? schedules[0] : undefined
    if (schedule === undefined) throw new AppError('fee_code_missing')
    const validUntil = parseInstant(schedule.validUntil)
    if (validUntil <= this.now()) throw new AppError('fee_code_missing')
    const prefix = schedule.builderRef === null || schedule.builderRef === '' ? null : schedule.builderRef
    if (prefix !== null && !PREFIX.test(prefix)) throw new AppError('fee_code_missing')
    this.prefix = prefix
    // Binance pays a rebate or bills ledger-side; no per-order rate, approval or class cap exists (§2.6.3).
    this.published = Object.freeze({
      venue: 'binance' as const,
      rates: Object.freeze([]),
      approvedCap: null,
      classCaps: Object.freeze({}) as Readonly<Partial<Record<InstrumentClass, never>>>,
      builderRef: prefix,
      validUntil,
    })
    return this.published
  }

  /** `x-<code>`, or null while no client code exists: then no order carries an `x-` prefix (BNC-013). */
  clientPrefix(): string | null {
    return this.prefix
  }

  feeFields(_cls: InstrumentClass, _type: OrderType, _tif: TimeInForce, _reduceOnly: boolean): FeeField {
    return this.prefix === null
      ? { kind: 'none', reason: 'venue_has_no_fee_path' }
      : { kind: 'prefixed_client_id', prefix: this.prefix }
  }

  /**
   * The venue client order id for the bot's own id: `x-<code>` + the id if it fits, else
   * `x-<code>` + the last characters of base62(SHA-256(id)). Without an id, a random 22-character base62 suffix.
   * The map back to the bot's id stays in memory (`botId`).
   */
  async clientOrderId(userId?: string): Promise<string> {
    const prefix = this.prefix
    if (prefix === null)
      return userId ?? base62(globalThis.crypto.getRandomValues(new Uint8Array(16))).padStart(22, '0')
    const room = MAX_ID - prefix.length
    if (userId === undefined) {
      const random = base62(globalThis.crypto.getRandomValues(new Uint8Array(16))).padStart(22, '0')
      return `${prefix}${random.slice(-Math.min(22, room))}`
    }
    if (userId.startsWith(prefix) && ID_CHARS.test(userId) && userId.length <= MAX_ID) return userId
    if (ID_CHARS.test(userId) && userId.length <= room) return this.remember(`${prefix}${userId}`, userId)
    const digest = new Uint8Array(await globalThis.crypto.subtle.digest('SHA-256', new TextEncoder().encode(userId)))
    return this.remember(`${prefix}${base62(digest).padStart(room, '0').slice(-room)}`, userId)
  }

  /** The bot's own id for a venue id this module produced, or undefined. */
  botId(venueClientOrderId: string): string | undefined {
    return this.ids.get(venueClientOrderId)
  }

  private remember(venueId: string, userId: string): string {
    const known = this.ids.get(venueId)
    if (known !== undefined && known !== userId) throw new AppError('invalid_request', { field: 'clientOrderId' })
    this.ids.set(venueId, userId)
    return venueId
  }

  /**
   * Params of one request with every client id field carrying our prefix (BNC-011): single and SOR orders,
   * cancel-replace, the spot amend (`PUT`; the original id is kept when it already carries our prefix), spot order lists
   * (list id and every leg), USD-M algo orders and each item of `batchOrders`. Cancels, USD-M modifies and reads pass
   * through unchanged (they name the venue id). Returns a copy.
   */
  async tagParams(method: string, path: string, params: Params): Promise<Params> {
    const prefix = this.prefix
    if (prefix === null) return params
    const verb = method.toUpperCase()
    if (this.product === 'spot' && verb === 'PUT' && path === AMEND) {
      const orig = params.origClientOrderId
      const own = params.newClientOrderId ?? (orig?.startsWith(prefix) ? orig : undefined)
      return { ...params, newClientOrderId: await this.clientOrderId(own) }
    }
    if (verb !== 'POST') return params
    const required = REQUIRED[this.product][path]
    if (required === undefined) return params
    const out: Record<string, string> = { ...params }
    for (const field of required) out[field] = await this.clientOrderId(params[field])
    if (path === '/fapi/v1/batchOrders') {
      const raw = params.batchOrders
      const items: unknown = raw === undefined ? null : JSON.parse(raw)
      if (!Array.isArray(items)) throw new AppError('invalid_request', { field: 'batchOrders' })
      const tagged: unknown[] = []
      for (const item of items as Record<string, unknown>[]) {
        const own = item.newClientOrderId
        tagged.push({ ...item, newClientOrderId: await this.clientOrderId(typeof own === 'string' ? own : undefined) })
      }
      out.batchOrders = JSON.stringify(tagged)
    }
    return out
  }

  /** The unsigned Customer ID request (BNC-014). The caller adds timestamp and signature with its own key. */
  customerIdRequest(op: 'set' | 'get', customerId?: string): { method: 'POST' | 'GET'; path: string; params: Params } {
    const prefix = this.prefix
    if (prefix === null) throw new AppError('fee_code_missing')
    const code = prefix.slice(2)
    const [path, codeField] =
      this.product === 'spot'
        ? (['/sapi/v1/apiReferral/userCustomization', 'apiAgentCode'] as const)
        : (['/fapi/v1/apiReferral/userCustomization', 'brokerId'] as const)
    if (op === 'get') return { method: 'GET', path, params: { [codeField]: code } }
    if (customerId === undefined || !CUSTOMER_ID.test(customerId))
      throw new AppError('invalid_request', { field: 'customerId' })
    return { method: 'POST', path, params: { customerId, [codeField]: code } }
  }
}
