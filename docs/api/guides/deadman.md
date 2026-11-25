# Dead-man switch

This guide describes behavior when the relevant feature and account are enabled. Check the [dated live evidence](../../../AGENTS.md#current-access-and-live-evidence) before relying on its availability.

A dead-man switch asks a supported venue to cancel resting orders if the client stops refreshing the timer. Check the venue and account's capability before arming it. A refused or unknown heartbeat gives no proof that the timer was refreshed.

Arm the timer before relying on it and refresh it before expiry. On shutdown, cancel resting orders and confirm their resulting state before clearing the switch. If an order's outcome remains unknown, leave the switch armed and reconcile the order by its original client ID.

See the [stream and dead-man examples](../README.md#examples) and the [venue gate states](https://api.atreides.finance/v1/venues).

## The timer

Arm, refresh or clear the timer with `setDeadMan` (`client.deadman.arm`, `keep_alive` and `clear` in the Python SDK). The switch is per account. A `timeoutMs` of 0 clears the timer; otherwise it is 5000 to 600000 milliseconds. When the timer runs out, the gateway cancels the account's open orders. Refresh well before expiry and handle a refused heartbeat.

## The venue backstop

On Hyperliquid the gateway also arms the venue's own scheduled cancel while our timer is armed and refreshes it while healthy. That venue-side cancel removes every open order on the account, including orders you placed outside this product. If the venue's dead-man lane is full the refusal is `deadman_capacity` and the watchdog remains the backstop.
