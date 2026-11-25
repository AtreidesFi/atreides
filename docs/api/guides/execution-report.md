# Execution report

Every order carries an `executionReport` once it has a fill or is final. It is computed on read from the order's own record, so a number is never an estimate: a figure the record cannot give is `null`, never zero.

| Field                                        | Meaning                                                                                                                                                                                             |
| -------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `arrivalMid`, `arrivalMark`, `arrivalSource` | The book (`book` or `cache`) or, when none was held, the mark (`mark`, no `arrivalMid`) at the moment the API accepted the request. `arrivalSource` is `null` when the order has no arrival record. |
| `vwap`                                       | Average fill price, weighted by size.                                                                                                                                                               |
| `slippageVsArrivalBps`, `slippageVsMidBps`   | Basis points of the VWAP against the arrival price and against the arrival mid. Positive is worse for you, for a buy and a sell alike.                                                              |
| `venueFeeUsd`, `ourFeeUsd`                   | The venue's fee and ours in USD at each fill's time. `null` when any fill has no fee figure.                                                                                                        |
| `decisionMs`, `msToAck`, `msToFirstFill`     | Decision time is gateway receipt to signing or send. Ack and first-fill time are measured from the gateway send to the corresponding observation.                                                   |
| `provisional`                                | `true` while the order is not final or a fill is not yet confirmed.                                                                                                                                 |

Read it with `GET /v1/orders/{clientOrderId}` and `GET /v1/orders`, and on the `user` stream in the `order.updated` event of a final order. A routed parent keeps its own report (`GET /v1/route/{routeId}`: arrival price, slippage, fees by venue and ours, saving against the best single venue, time to complete); each of its legs is an order with its own report.

## Route report

Read `GET /v1/route/{routeId}` for each confirmed plan, each leg and the execution report. A replan appends a plan. Field shapes, optionality and the full list of reasons come from the [OpenAPI reference](../reference.html).

| Field                                        | Meaning                                                                                                                                                                                                                                                                      |
| -------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `plans[].splitDecision`                      | The selected single or split route, the reason, the maximum venue count and the configured margin. When a split was evaluated, `considered` carries the exit cost, forecast funding delta, fragmentation cost and decision threshold in basis points.                        |
| `plans[].legs[].venueFeeEvidence`            | The fee source used for the leg. `account` carries an observed account schedule. `public_table` carries the published schedule. The object includes the rates as fractions of notional and the observation time when available.                                              |
| `plans[].legs[].latency`                     | The model inputs and the resulting estimated price drift. `ackP50Ms` is observed median acknowledgement time, `venueDelayMs` is known venue delay, and `volatilityBps1s` is one-second volatility. The estimate appears as `driftBps` and `driftUsd`.                        |
| `report.savingParts.benchmarkNotionalUsd`    | The USD notional of the best single-venue benchmark at the size actually filled. It is the denominator for execution and funding basis points.                                                                                                                               |
| `report.savingParts.execution`               | Signed execution savings after evidenced fees and attributable refunds. `final` includes both USD and basis points. `pending` leaves both values null and includes reason codes in `exclusions`. Positive means lower paid cost for a buy or higher net proceeds for a sell. |
| `report.savingParts.fundingForecast`         | Estimated funding difference over the requested holding horizon. The forecast uses scheduled funding observations and returns null when the inputs are insufficient.                                                                                                         |
| `report.netSavingUsd`, `report.netSavingBps` | Compatibility fields for the same execution savings. The plan's own `savingUsd` and `savingBps` describe the plan estimate.                                                                                                                                                  |

`report.marginedPerVenue` is true when the route spans several venues. Each venue carries its own margin. The public sandbox slot report includes per-route split, fee-source and latency evidence and both saving parts. The slot's execution average weights the included routes by benchmark notional.

## Totals

`GET /v1/stats/execution?from&to&groupBy=key|venue|origin|instrument` sums the reports of the orders in the period (at most 31 days and 20000 orders). Each row carries `report`: filled notional in USD, slippage weighted by USD notional, fees, p50 and p95 of the three timings (nearest rank) and the fill and reject rates of final orders. A fee total is `null` when any filled order in the group lacks a fee figure. Narrow with `keyId` and `venue`. With `format=csv` the answer also carries `csv`: one row per order, empty cells for unknown figures, whose totals equal the rows'.

Aggregate slippage uses confirmed taker fills against the arrival book mid, weighted by taker USD notional. The public sandbox quality report counts mark-only observations separately in `markSlippageBps` and `arrivalFromMark`. The fill rate is total filled size divided by total ordered size for final orders. The public maker figures use confirmed maker size and the passive portion of maker-eligible orders. The per-order CSV describes the order as a whole.

Latency requires two stamps from the same monotonic clock. Observations that lack comparable stamps remain unmeasured. The public latency report counts invalid, pending and unattempted observations separately. A recorded ack timeout contributes its deadline in milliseconds to the percentiles. Arrival mid and mark require a snapshot at API receipt.

## Plain order comparisons

The public sandbox saving report compares a plain order's confirmed fill price and fees with the router's
projected price and fees for the same size. The comparison excludes gas and forecast funding. A negative
`couldHaveSavedUsd` means routing would have cost more. The saving report also publishes the number
of negative comparisons and their signed total.

## Public sandbox aggregates

The summary, saving, quality, latency, routing, reliability and slot reports include an `excluded` array of `{reason, count}` entries. The reason identifies the metric and why a sample could not be used. Counts use the same scope as the metric. A single order can be counted under more than one metric, and a latency count can describe a placement rather than an order.

Volume-weighted execution savings use positive benchmark notional in USD. The other volume-weighted means use positive filled notional in USD. Each placement latency uses that placement's volume, and route completion uses the routed order's volume. A missing or zero weight excludes the sample from the weighted mean. A known sample can still contribute to the median. A weighted mean is `null` when no sample has a positive weight. The reliability incident rate uses one observation per order. It is 1 when the order has at least one stuck, duplicate, orphan or refusal flag, and 0 otherwise.
