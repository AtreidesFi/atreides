# Atreides for agents

Atreides is an execution platform for systematic traders. The [app](https://app.atreides.finance/) and [production API](https://api.atreides.finance/) expose market data, connected accounts, order tools and execution reports. The TypeScript and Python SDKs sign origin-bound API requests. The router walks eligible books, compares execution costs and records later fills and fees. [Router model](docs/ROUTER-MODEL.md) · [API reference](docs/api/reference.html)

## What Atreides gives you

- **Three live venues, one API.** Hyperliquid, Aster and Lighter trade real money through Atreides from servers in AWS Tokyo, next to the venues. One order format, one set of limits, one execution report. [Venue catalogue](https://api.atreides.finance/v1/venues)
- **A router that counts the whole cost.** It walks the live books of every connected venue and compares price impact, fees and funding before it picks a venue or splits the order. Try it on any size from 1,000 to 20,000,000 USD with no account. [Preview guide](docs/api/guides/router-on-your-size.md)
- **Agents trade through MCP.** An AI agent with a trade-scoped token can dry-run, place, cancel and close orders on the user's connected accounts, inside the limits the user set. [MCP setup](docs/api/guides/mcp.md)
- **Hosted bots.** A user's bot runs on Atreides servers beside the venues under its own scoped key, with caps, expiry and a dead-man switch.
- **Non-custodial.** Funds stay at the venues. Keys can trade and can never withdraw.
- **Invite-only.** Enter an invite code with your wallet at [app.atreides.finance](https://app.atreides.finance/), then connect a venue account. Public data and the router preview need no invite.

## Try Atreides

1. Read an instrument anonymously or ask the router for a plan. Use the [public endpoints](#public-evidence-endpoints) below.
2. With an invite, connect a wallet at [app.atreides.finance](https://app.atreides.finance/) and set the permissions and limits of each connection. Start with a [signed SDK read](docs/api/guides/sdk.md) or the [REST quick-start](docs/api/guides/quickstart.md).
3. For agent actions, use an issued token and [discover the MCP tools](docs/api/guides/mcp.md). Dry-run the exact order the user asked for before sending it. Get the user's explicit authorization for any action that places, changes or cancels orders. Keep credentials local.

## Public evidence endpoints

Use `https://api.atreides.finance` and send no credentials for the endpoints below. Read HTTP status and the JSON `ok` flag. Keep the observation timestamp, filters, sample count and units with a reported number. A preview is a model output, a replay is historical and a fill is execution evidence.

### Market data, model inputs and public records

| Operation | Evidence |
| --- | --- |
| `GET /v1/venues` and `GET /v1/venues/{venue}` | Gate state by mode, class and key place, fee model and available reachability or latency readings. |
| `GET /v1/instruments` and `GET /v1/instruments/{instrumentId}` | Instrument IDs, price tick, lot, minimums and other trading parameters. |
| `GET /v1/markets/{instrumentId}/book` | Bid and ask prices, sizes and observation time. |
| `GET /v1/markets/{instrumentId}/trades` | Recent public market prints. |
| `GET /v1/markets/{instrumentId}/candles` | OHLC, volumes and counts by bar. Supply a `tf` such as `1h` and check `complete`. |
| `GET /v1/markets/{instrumentId}/funding` | Settled funding rates, settlement times and intervals. |
| `GET /v1/market-snapshot` | Mark, mid, spread, depth, funding, fee fields, volume, open interest and row age. Example query `?instrumentId=hyperliquid:BTC`. |
| `GET /v1/compare/funding` and `GET /v1/compare/prices` | Cross-venue funding and price comparisons, with available fee assumptions. Example query `?asset=ETH`. |
| `GET /v1/coverage` | Available history by venue and instrument. |
| `GET /v1/help` | Product help with available data values for the requested topic. |
| `POST /v1/route/preview` | Credentialless indicative plan, book ages, fees, funding estimates, split decision and comparison with the best full-size single venue when available. Send JSON such as `{"group":"perp:BTC","side":"buy","notional":"10000","fundingHorizonHours":24}`. |
| `GET /v1/route/benchmark` | Stored route-replay results for the requested group and window, for example `?group=perp:BTC&days=30`. |
| `GET /v1/public/volume/daily` | Fill-derived volume, trade count and fees by venue for the UTC `date`. Check completeness and reconciliation fields. |
| `GET /v1/public/volume/fills` | Paginated public fill rows for UTC `date` and `venue`. Check these against the daily totals for the same scope. |
| `GET /v1/stream` (WebSocket upgrade) | Public `venues`, `research_cards`, `book:{instrumentId}`, `trades:{instrumentId}`, `candles:{instrumentId}:{tf}` and `mark:{instrumentId}` events. The `user` channel requires authorization. |

The contract also describes the following public reads, subject to their feature and data availability. Their presence in the reference does not establish live use.

- `GET /v1/competitions`, `GET /v1/competitions/{competitionId}`, `GET /v1/competitions/{competitionId}/standings`, `GET /v1/competitions/{competitionId}/snapshots` and `GET /v1/competitions/{competitionId}/snapshots/{day}/{kind}` expose competition parameters, standings and snapshots when published.
- `GET /v1/passport/{token}` exposes a deliberately shared record, when available. Use a token provided for that purpose.
- `GET /v1/vaults`, `GET /v1/vaults/matrix`, `GET /v1/vaults/{id}`, `GET /v1/vaults/{id}/index`, `GET /v1/vaults/{id}/log`, `GET /v1/vaults/{id}/exposure` and `GET /v1/vaults/{id}/records` describe vault capabilities, published history and record evidence.
- `GET /v1/research-cards` and `GET /v1/research-cards/{slugOrId}` expose published research and any recorded metrics. Treat research, simulations and live execution as separate samples.
- `GET /v1/security/status` and `GET /v1/attestation` describe available security and attestation evidence. A recorded identifier alone does not establish live enclave signing.
- `GET /v1/referrals/codes/{code}` resolves a referral code. A valid code does not establish a payout.

Use the [OpenAPI bundle](spec/openapi.yaml) for request parameters, response schemas and access requirements. Account holdings, account-specific fee quotes, order records and MCP tools require the corresponding credentials. Agents should keep credentials local and ask for user authorization before any operation that places, changes or cancels an order.
