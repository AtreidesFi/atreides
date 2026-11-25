// Direct Bitget UTA v3: `X-CHANNEL-API-CODE` on REST, top-level `apiCode` on WS, `clientOid` prefix
// (06 §5.2, §1.5, §8; CEX-306). Both transforms run before signing: the header is outside the signed string,
// `clientOid` is inside the body.
import {
  type CexTag,
  type DirectCexOptions,
  isObject,
  type Json,
  type JsonObject,
  prefixClientId,
  withHeader,
} from './cex.ts'

const CLIENT_OID = /^[A-Za-z0-9_-]+$/
const CLIENT_OID_MAX = 32
const REST_PLACE = '/api/v3/trade/place-order'
const REST_BATCH = '/api/v3/trade/place-batch'
const NOTICE = "Bitget: the bot's own channel API code was replaced by the platform code"

export interface BitgetRestRequest {
  readonly method: string
  /** Path without the query. */
  readonly path: string
  readonly headers?: Readonly<Record<string, string>>
  /** An object for `place-order`, an array of orders for `place-batch`. */
  readonly body?: Json
}
export interface BitgetWsFrame {
  readonly op: string
  readonly topic?: string
  readonly args: readonly Json[]
  readonly [key: string]: Json | undefined
}

export interface DirectBitget {
  tag(): CexTag
  /** `place-order` and `place-batch` get the header and the id prefix; everything else passes through. */
  rest(request: BitgetRestRequest): BitgetRestRequest
  /** `trade` frames with topic `place-order` or `batch-place` get top-level `apiCode` and the id prefix. */
  ws(frame: BitgetWsFrame): BitgetWsFrame
}

export function directBitget(tag: CexTag, options: DirectCexOptions = {}): DirectBitget {
  if (tag.kind !== 'header' || tag.name !== 'X-CHANNEL-API-CODE') {
    throw new RangeError('Bitget tag is the X-CHANNEL-API-CODE header')
  }
  const prefix = options.prefixClientIds === false ? null : (options.clientPrefix ?? 'a1')
  const notice = options.onNotice ?? ((message: string) => console.warn(message))
  const order = (value: Json): Json => {
    if (prefix === null || !isObject(value) || value.clientOid === undefined) return value
    return { ...value, clientOid: prefixClientId(value.clientOid, prefix, CLIENT_OID_MAX, CLIENT_OID) as Json }
  }
  return {
    tag: () => tag,
    rest(request) {
      if (request.method !== 'POST' || (request.path !== REST_PLACE && request.path !== REST_BATCH)) return request
      const headers =
        tag.value === '' ? request.headers : withHeader(request.headers, tag.name, tag.value, [], () => notice(NOTICE))
      const body = Array.isArray(request.body)
        ? request.body.map(order)
        : request.body === undefined
          ? undefined
          : order(request.body)
      return { ...request, ...(headers === undefined ? {} : { headers }), ...(body === undefined ? {} : { body }) }
    },
    ws(frame) {
      if (frame.op !== 'trade' || (frame.topic !== 'place-order' && frame.topic !== 'batch-place')) return frame
      const held = frame.apiCode
      if (tag.value !== '' && typeof held === 'string' && held !== '' && held !== tag.value) notice(NOTICE)
      return {
        ...frame,
        ...(tag.value === '' ? {} : { apiCode: tag.value }),
        args: frame.args.map(order),
      }
    },
  }
}
