# Limits and safety

Limits stop a bot from doing more than you meant. They are checked before the order reaches the venue; an order over a limit is refused with `limit_exceeded` and never sent.

## Per-key limits

Each API key carries its own limits: `maxOrderNotional` (the largest single order), `dailyNotional` (the most it may trade per UTC day) and `venues` (the venues it may use, a subset of the venues of its bound accounts). A limit left out is unlimited. Set them when you create the key in API & Bots, or change them later in API & Bots. Changing a key's limits needs your signed-in browser session: a key cannot raise its own limits (`setApiKeyLimits`, session scope). Every change writes a new version.

## Account and user limits

Limits also exist for the whole user and for one account, with more fields: allowed venues, instrument classes and instruments, maximum position notional, maximum leverage and maximum open orders. Read them with `listLimits` by scope `user`, `account` or `key`. Platform, user, account and key limits all apply to an order.

## Reference price for an opening

The gateway computes notional from the quantized order size and one reference price for the opening checks and reservation.

| Order           | Reference price                                                              |
| --------------- | ---------------------------------------------------------------------------- |
| Market          | Fresh mark. The slippage band separately bounds the price sent to the venue. |
| Buy limit       | Quantized limit price.                                                       |
| Sell limit      | Higher of the quantized limit price and the mark.                            |
| Opening trigger | Higher of the quantized executable price and the trigger price.              |

The venue minimum and any configured signer or canary caps can apply additional checks. A canary's per-order cap uses the higher of the reference price and the mark. The UTC window for `dailyNotional` differs from the Dubai calendar day used for wallet loss.

## What counts as exposure

`maxOpenNotional` checks the sum of gross marked position notional, outstanding opening reservations and the new opening's notional. Gross means the absolute position value before offsetting long and short positions. Pending, accepted, resting, partially filled and unknown openings hold reservations. Reduce-only orders do not add to that total. An amendment replaces the prior reservation for the order in the quota calculation.

Account and key checks use the order's account for the live position read and count reservations in the scope of the applicable limit. User-level position and open-notional checks combine linked accounts. The shared sandbox user combines the accounts of the active wallet. The canary guard adds the outstanding size of its local opening orders and the net value of recorded opening and closing fills since the last position snapshot, with the fill adjustment floored at zero. Where a path requires a live position read, missing or unpriceable evidence refuses the opening with `limit_exceeded` and reason `exposure_unknown`.

## How loss limits are measured

- Session loss is session-start marked equity minus current marked equity.
- Daily loss is Dubai day-start marked equity minus current marked equity. The day starts at midnight UTC+04:00.
- The minimum equity lock compares current marked equity with the funded baseline multiplied by the configured ratio.

The gateway checks the current order account for account-scoped loss limits. User loss limits sum linked accounts. Key loss limits sum the same wallet address. The shared sandbox user uses the current wallet for user-level loss limits too. Every account in scope needs fresh equity and the relevant baseline. The loss cap refuses an opening when loss reaches or exceeds the limit. The equity lock refuses below its threshold. A missing baseline or equity mark older than ten seconds refuses an opening with `exposure_unknown`. Reduce-only closes can continue through these opening limits.

The sandbox can enforce its configured loss limits. The API-key limits endpoint currently refuses setting `maxDailyLoss` directly with `feature_unavailable`. See the `ApiKeyLimitsInput` and `ApiKeyLimits` schemas in the [OpenAPI reference](../reference.html).

## Bound accounts

A key acts only on the accounts it is bound to. A read key can be bound to all accounts; a key with trade scope must name its accounts. An order for another account is refused with `account_not_bound`.

## Protect your bot

Arm the [dead-man switch](deadman.md) so resting orders are cancelled if your bot stops. Keep the key file readable only by you and restrict the key to your server's IP addresses with an allowlist (up to 16 CIDRs).
