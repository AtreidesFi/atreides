# Authentication and signing

The browser establishes a session after a wallet signs the one-time challenge. Bots use Ed25519 API keys. The test prefix is for testnet and the live prefix is for mainnet. A key file carries the API origin to which the key is bound.

Use the SDK signer to produce the canonical signature over the method, raw path and query, body bytes, timestamp, nonce and key ID. Sign the transmitted body as UTF-8. Preserve repeated query parameters, escaping and order. Generate a new nonce for each signed attempt and keep your machine's clock synchronized.

The client sends X-Api-Key, X-Api-Timestamp, X-Api-Nonce and X-Api-Signature. The WebSocket upgrade signs the GET target with an empty body. Use a WebSocket client that supports headers. The [signing vectors](authentication.md#sign-a-curl-request) include Unicode, repeated queries and the stream upgrade.

## get-a-key

A key comes from the product, never from this site.

- **Your own account.** Sign in to the invite-only app with your wallet, open API & Bots and choose **Create key**. Pick **read** for market and account queries; add **trade** only for a bot that places orders, bind the key to the accounts it may use and set its limits. The app shows the key file once. Save it with mode 0600. An **MCP token** is a separate credential for agent access; see [MCP setup](mcp.md).

A key file is a small JSON object with `v` set to `1`, plus `keyId`, `privateKey`, `publicKey`, `createdAt` and the bound `apiOrigin`. For mainnet the origin is `https://api.atreides.finance` and the key ID starts with `live_`. Anyone with the file can trade within the key's scope and limits, so keep it out of repositories and chat.

## sign-a-curl-request

The SDKs sign for you. To call the API with plain `curl`, this script builds the four headers for one request. It uses only Node 22 or later and the key file. Pass the exact body text you will send; the signature covers its bytes.

```js
// node sign.mjs <key-file> <METHOD> <path-and-query> [body]
// Prints curl config lines with the four signature headers. Use: node sign.mjs ... | curl -K - <url>
import { createHash, createPrivateKey, randomBytes, sign } from 'node:crypto'
import { readFileSync } from 'node:fs'

const [keyFile, method, target, body = ''] = process.argv.slice(2)
const key = JSON.parse(readFileSync(keyFile, 'utf8'))
const timestamp = String(Date.now())
const nonce = randomBytes(16).toString('base64url')
const bodyHash = createHash('sha256').update(body).digest('hex') // the exact bytes you will send
const canonical = ['api-req/v1', method.toUpperCase(), target, bodyHash, timestamp, nonce, key.keyId].join('\n')
const pkcs8 = Buffer.concat([
  Buffer.from('302e020100300506032b657004220420', 'hex'),
  Buffer.from(key.privateKey, 'base64url'),
])
const signature = sign(
  null,
  Buffer.from(canonical),
  createPrivateKey({ key: pkcs8, format: 'der', type: 'pkcs8' }),
).toString('base64url')
const headers = {
  'X-Api-Key': key.keyId,
  'X-Api-Timestamp': timestamp,
  'X-Api-Nonce': nonce,
  'X-Api-Signature': signature,
}
for (const [name, value] of Object.entries(headers)) console.log(`header = "${name}: ${value}"`)
```

Save it as `sign.mjs`. The output is in curl's config format, so pipe it into `curl -K -` and the key never appears in your shell history:

```sh
node sign.mjs atreides-key.json GET /v1/whoami \
  | curl -s -K - https://api.atreides.finance/v1/whoami
```

A signed read answers:

```json
{
  "ok": true,
  "data": {
    "userId": "01a1fcb2-…",
    "principal": "api_key",
    "keyText": "live_06GZ…",
    "scopes": ["read", "trade"],
    "accounts": ["01a1fcb3-…"],
    "env": "live",
    "serverTime": "2026-11-22T21:51:38.489Z"
  }
}
```

The script reproduces the shared [signing vectors](authentication.md#sign-a-curl-request) byte for byte. For a request with a body, pass the same JSON text to the script and to `curl --data-binary`, with `-H 'Content-Type: application/json'`.

A request without valid credentials is refused, never half served:

```json
{
  "ok": false,
  "code": "unauthenticated",
  "message": "Sign in, or sign the request with an API key.",
  "retryable": false,
  "requestId": "01a2053d-6123-7000-871b-9b71e64461b4"
}
```
