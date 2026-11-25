# Router on your size

Use the public preview endpoint to compare a BTC or ETH perpetual order against live books for Hyperliquid, Aster and Lighter. The calculator uses public entry-tier fees and skips account margin reads. It reserves, signs and places nothing.

## Request a preview

Send `POST /v1/route/preview` to the public API with JSON and no credentials. Anonymous calls are rate-limited per IP.

| Field | Value |
| --- | --- |
| `group` | `perp:BTC` or `perp:ETH`. |
| `side` | `buy` or `sell`. |
| `notional` | USD size from 1,000 to 20,000,000 as a decimal string. |
| `fundingHorizonHours` | Optional holding period from 1 to 8,760 whole hours. |

```sh
curl -fsS 'https://api.atreides.finance/v1/route/preview' \
  -H 'Content-Type: application/json' \
  --data '{"group":"perp:ETH","side":"buy","notional":"100000","fundingHorizonHours":24}'
```

## Read the response

Check the HTTP status and the response's `ok` field. A successful preview carries `indicative: true`, `executable: false`, its `asOf` time and a status of `smart_route`, `single_venue` or `no_quote`.

- Read the venue, size and expected price for each planned leg in `smartRoute`. The planner returns all-in price, venue fees, Atreides fees, expected funding when known, and the estimated overhead and gain that informed the split.
- Read `bestFullSingle` for the full requested size when available. Report the signed USD and basis-point amounts from `saving` as returned.
- Read the book age for each available venue under `venues[].book.ageMs`. The `skip` and `skipped` fields describe excluded venues. Keep unknown values as null.
- Historical comparisons at 10,000, 100,000 and 1,000,000 USD come from `GET /v1/route/benchmark` matched on group, side and size. Read the sample count and window with the estimate. The benchmark contains historical plan-time estimates. The preview comes from the books read for your request.

The [preview operation](../reference.html#previewRoute) and [benchmark operation](../reference.html#getRouteBenchmark) show the full response shapes. The [router model](../../ROUTER-MODEL.md) explains cost and split decisions.
