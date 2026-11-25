import { parseKeyText } from '@atr/api-types/auth'
import { apiOrigin, type Key, object } from '../transport.ts'

export interface ApiKeyFile {
  readonly v: 1
  readonly keyId: string | null
  readonly privateKey: string
  readonly publicKey: string
  readonly createdAt: string
  readonly apiOrigin: string
}

export type KeyScope = 'read' | 'trade'

function seedBytes(value: string): Uint8Array {
  if (!/^[A-Za-z0-9_-]{43}$/.test(value)) throw new TypeError('Expected a 32-byte base64url key')
  const bytes = Uint8Array.from(atob(value.replaceAll('-', '+').replaceAll('_', '/') + '='), (part) =>
    part.charCodeAt(0),
  )
  const canonical = btoa(String.fromCharCode(...bytes))
    .replaceAll('+', '-')
    .replaceAll('/', '_')
    .replace(/=+$/, '')
  if (bytes.length !== 32 || canonical !== value) throw new TypeError('Expected a 32-byte base64url key')
  return bytes
}

export function parseKeyFile(value: unknown): ApiKeyFile {
  const input = object(value)
  if (
    input === null ||
    input.v !== 1 ||
    (input.keyId !== null && (typeof input.keyId !== 'string' || parseKeyText(input.keyId, 'ed25519') === null)) ||
    typeof input.privateKey !== 'string' ||
    typeof input.publicKey !== 'string' ||
    typeof input.createdAt !== 'string' ||
    !/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$/.test(input.createdAt) ||
    !Number.isFinite(Date.parse(input.createdAt)) ||
    typeof input.apiOrigin !== 'string'
  ) {
    throw new TypeError('Malformed API key file')
  }
  seedBytes(input.privateKey)
  seedBytes(input.publicKey)
  return {
    v: 1,
    keyId: input.keyId,
    privateKey: input.privateKey,
    publicKey: input.publicKey,
    createdAt: input.createdAt,
    apiOrigin: apiOrigin(input.apiOrigin),
  }
}

export function boundKey(origin: string, file: ApiKeyFile): Key {
  const key = parseKeyFile(file)
  if (key.keyId === null || key.apiOrigin !== apiOrigin(origin)) {
    throw new TypeError('The key file must be bound to this API origin')
  }
  return { keyText: key.keyId, seed: seedBytes(key.privateKey) }
}

export async function generateKeyPair(origin: string, now = Date.now): Promise<ApiKeyFile> {
  const { publicKey, privateKey } = await crypto.subtle.generateKey('Ed25519', true, ['sign', 'verify'])
  const secret = await crypto.subtle.exportKey('jwk', privateKey)
  const published = await crypto.subtle.exportKey('jwk', publicKey)
  return parseKeyFile({
    v: 1,
    keyId: null,
    privateKey: secret.d,
    publicKey: published.x,
    createdAt: new Date(now()).toISOString(),
    apiOrigin: origin,
  })
}

export function keyRequestLink(appOrigin: string, key: ApiKeyFile, name: string, scopes: readonly KeyScope[]): string {
  if (!/^[A-Za-z0-9 ._-]{1,40}$/.test(name) || !['read', 'read,trade'].includes(scopes.join(','))) {
    throw new TypeError('Expected a key name and read or read,trade scopes')
  }
  const query = new URLSearchParams({ publicKey: parseKeyFile(key).publicKey, name, scopes: scopes.join(',') })
  return `${apiOrigin(appOrigin)}/api-bots?${query}`
}
