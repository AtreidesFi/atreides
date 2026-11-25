# Market, quote and order endpoints

All paths are under `https://api.atreides.finance`. Market data is public. Account and order calls are signed with a key; see [authentication](authentication.md). The examples below were run against the production API; ids are shortened with an ellipsis. The full contract is the [OpenAPI reference](../reference.html).

## venues-and-instruments

```sh
curl -s https://api.atreides.finance/v1/venues
curl -s https://api.atreides.finance/v1/instruments/hyperliquid:ETH
```

```json
{
  "ok": true,
  "data": {
    "instrumentId": "hyperliquid:ETH",
    "venue": "hyperliquid",
    "venueSymbol": "ETH",
    "cls": "perp",
    "underlying": "crypto",
    "shareRate": null,
    "base": "eth",
    "quote": "usdc",
    "feeAsset": "usdc",
    "chargedSide": "both",
    "tick": "0.1",
    "lot": "0.0001",
    "minSize": null,
    "minNotional": "10",
    "maxLeverage": 25,
    "status": "trading",
    "venueExtra": {
      "dex": null,
      "assetId": 1,
      "dexIndex": null,
      "growthMode": null,
      "szDecimals": 4,
      "marginModes": "cross,isolated",
      "onlyIsolated": false,
      "maxPriceDecimals": 2
    }
  }
}
```

An instrument ID is `<venue>:<venueSymbol>`. Prices must be on the `tick`, sizes a multiple of the `lot`, and notional at least `minNotional`; the order checks below refuse anything else with `tick_size`, `lot_size` or `min_notional`.

`GET /v1/venues` lists every venue with its gate state for each mode, key place and class.

## order-book-trades-candles-funding

```sh
curl -s https://api.atreides.finance/v1/markets/hyperliquid:BTC/book
curl -s "https://api.atreides.finance/v1/markets/hyperliquid:ETH/trades?limit=2"
curl -s "https://api.atreides.finance/v1/markets/hyperliquid:ETH/candles?tf=1h&limit=2"
curl -s https://api.atreides.finance/v1/markets/hyperliquid:ETH/funding
```

```json
{"ok":true,"data":{"instrumentId":"hyperliquid:BTC","bids":[{"px":"60997","sz":"7.15806"},{"px":"60996","sz":"1.76589"}],"asks":[{"px":"60998","sz":"1.98269"},{"px":"60999","sz":"0.68637"}],"at":1795384142094}}
{"ok":true,"data":[{"ts":1795384327083,"id":"1795384327083-756009168102122","px":"1722.2","sz":"0.0064","side":"sell"},{"ts":1795384325960,"id":"1795384325960-206803639892869","px":"1722.3","sz":"0.0958","side":"sell"}]}
{"ok":true,"data":[{"t":1795377600000,"open":"1714.6","high":"1720.4","low":"1710.9","close":"1720.1","volume":"6194.9236","notional":"10621121.7209","trades":3178,"buyNotional":"6202026.60498","sellNotional":"4419095.11592","complete":false,"origin":"ours"}]}
{"ok":true,"data":[{"settledAt":1794744000082,"rate":"0.0000002974","intervalHours":1},{"settledAt":1794747600029,"rate":"-0.0000141253","intervalHours":1}]}
```

The book shows the top levels only (five here). Amounts are decimal strings; times are Unix milliseconds. A candle with `"complete":false` is still forming. The candle call needs `tf`; without it the API refuses the request and names the missing field:

```json
{
  "ok": false,
  "code": "invalid_request",
  "message": "The request does not match the API spec: query must have required property 'tf'. Fix the field and send it again.",
  "retryable": false,
  "details": { "issue": "query must have required property 'tf'" },
  "requestId": "01a2053e-c078-7000-85f8-fd89938cbd97"
}
```

## market-snapshot-and-comparisons

`GET /v1/market-snapshot?instrumentId=…` returns mark, mid, spread, depth, funding, volume and open interest with the age of each row; see the [quickstart](quickstart.md) for a real answer. `GET /v1/compare/prices?asset=ETH` and `GET /v1/compare/funding` compare venues side by side and return an empty `rows` list when no venue has fresh data for the asset.

## your-account-orders-and-routes

These calls need a key with read scope. Sign each request as shown on the [authentication page](authentication.md#sign-a-curl-request).

```sh
node sign.mjs atreides-key.json GET "/v1/positions" \
  | curl -s -K - "https://api.atreides.finance/v1/positions"
node sign.mjs atreides-key.json GET "/v1/orders?limit=2" \
  | curl -s -K - "https://api.atreides.finance/v1/orders?limit=2"
```

```json
{"ok":true,"data":[{"accountId":"01a1fcb3-…","venue":"hyperliquid","read":{"ok":true,"asOf":"2026-11-22T21:51:44.701Z","value":[]}}]}
{"ok":true,"data":[{"orderId":"01a204ab-…","routeId":null,"clientOrderId":"…","accountId":"01a1fcb3-…","venue":"hyperliquid","instrumentId":"hyperliquid:ETH","cls":"perp","mode":"gateway","keyPlace":"signer","origin":"api","side":"buy","type":"limit","tif":"ioc","price":"1716.1","size":"0.0071","reduceOnly":false,"state":"rejected","filledSize":"0","avgPrice":null,"venueOrderId":null,"fee":{"rate":"0.00001","field":"1","asked":null},"reject":{"code":"not_placed","venueText":null},"finalReason":"not_placed", …}]}
```

A positions answer is per account. `read.ok` says whether the venue could be read; a venue that could not be read is reported, never shown as an empty list. An order carries its `state`, fills, the fee that applied and, for a refused or unplaced order, the reason in `reject` and `finalReason`. Read one order with `GET /v1/orders/{clientOrderId}?accountId=…`. `GET /v1/route` lists routed parents and `GET /v1/route/{routeId}` returns one with its stored plans; [route explanations](route-explanations.md) shows how to read them.

## check-an-order-before-you-send-it

`POST /v1/orders` takes a plain intent. The body needs `accountId`, `clientOrderId`, `instrumentId`, `side` and `type`, plus `size` (and `price` for a limit order). Sizes and prices are decimal strings. Add `"dryRun": true` to run the checks without placing anything. The call needs a key with trade scope, because it is an order call, but a dry run stores nothing, signs nothing and uses no budget.

```sh
BODY='{"accountId":"<account id>","clientOrderId":"docs-check-1","instrumentId":"hyperliquid:BTC","side":"buy","type":"limit","price":"50000","size":"0.001","tif":"post_only","dryRun":true}'
node sign.mjs atreides-key.json POST /v1/orders "$BODY" \
  | curl -s -K - -H 'Content-Type: application/json' --data-binary "$BODY" https://api.atreides.finance/v1/orders
```

The answer is a `DryRunResult`, in this shape (the fee figures depend on the account and the venue):

```json
{
  "ok": true,
  "data": {
    "wouldPlace": true,
    "price": "50000",
    "size": "0.001",
    "capPrice": "50000",
    "fee": { "venueUsd": null, "atreidesUsd": "<decimal>", "rate": "<decimal>" },
    "refusal": null
  }
}
```

`wouldPlace` is `false` and `refusal` names the refusal the order would get. A figure the check cannot give is `null`: the venue's own fee is not known to the check, so `venueUsd` is `null`. A dry run covers a single-venue order only. For a routed order, ask for a quote instead with `POST /v1/route/quote` (read scope); it returns the proposed legs, the best single venue and the all-in saving, and places nothing. `POST /v1/route/preview` gives an indicative quote with no account.

Send the same body without `dryRun` to place the order. Keep the `clientOrderId`: it is the idempotency key. Retrying after a timeout with the same id returns the same order instead of placing a second one, and `GET /v1/orders/{clientOrderId}` reads its state.

## cancel-an-order

```sh
BODY='{"accountId":"<account id>"}'
node sign.mjs atreides-key.json POST /v1/orders/docs-check-1/cancel "$BODY" \
  | curl -s -K - -H 'Content-Type: application/json' --data-binary "$BODY" \
    https://api.atreides.finance/v1/orders/docs-check-1/cancel
```

`POST /v1/cancel-all` with `accountId` (and optionally `instrumentId`) cancels every resting order of the account. Neither call closes a position. To close one, send a reduce-only order for its size. The [dead-man switch](deadman.md) cancels for you when a bot goes silent.
