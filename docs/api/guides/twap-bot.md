# Write a TWAP bot

A time-weighted average price (TWAP) bot splits a parent size into equal slices sent at equal intervals. This sketch uses the Python SDK with a read and trade key bound to one account: it calls `client.orders.place` once per slice with a `clientOrderId`, reads the order back with `client.orders.get` and arms the dead-man switch. Run it on testnet first.

## Python sketch

    import time
    from decimal import Decimal

    from atreides import Client, PlaceOrderInput, Refusal
    from atreides.keys import read_key_file

    key = read_key_file("bot.key")
    if not key.api_origin or not key.key_id or not key.key_id.startswith("test_"):
        raise ValueError("Use an origin-bound testnet key")
    client = Client(key.api_origin, key)
    ACCOUNT = "<your account id>"
    INSTRUMENT = "hyperliquid:BTC"
    TOTAL = Decimal("0.010")
    SLICES = 10
    PAUSE_SECONDS = 60

    armed = client.deadman.arm(ACCOUNT, 120_000)
    heartbeat = client.deadman.keep_alive([ACCOUNT], 120_000)
    try:
        for n in range(SLICES):
            order: PlaceOrderInput = {
                "accountId": ACCOUNT,
                "instrumentId": INSTRUMENT,
                "side": "buy",
                "type": "market",
                "size": TOTAL / SLICES,
                "slippageBps": 10,
            }
            placement = client.orders.place(order)
            if isinstance(placement.result, Refusal):
                raise RuntimeError(f"{placement.client_order_id}: {placement.result.code}")
            read = client.orders.get(ACCOUNT, placement.client_order_id)
            print(n, placement.client_order_id, getattr(read, "data", read))
            time.sleep(PAUSE_SECONDS)
    finally:
        heartbeat.stop()

## Rules for the bot

Keep the client order ID of every slice: after a timeout or an unknown outcome read the order by that ID before sending another slice, and never resend blindly. Stop on a refusal and read its stable code (see the [error codes](../reference.html#schema-ErrorCode)). Use a key limited to the account, a `maxOrderNotional` just above the slice notional and a `dailyNotional` just above the parent notional so a bug cannot trade more ([limits](limits.md)). Algo orders pay the algo fee ([fees](fees.md)). To spread a parent across venues use [smart routing](smart-routing.md) instead of slicing by hand.
