# Connect Hyperliquid

This guide describes behavior when the relevant feature and account are enabled. Check the [dated live evidence](../../../AGENTS.md#current-access-and-live-evidence) before relying on its availability.

Connect a Hyperliquid account in Connections: choose Hyperliquid, pick the mode and follow the steps. Your wallet signs; no secret is pasted into the page. You approve our builder fee (the fee approval) and, in gateway and terminal mode, an agent wallet that can trade for you but cannot withdraw.

## How many wallet prompts it needs

The number of wallet prompts depends on the mode. Direct mode needs one prompt, which approves our builder fee. Gateway and terminal need two chained prompts: first the builder-fee approval, then the approval of an agent wallet that is allowed to trade for you but cannot withdraw. One fee approval covers perpetuals, HIP-3 markets and spot, for API, SDK, MCP and terminal orders alike, so a tier change never needs a new signature.

## Checks before you sign

Before it asks for any signature, the connect page reads your Hyperliquid account without signing. It stops and tells you what to do when the account does not exist yet on Hyperliquid (deposit first, the page shows the deposit panel), when three named agent wallets already exist and none is ours (remove one at Hyperliquid), or when ten builders are already approved and ours is not (remove one approval in Hyperliquid's builder settings; the code is `builder_limit_reached`).

## After connecting

The connection card shows what the key can do, who can move funds and when the key expires. An opening order is refused with `fee_approval_missing` until the fee approval is in place; closes still go. Create an API key for your bot in API & Bots and bind it to this account. The gate state of each Hyperliquid mode is on the [Hyperliquid venue page](https://api.atreides.finance/v1/venues/hyperliquid).
