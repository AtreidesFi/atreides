// Direct Bybit V5: Broker ID in `Referer` and the `orderLinkId` prefix (06 §4.6, §1.5, §8; CEX-201).
// Both transforms run before signing: `Referer` is outside the signed string, `orderLinkId` is inside the body.
import {
  type CexTag,
  type DirectCexOptions,
  isObject,
  type Json,
  type JsonObject,
  prefixClientId,
  withHeader,
} from './cex.ts'

const LINK_ID = /^[A-Za-z0-9_-]+$/
const LINK_ID_MAX = 36
const REST_CREATE = '/v5/order/create'
const REST_BATCH = '/v5/order/create-batch'
/** Bybit reads the Broker ID from either spelling, so a bot's `X-Referer` is dropped too. */
const ALIASES = ['X-Referer']
const NOTICE = "Bybit: the bot's own Referer was replaced by the platform Broker ID"

export interface BybitRestRequest {
  readonly method: string
  /** Path without the query. */
  readonly path: string
  readonly headers?: Readonly<Record<string, string>>
  readonly body?: JsonObject
}
export interface BybitWsFrame {
  readonly op: string
  readonly header?: Readonly<Record<string, string>>
  readonly args: readonly Json[]
  readonly [key: string]: Json | undefined
}

export interface DirectBybit {
  tag(): CexTag
  /** Create and batch-create get `Referer` and the id prefix; every other request passes through unchanged. */
  rest(request: BybitRestRequest): BybitRestRequest
  /** `order.create` and `order.create-batch` get `header.Referer` and the id prefix. */
  ws(frame: BybitWsFrame): BybitWsFrame
}

export function directBybit(tag: CexTag, options: DirectCexOptions = {}): DirectBybit {
  if (tag.kind !== 'header' || tag.name !== 'Referer') throw new RangeError('Bybit tag is the Referer header')
  const prefix = options.prefixClientIds === false ? null : (options.clientPrefix ?? 'a1')
  const notice = options.onNotice ?? ((message: string) => console.warn(message))
  const tagged = (held: Readonly<Record<string, string>> | undefined) =>
    tag.value === '' ? held : withHeader(held, tag.name, tag.value, ALIASES, () => notice(NOTICE))
  const link = (order: Json): Json => {
    if (prefix === null || !isObject(order) || order.orderLinkId === undefined) return order
    return { ...order, orderLinkId: prefixClientId(order.orderLinkId, prefix, LINK_ID_MAX, LINK_ID) as Json }
  }
  const single = (body: JsonObject): JsonObject => link(body) as JsonObject
  const batch = (body: JsonObject): JsonObject =>
    Array.isArray(body.request) ? { ...body, request: (body.request as readonly Json[]).map(link) } : body
  return {
    tag: () => tag,
    rest(request) {
      const create = request.method === 'POST' && request.path === REST_CREATE
      const many = request.method === 'POST' && request.path === REST_BATCH
      if (!create && !many) return request
      const headers = tagged(request.headers)
      const body = request.body === undefined ? undefined : create ? single(request.body) : batch(request.body)
      return { ...request, ...(headers === undefined ? {} : { headers }), ...(body === undefined ? {} : { body }) }
    },
    ws(frame) {
      if (frame.op !== 'order.create' && frame.op !== 'order.create-batch') return frame
      const map = frame.op === 'order.create' ? single : batch
      const header = tagged(frame.header)
      return {
        ...frame,
        ...(header === undefined ? {} : { header }),
        args: frame.args.map((arg) => (isObject(arg) ? map(arg) : arg)),
      }
    },
  }
}
