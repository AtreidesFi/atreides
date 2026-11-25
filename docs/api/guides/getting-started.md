# Getting started

This is the execution layer for systematic trading. You can read public market data and preview a route anonymously. With access, venue connections can be used from the API, SDKs, web terminal and enabled agent surfaces.

## Venue availability

The real-money evidence covers limited owner runs on Hyperliquid, Aster and Lighter. The other integrations have different levels of implementation and validation. Use [the dated status](../../../AGENTS.md#current-access-and-live-evidence) and [the venue states](https://api.atreides.finance/v1/venues) to check access for your mode and account.

## Direct, gateway and terminal modes

Three modes decide who signs. In direct mode your bot signs with its own venue key and sends the request to the venue. In gateway mode you send a normalized order to the API and a gateway signer signs it where the venue supports it. In terminal mode you trade from the web terminal and the key stays in the browser, wallet, signer or enclave. Pick the mode per account; the [gateway and direct orders guide](execution.md) explains the differences.

## First steps when you have access

1. Enter an invite code and sign in with your wallet at [app.atreides.finance](https://app.atreides.finance/). The browser signs a one-time challenge; no password is created.
2. Open Connections and link a venue account. Each connection shows what the key can do, what it cannot do and who can move your funds. See [connect Hyperliquid](connect-hyperliquid.md).
3. Open API & Bots and create a key. Choose read scope for market and account queries and add trade scope only for a bot that places orders. Bind the key to the accounts it may use and set its [limits](limits.md).
4. Make a signed read, then check an order with a dry run, which stores nothing. The [quickstart](quickstart.md) lists the steps and the [SDK quick-start](sdk.md) shows them in TypeScript and Python. Testnet keys start with `test_`; mainnet keys start with `live_`.
5. Arm the [dead-man switch](deadman.md) before a bot runs unattended.
6. If your account offers agent trading or hosted bots, review those permissions in API & Bots. See [MCP setup](mcp.md) for token discovery and order dry runs.

Every order sent through the API, SDK or MCP is an algo order; orders from the terminal and tickets are manual orders. The two pay different fees; see [fees](fees.md).
