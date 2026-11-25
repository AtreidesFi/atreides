# Smart routing

This guide describes behavior when the relevant feature and account are enabled. Check the [dated live evidence](../../../AGENTS.md#current-access-and-live-evidence) before relying on its availability.

A route chooses among the linked accounts that can trade the same group. Use `perp:BTC` for BTC perpetuals across venues. Pass the account IDs you want the router to use and either a base size or a USD notional. Set `split: true` to allow more than one venue.

1. Call `client.route.quote` with the order. Review the proposed legs, the best single venue, and the all-in saving after fees.
2. Send the same order with the quote ID and a client route ID you have saved locally. If the quote has moved outside the confirmed terms, obtain and review a new quote before retrying. For a transport failure, retry the same request with the same client route ID or check the existing parent.
3. Use `route.execute` to wait for the final parent. Supply a `legPlacer` or `leg_placer` for a client-executed plan. Direct legs use the reserved venue client order ID and the quoted fee rate. Gateway legs can use the API, a relay signer, or a wallet signer as specified by each reservation.
4. Use `route.get`, `route.list`, and `route.cancel` to inspect parents and stop a working route. Keep the parent ID with your order records. Inspect the final report for actual fills, net savings from the plan, and any open leaves.

Try the [TypeScript route-split example](../examples/ts/route-split.ts) or [Python route-split example](../examples/py/route_split.py) with linked testnet accounts.

To see why a route went where it did, read [how routing decisions are explained](route-explanations.md).
