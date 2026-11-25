# Gateway and direct orders

In gateway mode, send a normalized order to the API for a bound account. The API and gateway check the trade grant, instrument, limits and venue state. A gateway signer signs the venue request where the venue and account support it. Some venues use a user-signed relay.

In direct mode, the bot signs with its venue key and sends the request to the venue. Link the account and fetch its fee schedule first. Keep the venue's required attribution field in the signed request. Read the approval and exact venue fee step before encoding it.

Use the original client order ID after a timeout or an unknown outcome. Poll the corresponding order and fill state before placing another order. Use the same idempotency key and body when retrying an idempotent request. Sign each retry with a fresh timestamp and nonce.

See the [venue gate states](https://api.atreides.finance/v1/venues) for the supported class, key place and mode and the [dead-man guide](deadman.md) for automatic cancellation.

## Terms

When the operator requires a Terms version, `GET /v1/me` shows it as `termsVersionRequired` and the version you accepted last as `termsAcceptedVersion`. Until you accept exactly the required version, every opening order (any key, a session, a ticket or a route) is refused `terms_not_accepted`; closes (`reduceOnly: true`), cancels and reads still work. A person accepts in the app, or a session calls `POST /v1/me/terms` with `{ "version": "<required>" }` and an `Idempotency-Key`; a key cannot accept for you.
