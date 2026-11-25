# Refusals and error codes

Every call answers with an envelope. A success is `{"ok":true,"data":…}`. A refusal is:

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

- Decide on `code`, never on the message text. Codes are stable; messages are written for people and carry the numbers of this refusal.
- Check `ok`, not only the HTTP status. A decided refusal of an order can arrive with HTTP 200 and `ok: false`; transport and authentication failures use 4xx and 5xx.
- `retryable` says whether sending the same request again can succeed. When it is true, wait the time the answer gives (`retryAfterMs` in `details`) and keep the same `clientOrderId`. When it is false, change something first.
- Keep the `requestId`. Support finds the request with it.
- `details` names what blocked the request: the limit, the venue, the field.

The complete generated table, with the HTTP status, the retry flag and the message of every code, is on the [error codes](../reference.html#schema-ErrorCode) page. The codes below are the ones an order meets most often.

## an-order-was-refused-before-it-left

These come from the checks every order passes. None of them reached the venue, so nothing needs reconciling. Fix the cause and send a new order.

| Code                                                                                                                           | What to do                                                                                                                                            |
| ------------------------------------------------------------------------------------------------------------------------------ | ----------------------------------------------------------------------------------------------------------------------------------------------------- |
| [`limit_exceeded`](../reference.html#schema-ErrorCode)                                                                                     | A limit of your key, account or slot would be exceeded. `details` names the limit and its value. Lower the size or wait; do not retry the same order. |
| [`tick_size`](../reference.html#schema-ErrorCode), [`lot_size`](../reference.html#schema-ErrorCode), [`min_notional`](../reference.html#schema-ErrorCode)                       | The price is not on the tick, the size is not a multiple of the lot, or the notional is below the minimum. The message gives the nearest valid value. |
| [`price_band`](../reference.html#schema-ErrorCode), [`price_out_of_band_venue`](../reference.html#schema-ErrorCode)                               | The price is too far from the mark or outside the venue's collar.                                                                                     |
| [`insufficient_margin`](../reference.html#schema-ErrorCode), [`insufficient_balance`](../reference.html#schema-ErrorCode)                   | The account does not have the margin or balance the order needs.                                                                                      |
| [`venue_closed`](../reference.html#schema-ErrorCode), [`kill_switch_active`](../reference.html#schema-ErrorCode), [`key_frozen`](../reference.html#schema-ErrorCode) | Opening orders are stopped for the venue, the account or the key. Closes and cancels still go.                                                        |
| [`scope_missing`](../reference.html#schema-ErrorCode), [`account_not_bound`](../reference.html#schema-ErrorCode)                                     | The key lacks trade scope or is not bound to the account.                                                                                             |
| [`stale_price`](../reference.html#schema-ErrorCode), [`market_data_unavailable`](../reference.html#schema-ErrorCode)                             | A fresh price is not available. Retry after the time given.                                                                                           |
| [`route_no_candidate`](../reference.html#schema-ErrorCode), [`route_requote`](../reference.html#schema-ErrorCode)                                   | No connected account can take a routed order now, or prices moved since the quote. Quote again.                                                       |

Run the order with `"dryRun": true` first to see these refusals without placing anything. See [check an order](market-and-orders.md#check-an-order-before-you-send-it).

## the-order-reached-or-may-have-reached-the-venue

| Code                                                                                                                                                   | What to do                                                                                                |
| ------------------------------------------------------------------------------------------------------------------------------------------------------ | --------------------------------------------------------------------------------------------------------- |
| [`not_placed`](../reference.html#schema-ErrorCode)                                                                                                                     | The order never reached the venue and is final. Send it again with a new client order id.                 |
| [`unknown_outcome`](../reference.html#schema-ErrorCode)                                                                                                           | The order may have reached the venue. Do not resend it. Read it by its client order id until it is final. |
| [`duplicate_client_order_id`](../reference.html#schema-ErrorCode)                                                                                       | That id is already in use. Read the existing order by that id.                                            |
| [`idempotency_conflict`](../reference.html#schema-ErrorCode), [`idempotency_in_progress`](../reference.html#schema-ErrorCode)                                   | The key was used with a different request, or the first attempt is still running.                         |
| [`post_only_would_take`](../reference.html#schema-ErrorCode), [`fok_unfilled`](../reference.html#schema-ErrorCode), [`reduce_only_rejected`](../reference.html#schema-ErrorCode) | The venue refused the order's type for the current book or position.                                      |
| [`venue_rejected`](../reference.html#schema-ErrorCode)                                                                                                             | The venue refused it and gave its own text.                                                               |

The retry rule for orders is short: a retry after a timeout uses the same `clientOrderId`, so it can only ever return the same order. A new order after a final refusal uses a new one.

## authentication-and-rate-limits

[`unauthenticated`](../reference.html#schema-ErrorCode) means no valid credentials. [`signature_invalid`](../reference.html#schema-ErrorCode), [`timestamp_out_of_window`](../reference.html#schema-ErrorCode) and [`nonce_reused`](../reference.html#schema-ErrorCode) mean the signature, the clock or the nonce is wrong; see [authentication](authentication.md). [`rate_limited`](../reference.html#schema-ErrorCode) carries `retryAfterMs`; see [errors and rate limits](budgets.md) and the [default rate limits](../reference.html).

## mcp-and-the-assistant

The MCP order tools return the same refusals: a tool error carries the `code` and `message` as structured content. When a tool says which limit blocked an order, report it to the user and do not retry with a smaller size.
