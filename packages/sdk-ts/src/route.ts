import { dec, type VenueId } from '@atr/core'
import type { Client, OrderTriggerInput, PlaceOrderInput } from './client.ts'
import { type RelaySigner, relayPlace } from './relay.ts'
import type { PlaceLeg, Route, RouteLeg } from './route-execution.ts'
import { ApiError } from './transport.ts'

export type {
  PlaceLeg,
  Route,
  RouteAnswer,
  RouteExecuteInput,
  RouteInput,
  RouteLeg,
  RoutePlaceInput,
} from './route-execution.ts'

export interface DirectLegOrder extends PlaceOrderInput {
  readonly venueClientId: string
  readonly ourRate: string
  readonly placeBefore: string
}
export type DirectLegSender = (order: DirectLegOrder) => Promise<void>
export interface LegPlacerOptions {
  readonly client?: Pick<Client, 'orders' | 'relay'>
  readonly relay?: Readonly<Partial<Record<VenueId, RelaySigner>>>
  readonly direct?: Readonly<Partial<Record<VenueId, DirectLegSender>>>
  readonly wallet?: (order: PlaceOrderInput, leg: RouteLeg, route: Route) => Promise<void>
  readonly triggers?: (
    leg: RouteLeg,
    route: Route,
  ) => { readonly tp?: OrderTriggerInput; readonly sl?: OrderTriggerInput }
  readonly now?: () => number
}

/** Submit the exact reserved order and hand a direct module the exact venue ID and all-in fee rate. */
export function legPlacer(options: LegPlacerOptions): PlaceLeg {
  const now = options.now ?? Date.now
  return async (leg, route) => {
    const fail = (message: string) =>
      new ApiError(message, { code: 'route_leg_invalid', clientOrderId: leg.clientOrderId })
    const expires = Date.parse(leg.placeBefore)
    const ready = leg.state === 'reserved' || (leg.place === 'direct' && leg.state === 'placed')
    if (!ready || !Number.isFinite(expires) || expires <= now()) throw fail('Route leg reservation expired')
    if (dec.parse(leg.size) <= 0n || dec.parse(leg.limitPrice) <= 0n || dec.parse(leg.ourRate) < 0n)
      throw fail('Invalid route leg terms')
    const order: PlaceOrderInput = {
      accountId: leg.accountId,
      instrumentId: leg.instrumentId,
      side: route.side,
      type: 'limit',
      tif: leg.tif,
      size: leg.size,
      price: leg.limitPrice,
      reduceOnly: route.reduceOnly,
      routeId: route.routeId,
      clientOrderId: leg.clientOrderId,
      ...options.triggers?.(leg, route),
    }
    if (leg.place === 'direct') {
      const send = options.direct?.[leg.venue]
      if (send === undefined || leg.venueClientId === '') throw fail('No matching direct route leg sender')
      return send({ ...order, venueClientId: leg.venueClientId, ourRate: leg.ourRate, placeBefore: leg.placeBefore })
    }
    if (leg.place === 'wallet') {
      if (options.wallet === undefined) throw fail('No wallet route leg signer')
      return options.wallet(order, leg, route)
    }
    const client = options.client
    if (client === undefined || leg.place === 'gateway') throw fail('No client for reserved route leg')
    const signer = options.relay?.[leg.venue]
    if (leg.place === 'relay' && (signer === undefined || signer.venue !== leg.venue))
      throw fail('No matching relay route leg signer')
    const reply = await (leg.place === 'relay' && signer !== undefined
      ? relayPlace(client, order, signer).result
      : client.orders.place(order).result)
    if (!reply.ok)
      throw new ApiError(reply.message, {
        code: reply.code,
        requestId: reply.requestId,
        clientOrderId: leg.clientOrderId,
      })
  }
}
