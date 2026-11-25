import { type Client, createClient } from '@atreides/sdk'
import { readKeyFile } from '@atreides/sdk/keys/node'

export async function stagingClient(origin: string, keyPath: string): Promise<Client> {
  const parsed = new URL(origin)
  if (parsed.protocol !== 'https:' || parsed.origin !== origin)
    throw new Error('Use the configured testnet HTTPS origin')
  const key = await readKeyFile(keyPath)
  if (!key.keyId?.startsWith('test_') || key.apiOrigin !== origin)
    throw new Error('Use a test key bound to that origin')
  return createClient({ baseUrl: origin, key })
}

export async function discoverVenues(client: Client) {
  const reply = await client.venues.list()
  if (!reply.ok) throw new Error(`${reply.code}: ${reply.message}`)
  return reply.data
}

type Place = Parameters<Client['orders']['place']>[0]

/** Place a resting limit order, then cancel it using the SDK-generated ID. */
export async function placeAndCancel(client: Client, order: Place) {
  const placement = client.orders.place(order)
  const placed = await placement.result
  if (!placed.ok) throw new Error(`${placed.code}: ${placed.message}`)
  const cancelled = await client.orders.cancel(order.accountId, placement.clientOrderId)
  if (!cancelled.ok) throw new Error(`${cancelled.code}: ${cancelled.message}`)
  return { placed: placed.data, cancelled: cancelled.data, clientOrderId: placement.clientOrderId }
}

/** Close a position on the same instrument. Submit only the size you intend to close. */
export async function closePosition(
  client: Client,
  position: Pick<Place, 'accountId' | 'instrumentId' | 'size' | 'side'>,
) {
  const closed = await client.orders.place({ ...position, type: 'market', reduceOnly: true }).result
  if (!closed.ok) throw new Error(`${closed.code}: ${closed.message}`)
  return closed.data
}
