# Fees

This guide describes behavior when the relevant feature and account are enabled. Check the [dated live evidence](../../../AGENTS.md#current-access-and-live-evidence) before relying on its availability.

You pay two separate fees on an order: the venue's own fee, which the venue charges and we never touch, and our fee, which is added on top on venues that carry our fee code. The configured base rates are 0.1 basis points for algo orders from the API, SDK and MCP and 1 basis point for manual orders from the terminal and tickets. Quote the exact fee before you send an order.

## Our fee

Our fee depends on where the order came from. Orders sent through the API, the SDK or MCP are algo orders. Orders placed from the web terminal or a ticket are manual orders. On Hyperliquid, Aster and the other add-on venues the base rates are 0.1 basis points for algo orders and 1 basis point for manual orders, on both the maker and the taker side. Above 100 million USD of trailing 30-day volume (tier t1) the algo rate is lower on venues that can encode a finer step. Hyperliquid's smallest step is 0.1 basis point, so its algo rate stays at 0.1.

## See the fee first

Quote the applicable fee before you send an order with the fee quote operation (`quoteFee`, `client.fees.quote`); the answer splits the venue's part from ours. A trade ticket shows both parts and the all-in price before you confirm, and the fees view shows what you paid by venue and period.

## Fees and smart routing

Smart routing compares venues on the all-in price: price, venue fee, our fee and funding. A route quote reports the saving against the best single venue after every fee, so a route that does not save money says so. See [smart routing](smart-routing.md).

Market previews use the fresh mark, or the book mid when the mark is stale. A marketable limit uses qualifying book levels and prices any unfilled remainder at the limit. The band reports available depth and does not change the preview size or reference. Each fee amount is rounded after conversion. The all-in total uses the exact combined cost, rounded down for buys and up for sell proceeds.
