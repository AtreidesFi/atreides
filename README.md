# Atreides public API and SDKs

Sign in with an invite at [app.atreides.finance](https://app.atreides.finance/) or try an anonymous market read.

```sh
curl -fsS 'https://api.atreides.finance/v1/market-snapshot?instrumentId=hyperliquid:BTC'
```

- [Changelog](CHANGELOG.md) and [Router on your size API guide](docs/api/guides/router-on-your-size.md)
- [For AI agents: what Atreides gives you and the public endpoints](AGENTS.md)
- [Generated API reference](docs/api/reference.html) and [OpenAPI bundle](spec/openapi.yaml)
- [API guides](docs/api/README.md), [router model](docs/ROUTER-MODEL.md) and [MCP quick-start](docs/api/guides/mcp.md)
- [TypeScript SDK](packages/sdk-ts/README.md), [Python SDK](sdk-python/README.md) and [SDK quick-start](docs/api/guides/sdk.md)

## Recheck the public numbers

Every public number comes from an anonymous GET. Compare a day's totals with the fills behind them:

```sh
curl -fsS 'https://api.atreides.finance/v1/public/volume/daily?date=2026-11-24'
curl -fsS 'https://api.atreides.finance/v1/public/volume/fills?date=2026-11-24&venue=hyperliquid'
```

Reproduce a router plan on your own size:

```sh
curl -fsS 'https://api.atreides.finance/v1/route/preview' \
  -H 'Content-Type: application/json' \
  -d '{"group":"perp:BTC","side":"buy","notional":"10000","fundingHorizonHours":24}'
```

Compare leg sizes, price, fees, forecast funding, book age and the single-venue comparison from responses captured at the same time. The [router model](docs/ROUTER-MODEL.md) describes the estimates and execution reports. The [agent guide](AGENTS.md#public-evidence-endpoints) lists every public endpoint.

## License

Atreides code and documentation are provided under the [MIT License](LICENSE). The SDK distribution includes its [third-party notices](packages/sdk-ts/THIRD-PARTY-NOTICES.md).
