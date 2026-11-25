// Shared pieces of the CEX direct modules (06 §8): the tag and the client-id prefix. No venue client is imported.

export type CexTagKind = 'body_field' | 'header' | 'ws_field' | 'ws_header'

/** Our program code for one venue, from config. An empty `value` means the program is not live: nothing is sent. */
export interface CexTag {
  readonly kind: CexTagKind
  readonly name: string
  readonly value: string
}

export interface DirectCexOptions {
  /** Default true: prefix the bot's own client id. */
  readonly prefixClientIds?: boolean
  /** `venues.<v>.sdk_client_prefix` from config; default `a1`. */
  readonly clientPrefix?: string
  /** Default console.warn. Called once each time a bot's own tag value is replaced by ours. */
  readonly onNotice?: (message: string) => void
}

export type Json = null | boolean | number | string | readonly Json[] | { readonly [key: string]: Json }
export type JsonObject = { readonly [key: string]: Json }

/** Prefix the bot's own id, truncating the id to fit. Ids the venue would refuse anyway are left to the venue. */
export function prefixClientId(
  id: Json | undefined,
  prefix: string,
  maxLen: number,
  pattern: RegExp,
): Json | undefined {
  if (typeof id !== 'string' || id === '' || !pattern.test(id)) return id
  if (id.startsWith(prefix)) return id
  return prefix + id.slice(0, maxLen - prefix.length)
}

export function isObject(value: Json | undefined): value is JsonObject {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
}

/** Set one header, dropping any spelling of `name` and `aliases`; `onReplace` runs when a bot value differs from ours. */
export function withHeader(
  headers: Readonly<Record<string, string>> | undefined,
  name: string,
  value: string,
  aliases: readonly string[] = [],
  onReplace?: () => void,
): Record<string, string> {
  const drop = new Set([name, ...aliases].map((key) => key.toLowerCase()))
  const out: Record<string, string> = {}
  let foreign = false
  for (const [key, held] of Object.entries(headers ?? {})) {
    if (!drop.has(key.toLowerCase())) out[key] = held
    else if (held !== '' && held !== value) foreign = true
  }
  if (foreign) onReplace?.()
  out[name] = value
  return out
}
