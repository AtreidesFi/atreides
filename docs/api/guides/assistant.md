# The assistant

This guide describes behavior when the relevant feature and account are enabled. Check the [dated live evidence](../../../AGENTS.md#current-access-and-live-evidence) before relying on its availability.

The assistant lives in the command palette: press Cmd K, type a question and choose Ask. It also answers in a linked Discord or Telegram chat.

## What the assistant does

It explains the product and the API from this documentation, reads your own setup (connections, API keys and their scopes, limits and preferences) and tells you exactly what is missing before you can trade, reads market data, and prepares a ticket for you to review. In the Lab it can draft a strategy for you to open in the builder, read and change.

## What the assistant never does

It never acts on its own: it answers only when you ask. Review the exact action and confirm it in the product before submitting an order. It never asks for a private key, recovery words, an API secret or a password, and it does not give personalised investment advice. If it does not know, it says the documentation does not cover it.

## Use your own agent instead

To use your own agent instead, create an MCP token and follow [MCP setup](mcp.md). Your agent lists the same read tools the assistant uses: market snapshot, funding comparison, price gaps, coverage, fee quote, route quote, your positions, orders, fills, equity and fees, and prepare ticket.
