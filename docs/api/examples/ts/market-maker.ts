import type { Client, PlaceOrderInput } from '@atreides/sdk'

export type Quote = Pick<PlaceOrderInput, 'accountId' | 'instrumentId' | 'price' | 'size' | 'side'>

// Supply post-only quotes within your test account's limits. Generate fresh quotes for each iteration.
export async function quoteOnce(client: Client, quote: Quote): Promise<string> {
  const placement = client.orders.place({ ...quote, type: 'limit', tif: 'post_only' })
  let initial: Awaited<typeof placement.result>
  try {
    initial = await placement.result
  } catch (cause) {
    throw new Error(`Look up ${placement.clientOrderId} before quoting again`, { cause })
  }
  if (!initial.ok) throw new Error(`${placement.clientOrderId}: ${initial.code}`)
  try {
    const read = await client.orders.get(quote.accountId, placement.clientOrderId)
    if (!read.ok || read.data.state === 'unknown') throw new Error('Order state is unresolved')
    if (!['filled', 'cancelled', 'rejected'].includes(read.data.state)) {
      const cancel = await client.orders.cancel(quote.accountId, placement.clientOrderId)
      if (!cancel.ok || !['filled', 'cancelled'].includes(cancel.data.state))
        throw new Error('Cancellation is unresolved')
    }
  } catch (cause) {
    throw new Error(`Resolve ${placement.clientOrderId} before quoting again`, { cause })
  }
  return placement.clientOrderId
}

export async function marketMakingLoop(client: Client, quotes: AsyncIterable<Quote>): Promise<void> {
  for await (const quote of quotes) await quoteOnce(client, quote)
}
