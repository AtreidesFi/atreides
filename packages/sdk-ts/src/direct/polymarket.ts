import {
  amounts,
  l2Signature,
  newSalt,
  newSessionKey,
  postOrderBody,
  signClobAuth,
  signerAddress,
  signOrder,
  ZERO32,
} from '@atr/venue-polymarket/sign'
import type { Client } from '../client.ts'
import { ApiError } from '../transport.ts'

type Address = `0x${string}`
type Family = 'ctf' | 'ctf_neg_risk' | 'v2'
type Tick = '0.1' | '0.01' | '0.005' | '0.0025' | '0.001' | '0.0001'
type TimeInForce = 'GTC' | 'GTD' | 'FAK' | 'FOK'
type Relay = Pick<Client['relay'], 'clientOrderId' | 'place'>
const DAY = 86_400_000
const HOUR = 3_600_000
const uuid = /^[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}$/iu
const random = (bytes: number) => globalThis.crypto.getRandomValues(new Uint8Array(bytes))

export interface PolymarketCredentials {
  readonly key: string
  readonly secret: string
  readonly passphrase: string
}

export interface PolymarketBinding {
  readonly accountId: string
  readonly connectionId: string
  readonly wallet: Address
  readonly sessionSigner: Address
  readonly validUntil: string
}

export interface PolymarketOrder {
  readonly tokenId: string
  readonly family: Family
  readonly price: string
  readonly shares: string
  readonly tick: Tick
  readonly side: 'buy' | 'sell'
  readonly builder: Address
  readonly timeInForce?: TimeInForce
  readonly expiration?: number
  readonly postOnly?: boolean
  readonly reduceOnly?: boolean
  readonly clientOrderId?: string
}

function wallet(value: string): Address {
  if (!/^0x[0-9a-f]{40}$/iu.test(value) || /^0x0{40}$/u.test(value)) throw new TypeError('Invalid Polymarket wallet')
  return value.toLowerCase() as Address
}

function localUrl(text: string): URL {
  const url = new URL(text)
  if (
    url.username ||
    url.password ||
    url.hash ||
    url.search ||
    (url.protocol !== 'https:' &&
      !(url.protocol === 'http:' && ['localhost', '127.0.0.1', '[::1]'].includes(url.hostname)))
  )
    throw new TypeError('A trusted HTTPS origin is required')
  return url
}

export function createPolymarketSessionSigner(): { readonly address: Address; readonly privateKey: Uint8Array } {
  const privateKey = newSessionKey(random)
  return { address: signerAddress(privateKey), privateKey }
}

function credentials(value: unknown): PolymarketCredentials {
  if (typeof value !== 'object' || value === null) throw new ApiError('Invalid CLOB credential response')
  const fields = value as Record<string, unknown>
  const key = fields.apiKey
  const { secret, passphrase } = fields
  if (
    typeof key !== 'string' ||
    !/^[A-Za-z0-9_-]{1,128}$/u.test(key) ||
    typeof secret !== 'string' ||
    !/^[A-Za-z0-9+/_-]{1,254}={0,2}$/u.test(secret) ||
    typeof passphrase !== 'string' ||
    !/^[!-~]{1,256}$/u.test(passphrase)
  )
    throw new ApiError('Invalid CLOB credential response')
  return { key, secret, passphrase }
}

export class PolymarketDirect {
  readonly address: Address
  readonly #privateKey: Uint8Array
  readonly #app: URL
  readonly #now: () => number
  readonly #relay: Relay

  constructor(options: {
    readonly client: { readonly relay: Relay }
    readonly appOrigin: string
    readonly privateKey: Uint8Array
    readonly now?: () => number
  }) {
    this.#privateKey = options.privateKey.slice()
    this.address = signerAddress(this.#privateKey)
    this.#app = localUrl(options.appOrigin)
    this.#relay = options.client.relay
    this.#now = options.now ?? Date.now
  }

  /** This link conveys only the public Session Key to the connected owner's browser. */
  connectLink(accountId?: string): string {
    if (accountId !== undefined && !uuid.test(accountId)) throw new TypeError('Invalid Polymarket account ID')
    const link = new URL('/connections/polymarket/connect', this.#app.origin)
    link.searchParams.set('mode', 'direct')
    link.searchParams.set('keyPlace', 'bot')
    link.searchParams.set('kind', 'register_key')
    link.searchParams.set('sessionSigner', this.address)
    if (accountId !== undefined) link.searchParams.set('accountId', accountId)
    return link.href
  }

  renewal(binding: PolymarketBinding): { readonly daysLeft: number; readonly url: string } | null {
    const { remaining } = this.#binding(binding)
    if (remaining > 30 * DAY) return null
    const link = new URL(`/connections/${binding.connectionId}`, this.#app.origin)
    link.searchParams.set('action', 'renew')
    return { daysLeft: Math.max(0, Math.ceil(remaining / DAY)), url: link.href }
  }

  /** Run once after the owner's authorization is visible. Only the pinned CLOB receives the L1 proof. */
  async createCredentials(binding: PolymarketBinding, send: typeof fetch = fetch): Promise<PolymarketCredentials> {
    const { remaining } = this.#binding(binding)
    if (remaining <= 0) throw new ApiError('Renew the expired Session Key', { code: 'agent_expired' })
    const ts = Math.floor(this.#now() / 1000)
    const headers = {
      POLY_ADDRESS: this.address,
      POLY_SIGNATURE: signClobAuth(this.#privateKey, ts),
      POLY_TIMESTAMP: String(ts),
      POLY_NONCE: '0',
    }
    const create = await send('https://clob.polymarket.com/auth/api-key', {
      method: 'POST',
      headers,
      redirect: 'error',
      signal: AbortSignal.timeout(10_000),
    })
    if (create.ok) return credentials(await create.json())
    if (create.status !== 400 && create.status !== 409)
      throw new ApiError('Polymarket refused CLOB credential creation', { status: create.status })
    await create.body?.cancel()
    const derive = await send('https://clob.polymarket.com/auth/derive-api-key', {
      method: 'GET',
      headers,
      redirect: 'error',
      signal: AbortSignal.timeout(10_000),
    })
    if (!derive.ok) throw new ApiError('Polymarket refused CLOB credential derivation', { status: derive.status })
    return credentials(await derive.json())
  }

  /** Sign once, then let the API client retry with the same client ID and exact CLOB bytes. */
  place(binding: PolymarketBinding, creds: PolymarketCredentials, input: PolymarketOrder) {
    const { maker, remaining } = this.#binding(binding)
    if (remaining <= 0) throw new ApiError('Renew the expired Session Key', { code: 'agent_expired' })
    if (remaining <= HOUR && input.reduceOnly !== true)
      throw new ApiError('Opening orders wait for Session Key renewal', { code: 'key_expiring' })
    if (input.side !== 'buy' && input.side !== 'sell') throw new TypeError('Invalid Polymarket side')
    if (!/^[1-9]\d{0,77}$/u.test(input.tokenId) || !/^0x[0-9a-f]{64}$/iu.test(input.builder))
      throw new TypeError('Invalid Polymarket market or builder')
    credentials({ apiKey: creds.key, secret: creds.secret, passphrase: creds.passphrase })
    const id = this.#relay.clientOrderId(input.clientOrderId)
    const ts = this.#now()
    const fields = {
      salt: newSalt(random),
      maker,
      signer: maker,
      tokenId: BigInt(input.tokenId),
      ...amounts(input.side === 'buy' ? 'BUY' : 'SELL', input.price, input.shares, input.tick),
      side: input.side === 'buy' ? (0 as const) : (1 as const),
      signatureType: 3 as const,
      timestamp: BigInt(ts),
      metadata: ZERO32,
      builder: input.builder,
    }
    const expires = input.expiration ?? 0
    if (expires !== 0 && (expires * 1000 <= ts || expires * 1000 > ts + remaining))
      throw new TypeError('Order expiration must be within the Session Key lifetime')
    const type = input.timeInForce ?? 'GTC'
    const signature = signOrder(fields, input.family, this.#privateKey, this.address)
    const body = postOrderBody(fields, signature, creds.key, type, expires, input.postOnly ?? false)
    const seconds = Math.floor(ts / 1000)
    const headers = {
      POLY_ADDRESS: this.address,
      POLY_API_KEY: creds.key,
      POLY_PASSPHRASE: creds.passphrase,
      POLY_SIGNATURE: l2Signature(creds.secret, seconds, 'POST', '/order', body),
      POLY_TIMESTAMP: String(seconds),
    }
    const result = this.#relay.place({
      accountId: binding.accountId,
      clientOrderId: id,
      relay: { venue: 'polymarket', signed: { body, headers } },
    })
    return { clientOrderId: id, result }
  }

  #binding(binding: PolymarketBinding) {
    const validUntil = Date.parse(binding.validUntil)
    const now = this.#now()
    if (
      !uuid.test(binding.accountId) ||
      !uuid.test(binding.connectionId) ||
      wallet(binding.sessionSigner) !== this.address ||
      !Number.isSafeInteger(now) ||
      !Number.isSafeInteger(validUntil)
    )
      throw new ApiError('Session Key does not match the connected wallet', { code: 'account_not_bound' })
    return { maker: wallet(binding.wallet), remaining: validUntil - now }
  }
}
