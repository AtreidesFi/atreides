import type { JsonValue, VenueId } from '@atr/core'
import type { Client, PlaceOrderInput } from './client.ts'
import { encodeBody } from './transport.ts'

type RelayClient = Pick<Client, 'relay'>
type Change = {
  readonly price?: PlaceOrderInput['price']
  readonly size?: PlaceOrderInput['size']
}

export type RelaySignRequest =
  | { readonly kind: 'place'; readonly clientOrderId: string; readonly order: PlaceOrderInput }
  | { readonly kind: 'cancel'; readonly accountId: string; readonly clientOrderId: string }
  | {
      readonly kind: 'modify'
      readonly accountId: string
      readonly clientOrderId: string
      readonly newClientOrderId: string
      readonly change: Change
    }

export interface RelaySigner {
  readonly venue: VenueId
  /** A venue sign-module adapter. The returned wire object is sent unchanged under RelaySigned.signed. */
  sign(request: RelaySignRequest): Promise<Readonly<Record<string, JsonValue>>>
}

async function signed(signer: RelaySigner, request: RelaySignRequest) {
  if (request.kind === 'place') encodeBody(request.order)
  if (request.kind === 'modify') encodeBody(request.change)
  const wire = await signer.sign(request)
  if (wire === null || typeof wire !== 'object' || Array.isArray(wire) || Object.keys(wire).length === 0) {
    throw new TypeError('Relay signer must return a non-empty wire object')
  }
  return { venue: signer.venue, signed: wire }
}

/** Allocate one ID before signing. Transport retries reuse this ID and the same signed body. */
export function relayPlace(client: RelayClient, order: PlaceOrderInput, signer: RelaySigner) {
  const clientOrderId = client.relay.clientOrderId(order.clientOrderId)
  const { accountId, strategyRunId, routeId } = order
  const result = signed(signer, { kind: 'place', clientOrderId, order: { ...order, clientOrderId } }).then((relay) =>
    client.relay.place({
      accountId,
      clientOrderId,
      ...(strategyRunId === undefined ? {} : { strategyRunId }),
      ...(routeId === undefined ? {} : { routeId }),
      relay,
    }),
  )
  return { clientOrderId, result }
}

export async function relayCancel(client: RelayClient, accountId: string, clientOrderId: string, signer: RelaySigner) {
  const wire = await signed(signer, { kind: 'cancel', accountId, clientOrderId })
  return client.relay.cancel(clientOrderId, { accountId, signed: wire })
}

export function relayModify(
  client: RelayClient,
  accountId: string,
  clientOrderId: string,
  change: Change,
  signer: RelaySigner,
  replacementId?: string,
) {
  const newClientOrderId = client.relay.clientOrderId(replacementId)
  const { price, size } = change
  const result = signed(signer, { kind: 'modify', accountId, clientOrderId, newClientOrderId, change }).then((wire) =>
    client.relay.modify(clientOrderId, {
      accountId,
      newClientOrderId,
      signed: wire,
      ...(price === undefined ? {} : { price }),
      ...(size === undefined ? {} : { size }),
    }),
  )
  return { newClientOrderId, result }
}
