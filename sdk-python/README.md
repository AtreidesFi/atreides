# Atreides Python SDK

Use Python 3.11 or later. In a virtual environment, set `ATREIDES_REPO` to the absolute path to this checkout and install the package.

```sh
python -m pip install "$ATREIDES_REPO/sdk-python"
```

```python
from atreides import Client, Refusal

with Client("https://api.atreides.finance") as client:
    result = client.venues.list()
    if isinstance(result, Refusal):
        raise RuntimeError(f"{result.code}: {result.message}")
    print(result.data)
```

The distribution, import and CLI use the name `atreides`. See the [SDK quick-start](../docs/api/guides/sdk.md) for signed reads and dry runs.

## Hyperliquid direct orders

Install with `python -m pip install "$ATREIDES_REPO/sdk-python[hyperliquid]"`. Supply the Hyperliquid SDK's `Exchange` to `atreides.direct.hyperliquid.HyperliquidDirect`, link the wallet with `link_address` and call `direct.start(client, account_ref)`. `place_batch` passes the approved builder to the exchange and returns results by original order index. Supply prices and sizes as decimal strings or `Decimal` values. See the [direct example](../docs/api/examples/py/hyperliquid_direct.py) and the [current deployment evidence](../AGENTS.md).
