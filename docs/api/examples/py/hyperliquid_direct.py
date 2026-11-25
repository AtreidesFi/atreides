from atreides import Client
from atreides.direct.hyperliquid import BatchResult, Exchange, HyperliquidDirect, Order


def place_direct_testnet(client: Client, account_ref: str, testnet_exchange: Exchange, order: Order) -> list[BatchResult]:
    # Construct the official Exchange against its testnet URL before calling this function.
    direct = HyperliquidDirect(testnet_exchange)
    direct.start(client, account_ref)
    return direct.place_batch([order])
