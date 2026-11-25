import type { DataOf } from '@atr/api-types'
import { type Dec, dec } from '@atr/core'
import type { Answer, OrderTriggerInput } from './client.ts'
import { ApiError } from './transport.ts'

export interface RouteInput {
  readonly group: string
  readonly side: 'buy' | 'sell'
  readonly type: 'market' | 'limit'
  readonly tif?: 'ioc' | 'gtc' | 'post_only'
  readonly price?: Dec | string
  readonly size?: Dec | string
  readonly notional?: Dec | string
  readonly reduceOnly?: boolean
  readonly maxSlippageBps?: number
  readonly tp?: OrderTriggerInput
  readonly sl?: OrderTriggerInput
  readonly fundingHorizonHours?: number
  readonly split?: boolean
  readonly maxLegs?: number
  readonly crossClass?: boolean
  readonly allowPartial?: boolean
  readonly accountIds?: readonly string[]
  readonly mode?: 'gateway' | 'direct'
}

export type Route = DataOf<'getRoute'>
export type RouteLeg = Route['legs'][number]
export type PlaceLeg = (leg: RouteLeg, route: Route) => Promise<void>
export type RouteAnswer =
  | Extract<Answer<'placeOrder' | 'getRoute' | 'quoteRoute'>, { readonly ok: false }>
  | { readonly ok: true; readonly data: Route; readonly requestId: string }
export type RoutePlaceInput = RouteInput & { readonly quoteId?: string; readonly clientRouteId?: string }
export type RouteExecuteInput = RoutePlaceInput & { readonly followUp?: boolean }

interface RoutePorts {
  readonly get: (routeId: string) => Promise<Answer<'getRoute'>>
  readonly quote: (input: { readonly routeId: string }) => Promise<Answer<'quoteRoute'>>
  readonly register: (input: RoutePlaceInput, replan?: number) => Promise<RouteAnswer>
  readonly newId: () => string
  readonly placeLeg?: PlaceLeg
}

const final = new Set<Route['state']>(['done', 'partial', 'failed', 'cancelled'])
const delay = (ms: number) => new Promise<void>((resolve) => setTimeout(resolve, ms))

export async function executeRoute(start: RouteExecuteInput, ports: RoutePorts): Promise<RouteAnswer> {
  const { followUp, ...original } = start
  let input: RoutePlaceInput = original
  let reply = await ports.register(input)
  const sent = new Set<string>()
  let followUps = 0
  const waitMs = start.mode === 'direct' ? 360_000 : 60_000
  let deadline = performance.now() + waitMs
  while (reply.ok) {
    const route = reply.data
    if (final.has(route.state)) {
      if (
        !followUp ||
        route.mode !== 'direct' ||
        route.state !== 'partial' ||
        route.report?.provisional !== false ||
        !['depth', 'not_replanned'].includes(route.endReason ?? '') ||
        followUps >= 2
      )
        return reply
      const remaining = dec.sub(dec.parse(route.requestedSize), dec.parse(route.filledSize))
      if (remaining <= 0n) return reply
      const { quoteId: _quote, notional: _notional, ...sameRoute } = original
      input = { ...sameRoute, clientRouteId: ports.newId(), size: remaining }
      followUps++
      deadline = performance.now() + waitMs
      reply = await ports.register(input)
      continue
    }
    if (performance.now() >= deadline)
      throw new ApiError('Route outcome is still pending', {
        code: 'unknown_outcome',
        clientOrderId: route.clientRouteId,
        requestId: reply.requestId,
      })
    if (route.execution === 'client') {
      for (const leg of route.legs) {
        const ready = leg.state === 'reserved' || (leg.place === 'direct' && leg.state === 'placed')
        if (!ready || sent.has(leg.clientOrderId)) continue
        if (ports.placeLeg === undefined)
          throw new ApiError('This route requires a client leg placer', {
            clientOrderId: route.clientRouteId,
            requestId: reply.requestId,
          })
        sent.add(leg.clientOrderId)
        await ports.placeLeg(leg, route)
      }
      const settled =
        route.legs.length > 0 && route.legs.every(({ state }) => state === 'released' || state === 'lapsed')
      if (
        route.mode !== 'direct' &&
        route.state === 'working' &&
        settled &&
        route.replans < 2 &&
        route.legs.every(({ place }) => place !== 'wallet') &&
        dec.parse(route.filledSize) < dec.parse(route.requestedSize)
      ) {
        const next = await ports.quote({ routeId: route.routeId })
        if (next.ok) {
          const registration = await ports.register({ ...input, quoteId: next.data.quoteId }, route.replans + 1)
          if (registration.ok) {
            reply = registration
            continue
          }
          if (registration.code !== 'route_not_live') return registration
        } else if (
          next.code !== 'route_not_live' &&
          next.code !== 'insufficient_depth' &&
          next.code !== 'route_no_candidate'
        )
          return next
      }
    }
    await delay(250)
    reply = await ports.get(route.routeId)
  }
  return reply
}
