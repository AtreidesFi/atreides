# Stream resume and dead-man

Subscribe to the user channel and persist the sequence only after processing the event successfully. Reconnect with that sequence as the since cursor. If the server sends reset, refresh the account snapshot before resuming the stream from the new sequence.

## Events on the user channel

Each event has `seq`, `kind`, `at`, a `subject` and `data`. Kinds you can rely on: `order.updated` (`data.order` is the `Order`; a final order carries its [execution report](execution-report.md)), `route.updated`, `fill.recorded`, `fill.reconciled`, `position.snapshot`, `balance.snapshot`, `deadman.updated`, `connection.updated`, `api_key.updated`, `ticket.updated`. Other kinds may appear; ignore the ones you do not use. A key bound to some accounts receives only the orders, routes and account snapshots of those accounts.

For a venue and account that support a dead-man, arm it with the timeout from your strategy's safety policy. Refresh before it expires and handle a refused heartbeat. On shutdown, cancel resting orders, verify the resulting state and clear the dead-man after the strategy has confirmed it is safe.

The SDK [stream and heartbeat examples](../README.md#examples) show where to persist a sequence and how to stop a refresh loop.
