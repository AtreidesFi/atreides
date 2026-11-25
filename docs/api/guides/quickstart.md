# Quickstart

This page takes you from a first request to a checked order in four steps. The first step uses the public production API at `https://api.atreides.finance`. Signed steps require an eligible connected account.

## 1. Read the market without a key

Market data, venues, instruments and funding are public.

```sh
curl -s "https://api.atreides.finance/v1/market-snapshot?instrumentId=hyperliquid:BTC"
```

```json
{
  "ok": true,
  "data": {
    "rows": [
      {
        "instrument": "hyperliquid:BTC",
        "venue": "hyperliquid",
        "state": "d-only",
        "mark": "60990",
        "mid": null,
        "spreadBps": null,
        "depth10bp": null,
        "depth25bp": null,
        "fundingRate": "0.0000125",
        "fundingIntervalH": 1,
        "fundingApr": "0.1095",
        "volume24h": { "amount": "3334047867.67194", "asset": "usdc" },
        "openInterest": { "amount": "2330706413.274593901", "asset": "usdc" },
        "takerFee": null,
        "makerFee": null,
        "age": { "at": "2026-11-22T21:48:37.302Z", "ageMs": 22313, "daysAgo": "0" }
      }
    ]
  }
}
```

Every answer is `{"ok":true,"data":…}` or `{"ok":false,"code":…,"message":…,"retryable":…}`. A figure the API cannot give is `null`, never zero. The `state` field is the gate state of the venue unit; `open` accepts orders. `d-only` accepts them only for internal accounts, which is the state of the mainnet units while access is limited. `closed`, `reduce-only` and `paused` limit what you can do. [Venues](https://api.atreides.finance/v1/venues) lists the current states. More public calls are in [market, quote and order endpoints](market-and-orders.md).

## 2. Get a key

Create a key in API & Bots after connecting an eligible account. Read access is enough for everything except placing and cancelling orders. See [get a key](authentication.md#get-a-key). Save the key file as `atreides-key.json`, readable only by you.

## 3. Make one signed read

```sh
node sign.mjs atreides-key.json GET /v1/whoami \
  | curl -s -K - https://api.atreides.finance/v1/whoami
```

The `sign.mjs` script is on the [authentication page](authentication.md#sign-a-curl-request). The answer lists the scopes of the key and the account IDs it is bound to. Use one of those account IDs below.

## 4. Check an order

Add `"dryRun": true` to an order and the API runs the checks (state, fee, price band, lot and tick, limits, frozen mode) and answers what would happen. Nothing is stored, signed or sent, and no budget is used. Remove the flag only when you mean to trade. The [SDK quick-start](sdk.md) shows the call in TypeScript and Python, and the [endpoints page](market-and-orders.md) shows it in `curl`.

Agents with an eligible token and the required tool can do the same through [MCP](mcp.md).

If a call is refused, the `code` says why. See [refusals and error codes](refusals.md).
