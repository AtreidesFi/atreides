# Errors and rate limits

Check both the HTTP status and the ok field in the response envelope. A decided refusal can arrive with HTTP 200 and ok false. Use the stable code to decide what to do and retain the request ID for support. See the [error codes](../reference.html#schema-ErrorCode) and [default rate limits](../reference.html).

Use the retry timing returned by the API. On a venue-specific rate refusal, read the account's venue budget and reduce the request rate for that account. Hyperliquid has address-scoped venue limits as well as IP-scoped limits. Sending more traffic through another connection can still spend the same account's budget.

Prefer subscriptions over repeated polling when they provide the updates you need. Budget order submission separately from lookups and protect close and cancel traffic.
