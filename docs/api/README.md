# Atreides API documentation

Open the [generated reference](reference.html) in a browser or load the [OpenAPI bundle](../../spec/openapi.yaml) into an OpenAPI reader. The reference is generated from that bundle and can be viewed offline.

- [Getting started](guides/getting-started.md) and [Router on your size](guides/router-on-your-size.md)
- [REST quick-start](guides/quickstart.md) and [market and order endpoints](guides/market-and-orders.md)
- [Authentication and signing](guides/authentication.md)
- [TypeScript and Python SDK quick-start](guides/sdk.md)
- [MCP quick-start](guides/mcp.md) and [assistant guide](guides/assistant.md)
- [Execution](guides/execution.md), [reports](guides/execution-report.md), [fees](guides/fees.md), [routing](guides/smart-routing.md) and [route explanations](guides/route-explanations.md)
- [Limits](guides/limits.md), [refusals](guides/refusals.md), [budgets](guides/budgets.md), [dead-man switch](guides/deadman.md) and [stream resume](guides/stream.md)
- [API keys](guides/keys.md), [TWAP example](guides/twap-bot.md), [Hyperliquid connection](guides/connect-hyperliquid.md) and [competition scoring](guides/competitions.md)
- [Public product status and independently checkable numbers](../../AGENTS.md)

## Examples

Install the SDK using the [SDK quick-start](guides/sdk.md).

| Example | TypeScript | Python |
| --- | --- | --- |
| Testnet client and discovery | [quickstart.ts](examples/ts/quickstart.ts) | [quickstart.py](examples/py/quickstart.py) |
| Stream resume | [stream-resume.ts](examples/ts/stream-resume.ts) | [stream_resume.py](examples/py/stream_resume.py) |
| Dead-man switch | [deadman.ts](examples/ts/deadman.ts) | [deadman.py](examples/py/deadman.py) |
| Market making | [market-maker.ts](examples/ts/market-maker.ts) | [market_maker.py](examples/py/market_maker.py) |
| Split routing | [route-split.ts](examples/ts/route-split.ts) | [route_split.py](examples/py/route_split.py) |
| Hyperliquid direct | [hyperliquid-direct.ts](examples/ts/hyperliquid-direct.ts) | [hyperliquid_direct.py](examples/py/hyperliquid_direct.py) |
| CCXT | [ccxt.ts](examples/ts/ccxt.ts) | [ccxt.py](examples/py/ccxt.py) |

The anonymous quick-start reads market data without signing. Order and account examples need the access specified in the guide. Use disposable testnet credentials for examples that send orders.
