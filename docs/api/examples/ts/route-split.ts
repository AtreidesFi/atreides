import type { Client, PlaceLeg, RouteInput } from '@atreides/sdk'

export async function routeSplit(client: Client, input: RouteInput, clientRouteId: string, placeLeg?: PlaceLeg) {
  const request = { ...input, split: true, maxLegs: Math.min(input.maxLegs ?? 4, 4) }
  const quote = await client.route.quote(request)
  if (!quote.ok) throw new Error(quote.message)
  const placement = client.route.execute({ ...request, quoteId: quote.data.quoteId, clientRouteId }, placeLeg)
  const result = await placement.result
  if (!result.ok) throw new Error(result.message)
  return result.data
}
