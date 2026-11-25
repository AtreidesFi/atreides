# How routing decisions are explained

A routed order goes to one venue or is split across several. The stored plan explains why an order went to each venue. The execution report uses the fills and fees recorded so far, and leaves missing amounts unset.

## where-the-data-lives

`GET /v1/route/{routeId}` returns the parent with its `plans` (one confirmed plan per attempt; a re-plan adds one), its `legs` and its `report`. The last plan is the one that ran. A plan holds:

- `legs`: the venue, account, size, expected price, fee rates, fees in USD, funding and the all-in price of each leg.
- `candidates`: every account the router considered. Each is either used, or dropped with a reason (`dropped.reason`, `dropped.detail`), and carries what the whole order would have cost on that venue alone (`alone.allInUsd`, `alone.allInPrice`) and its capacity.
- `bestSingle`: the best single venue for the whole order, with its all-in cost.
- `totals`, `savingUsd`, `savingBps`: what the plan costs all in and its estimated savings against the best single venue.

A signed read of a real routed order (a 0.006 ETH market buy, shortened):

```json
{
  "routeId": "01a20328-…",
  "group": "perp:ETH",
  "side": "buy",
  "type": "market",
  "mode": "gateway",
  "state": "done",
  "endReason": "filled",
  "filledSize": "0.006",
  "avgPrice": "1710.6",
  "plans": [
    {
      "legs": [
        {
          "legNo": 1,
          "venue": "hyperliquid",
          "instrumentId": "hyperliquid:ETH",
          "size": "0.006",
          "expectedPrice": "1712.6",
          "allInPrice": "1713.387796",
          "venueRate": "0.00045",
          "ourRate": "0.00001",
          "venueFeeUsd": "0.00462402",
          "ourFeeUsd": "0.000102756"
        }
      ],
      "totals": { "notionalUsd": "10.2756", "allInUsd": "10.280326776", "allInPrice": "1713.387796" },
      "bestSingle": { "venue": "hyperliquid", "allInUsd": "10.280326776" },
      "savingBps": "0",
      "savingUsd": "0",
      "candidates": []
    }
  ]
}
```

Only one account was connected, so there was nothing to compare: `candidates` is empty and the saving is zero.

## explain_route

The MCP tool `explain_route` takes the `routeId` and returns this reading of the stored route:

```json
{
  "routeId": "01a20328-…",
  "state": "done",
  "endReason": "filled",
  "planned": true,
  "used": [
    {
      "venue": "hyperliquid",
      "accountId": "01a1fcb3-…",
      "size": "0.006",
      "price": "1712.6",
      "feeSource": null,
      "latency": null
    }
  ],
  "considered": [],
  "bestSingle": { "venue": "hyperliquid", "allInUsd": "10.280326776", "allInPrice": "1713.387796" },
  "savingUsd": "0",
  "savingBps": "0",
  "allInUsd": "10.280326776",
  "splitDecision": null,
  "savings": null,
  "summary": "The plan used hyperliquid. The best single venue was hyperliquid at 10.280326776 USD all in."
}
```

| Field                    | Meaning                                                                                                                                                                        |
| ------------------------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `used`                   | The confirmed plan legs with venue, account, size, expected price, fee source and estimated latency cost.                                                                      |
| `considered`             | Every candidate: whether it was `used`, why it was dropped (`droppedBecause`, `droppedDetail`), what the order would have cost on it alone (`aloneAllInUsd`) and its capacity. |
| `bestSingle`             | The best single venue and its all-in cost.                                                                                                                                     |
| `savingUsd`, `savingBps` | The plan estimate against the best single venue.                                                                                                                               |
| `splitDecision`          | The stored choice of a single or split route and the reason for that choice.                                                                                                   |
| `savings`                | Execution savings, their evidence status and exclusions, and the separate funding forecast.                                                                                    |
| `summary`                | The venue choices and costs, the decision reason, and available execution savings and forecast funding.                                                                        |

When a route has no stored plan the summary says so and the other fields are empty. The tool is a read tool and needs only the route ID, which `GET /v1/route` and the placement answer give you.

Ask the agent "why did my last order go to Lighter?" and it calls `my_orders`, then `explain_route` with the order's `routeId`, and answers from `summary` and `considered`. Any drop reason it reports is a stored one. To explain a single fill rather than a route, use `explain_fill`, which gives the fee rate and tier and whether the venue day is reconciled.

## reading-the-saving

All-in means the price plus the venue fee, our fee, gas and funding over the horizon of the quote. The plan estimates savings against its best single-venue alternative. The execution report computes savings against the benchmark at the filled size and shows forecast funding separately. See the [execution report guide](execution-report.md). See [smart routing](smart-routing.md) for how to quote, place and follow a route.
