# SDK quick-start

There is one SDK in two languages. Both sign requests with your key file, retry safely, and return the same answer shape: `{ ok: true, data }` or a refusal with `code`, `message` and `retryable`.

|              | TypeScript                                     | Python                        |
| ------------ | ---------------------------------------------- | ----------------------------- |
| Package      | `@atreides/sdk`                                | `atreides`                    |
| Import       | `import { createClient } from '@atreides/sdk'` | `from atreides import Client` |
| Command line | `atreides`                                     | `atreides`                    |
| Needs        | Node 22.12 or later                            | Python 3.11 or later          |

Install the `atreides` distribution to use `from atreides import Client`. Both SDK packages provide the `atreides` CLI.

## install

Use Node.js 22.12 or later, or Python 3.11 or later. Run these commands from a client project and set `ATREIDES_REPO` to the absolute path to this checkout.

```sh
npm install --install-links "$ATREIDES_REPO/packages/sdk-ts"
python -m pip install "$ATREIDES_REPO/sdk-python"
```

## read-a-price

Public data needs no key.

```ts
import { createClient } from '@atreides/sdk'

const client = createClient({ baseUrl: 'https://api.atreides.finance' })
const snapshot = await client.request('getMarketSnapshot', { query: { instrumentId: 'hyperliquid:ETH' } })
if (!snapshot.ok) throw new Error(`${snapshot.code}: ${snapshot.message}`)
const [row] = snapshot.data.rows
if (row === undefined) throw new Error('No market observation returned')
console.log(row.instrument, 'mark', row.mark, 'funding APR', row.fundingApr)
```

```text
hyperliquid:ETH mark 1720.9 funding APR -0.022716432
```

```py
from atreides import Client, Refusal

with Client("https://api.atreides.finance") as client:
    snapshot = client.request("getMarketSnapshot", query={"instrumentId": "hyperliquid:ETH"})
    if isinstance(snapshot, Refusal):
        raise RuntimeError(f"{snapshot.code}: {snapshot.message}")
    rows = snapshot.data["rows"]
    if not rows:
        raise RuntimeError("No market observation returned")
    row = rows[0]
    print(row["instrument"], "mark", row["mark"], "funding APR", row["fundingApr"])
```

```text
hyperliquid:ETH mark 1720.9 funding APR -0.022716432
```

`client.request(operationId, …)` calls any operation of the [OpenAPI reference](../reference.html) by its `operationId`. The helpers `client.venues`, `client.instruments`, `client.orders`, `client.route`, `client.positions()`, `client.balances()` and `client.equity()` wrap the common ones.

## authenticate

Read the key file you saved from API & Bots. It carries the API origin the key is bound to. Save it where only you can read it.

```ts
import { createClient } from '@atreides/sdk'
import { readKeyFile } from '@atreides/sdk/keys/node'

const key = await readKeyFile('atreides-key.json')
const client = createClient({ baseUrl: key.apiOrigin, key })

const me = await client.request('whoami')
if (!me.ok) throw new Error(`${me.code}: ${me.message}`)
console.log(me.data.scopes, me.data.env)
```

```text
[ 'read', 'trade' ] live
```

```py
from atreides import Client, Refusal
from atreides.keys import read_key_file

key = read_key_file("atreides-key.json")
client = Client(key.api_origin, key)
me = client.request("whoami")
if isinstance(me, Refusal):
    raise RuntimeError(f"{me.code}: {me.message}")
print(me.data.model_dump(mode="json")["scopes"], me.data.env.value)
```

```text
['read', 'trade'] live
```

## check-an-order

A dry run answers what the order would meet and stores nothing. It needs trade scope. Use an account ID from `client.accounts.list()`.

```ts
const accounts = await client.accounts.list()
if (!accounts.ok) throw new Error(`${accounts.code}: ${accounts.message}`)
const accountId = accounts.data[0]?.accountId
if (accountId === undefined) throw new Error('No linked account')

const check = await client.request('placeOrder', {
  body: {
    accountId,
    clientOrderId: client.relay.clientOrderId(),
    instrumentId: 'hyperliquid:BTC',
    side: 'buy',
    type: 'limit',
    price: '50000', // far below the market: the order would rest
    size: '0.001',
    tif: 'post_only',
    dryRun: true,
  },
})
if (!check.ok) throw new Error(`${check.code}: ${check.message}`)
console.log(check.data)
```

```py
from uuid import uuid4

accounts = client.accounts.list()
if isinstance(accounts, Refusal) or not accounts.data:
    raise RuntimeError("No linked account")
account_id = str(accounts.data[0].accountId.root)

check = client.request(
    "placeOrder",
    body={
        "accountId": account_id,
        "clientOrderId": uuid4().hex,
        "instrumentId": "hyperliquid:BTC",
        "side": "buy",
        "type": "limit",
        "price": "50000",  # far below the market: the order would rest
        "size": "0.001",
        "tif": "post_only",
        "dryRun": True,
    },
)
if isinstance(check, Refusal):
    raise RuntimeError(f"{check.code}: {check.message}")
print(check.data)
```

The answer is a `DryRunResult`: `wouldPlace`, the quantized `price` and `size`, the `fee`, and a `refusal` when the order would be refused. See [market, quote and order endpoints](market-and-orders.md#check-an-order-before-you-send-it).

A routed order is checked with a quote. `client.route.quote` proposes the legs and the best single venue and places nothing:

```ts
const quote = await client.route.quote({
  group: 'perp:BTC',
  side: 'buy',
  type: 'market',
  size: '0.001',
  accountIds: [accountId],
})
if (!quote.ok) throw new Error(`${quote.code}: ${quote.message}`)
```

```py
quote = client.route.quote({"group": "perp:BTC", "side": "buy", "type": "market", "size": "0.001", "accountIds": [account_id]})
```

See [smart routing](smart-routing.md) for placing and following a route.

## place-and-cancel

Placing is the same call without `dryRun`, through `client.orders.place`, which makes a client order ID when you give none and returns it with the result. Save the ID: a retry with the same ID never places a second order.

```ts
const placement = client.orders.place({
  accountId,
  instrumentId: 'hyperliquid:BTC',
  side: 'buy',
  type: 'limit',
  price: '50000',
  size: '0.001',
  tif: 'post_only',
})
const placed = await placement.result
if (!placed.ok) throw new Error(`${placed.code}: ${placed.message}`)

const cancelled = await client.orders.cancel(accountId, placement.clientOrderId)
if (!cancelled.ok) throw new Error(`${cancelled.code}: ${cancelled.message}`)
```

```py
placement = client.orders.place(
    {"accountId": account_id, "instrumentId": "hyperliquid:BTC", "side": "buy", "type": "limit", "price": "50000", "size": "0.001", "tif": "post_only"}
)
placed = placement.result
if isinstance(placed, Refusal):
    raise RuntimeError(f"{placed.code}: {placed.message}")

cancelled = client.orders.cancel(account_id, placement.client_order_id)
if isinstance(cancelled, Refusal):
    raise RuntimeError(f"{cancelled.code}: {cancelled.message}")
```

Place only what your account and key limits allow. A refusal names the limit that stopped the order; see [refusals](refusals.md).

## close-a-position

Pick a price and size that your account and key permit for the current market. A resting order you cancel leaves no position. For an existing position, use the opposite side and set `reduceOnly` so the order cannot open or increase exposure. This example closes a long position.

```ts
const closed = await client.orders.place({
  accountId,
  instrumentId: 'hyperliquid:BTC',
  side: 'sell',
  type: 'market',
  size: '0.001',
  reduceOnly: true,
}).result
if (!closed.ok) throw new Error(`${closed.code}: ${closed.message}`)
```

```py
closed = client.orders.place({
    "accountId": account_id, "instrumentId": "hyperliquid:BTC", "side": "sell",
    "type": "market", "size": "0.001", "reduceOnly": True,
}).result
if isinstance(closed, Refusal):
    raise RuntimeError(f"{closed.code}: {closed.message}")
```

The checked-in [TypeScript](../examples/ts/quickstart.ts) and [Python](../examples/py/quickstart.py) quick-starts contain the same placement, cancellation and close calls.
