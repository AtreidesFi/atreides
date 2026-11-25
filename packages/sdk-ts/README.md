# Atreides TypeScript SDK

Use Node.js 22.12 or later. In a client project, set `ATREIDES_REPO` to the absolute path to this checkout and install the bundled SDK.

```sh
npm install --install-links "$ATREIDES_REPO/packages/sdk-ts"
```

```ts
import { createClient } from '@atreides/sdk'

const client = createClient({ baseUrl: 'https://api.atreides.finance' })
const result = await client.venues.list()
if (!result.ok) throw new Error(`${result.code}: ${result.message}`)
console.log(result.data)
```

See the [SDK quick-start](../../docs/api/guides/sdk.md) for signed reads and dry runs. The checkout includes SDK source and installable JavaScript and type declarations in `dist/`. The source imports shared types and utilities used to produce that bundle. Use the bundled entry points for standalone installs. The CLI is `atreides`.

## Hyperliquid direct orders

Import `HyperliquidDirect` and `linkAddress` from `@atreides/sdk/direct/hyperliquid`. Link the wallet and call `direct.start({ client, accountRef })` to get the applicable fee schedule. Supply a local signer, the venue network and a single-attempt venue transport. `placeBatch` returns results with their original indices. Check the venue state after an ambiguous send before retrying. See the [direct example](../../docs/api/examples/ts/hyperliquid-direct.ts) and the [current deployment evidence](../../AGENTS.md).
