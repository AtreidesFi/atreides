import type { Client } from '@atreides/sdk'
import { type DirectOrder, HyperliquidDirect, type HyperliquidDirectOptions } from '@atreides/sdk/direct/hyperliquid'

// The existing bot supplies its signer and one-shot exchange transport. The SDK sets the builder field before signing.
export async function placeDirectTestnet<Reply>(
  client: Client,
  accountRef: string,
  options: Omit<HyperliquidDirectOptions<Reply>, 'network'>,
  order: DirectOrder,
) {
  const direct = new HyperliquidDirect({ ...options, network: 'testnet' })
  await direct.start({ client, accountRef })
  return direct.placeBatch([order])
}
