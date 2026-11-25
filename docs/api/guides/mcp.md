# MCP setup

This guide describes behavior when the relevant feature and account are enabled. Check the [dated live evidence](../../../AGENTS.md#current-access-and-live-evidence) before relying on its availability.

An enabled MCP token can give an agent access to market and account reads and route explanations. An eligible trade-scoped agent token can also expose order tools. The server uses Streamable HTTP at the following endpoint.

```text
POST https://api.atreides.finance/mcp
Authorization: Bearer <your MCP token>
```

The endpoint takes `POST` only; a `GET` answers `405` with `Allow: POST`, and a request without a valid token answers `401` with `WWW-Authenticate: Bearer`. Use an MCP client that supports Streamable HTTP with a static authorization header.

## Get a token

- **Read token.** In API & Bots choose **Create key**, select **MCP token · read only**, name it and create it. Copy the token and the **MCP configuration** shown with it; the app shows the token once.
- **Trade token.** Use a trade-scoped MCP token issued for an enabled venue account. Sign in at [app.atreides.finance](https://app.atreides.finance/) with an invite and use the options shown in API & Bots. Trading tools depend on your account and rollout state.

Revoke a token in API & Bots when you no longer need it. It is scoped to the accounts and permissions granted to it.

## Connect a client

Claude Code:

```sh
claude mcp add --transport http atreides https://api.atreides.finance/mcp \
  --header "Authorization: Bearer <your MCP token>"
```

Any client with a JSON configuration for HTTP servers, for example a project `.mcp.json` file:

```json
{
  "mcpServers": {
    "atreides": {
      "type": "http",
      "url": "https://api.atreides.finance/mcp",
      "headers": { "Authorization": "Bearer <your MCP token>" }
    }
  }
}
```

Claude Desktop reads local servers from `claude_desktop_config.json`. To reach a remote endpoint with a header, run a bridge such as `mcp-remote` as the local command:

```json
{
  "mcpServers": {
    "atreides": {
      "command": "npx",
      "args": ["mcp-remote", "https://api.atreides.finance/mcp", "--header", "Authorization:${MCP_AUTH_HEADER}"],
      "env": { "MCP_AUTH_HEADER": "Bearer <your MCP token>" }
    }
  }
}
```

Save the configuration in the client's private settings, restart or reload the client and list its tools. The server name is `atreides`. Keep the token out of repositories.

## Read tools

A read token lists these tools: `market_snapshot`, `funding_compare`, `price_gaps`, `coverage`, `fee_quote`, `route_quote`, `product_help`, `my_positions`, `my_orders`, `my_fills`, `my_equity`, `my_fees`, `explain_fill`, `my_notices` and, for an account that can trade, `prepare_ticket`. Some are listed only while their feature is on for you. `explain_route` is listed beside them: it explains a routed order from stored data; see [route explanations](route-explanations.md).

Every result is JSON, sent both as structured content and as text, so clients that forward only text still see it. A venue that could not be read is named in the result and is never shown as an empty list.

## Trade tools

An agent token lists four more tools, and only while at least one of its accounts has the per-account setting `agent.trading` turned on. Use discovery to see whether the order tools are available for your token.

| Tool             | What it does                                                                                                                                                        |
| ---------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `place_order`    | Places one `limit`, `market` or reduce-only `stop_market` order on a named account, or a routed order when `route` is given. `dryRun: true` checks without placing. |
| `cancel_order`   | Cancels one resting order by its `clientOrderId`.                                                                                                                   |
| `cancel_all`     | Cancels every resting order of one account, or of one instrument. It never closes a position.                                                                       |
| `close_position` | Closes the position of one instrument on one account with a reduce-only market order for exactly its size. There is no close-everything call.                       |

The input is strict. A size is a decimal string in the instrument's base coin. There is no leverage field and no way to say "all"; whatever the model reads out of your words that the shape cannot hold, such as "10x" or "all in", is refused as invalid arguments and asked back, never resized. Send your own `clientOrderId` so that a retry after a timeout returns the same order instead of placing a second one. A routed order takes `route: { "kind": "best", "group": "perp:BTC" }` with optional `maxSlippageBps`, `split`, `maxLegs` and `allowPartial`.

The order tools call the same checks as the API: the key's limits, the account limits and the quota all apply. A refusal names the limit that stopped the order; report it and do not retry with a smaller size. See [refusals](refusals.md).

## Try it

Ask the agent: "What is the mark price and funding rate of hyperliquid:ETH?" It calls `market_snapshot`. With an agent token and `agent.trading` on: "Check a post-only buy of 0.001 hyperliquid:BTC at 50000 on my account, dry run only." It calls `place_order` with `dryRun: true` and reports the checks.

## Token limits

Calls are metered per token. When a token uses its allowance the server answers `mcp_quota_exceeded` with the limit, the window and the time it resets. The token only authorizes the accounts and operations it was issued for. If a tool returns a ticket link, open it in the browser, review the action and sign there.
