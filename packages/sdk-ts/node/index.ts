import WebSocket from 'ws'
import { type Client, type ClientOptions, createClient as client } from '../src/client.ts'
import type { SocketFactory } from '../src/stream.ts'

export * from '../src/index.ts'

const dial: SocketFactory = (address, headers, callbacks) => {
  const socket = new WebSocket(address, {
    headers,
    followRedirects: false,
    handshakeTimeout: 10_000,
    maxPayload: 1 << 20,
  })
  socket.on('open', callbacks.open)
  socket.on('message', (data, binary) => {
    if (binary) callbacks.close(1003)
    else callbacks.message(data.toString())
  })
  socket.on('close', callbacks.close)
  socket.on('error', callbacks.error)
  socket.on('unexpected-response', (_request, response) => {
    if (response.statusCode === 401 || response.statusCode === 403) callbacks.close(4001)
    else callbacks.close(1006)
    response.resume()
  })
  return socket
}

export function createClient(options: ClientOptions): Client {
  return client({ ...options, socket: options.socket ?? dial })
}

/**
 * The client of a hosted bot (09 §5.12): the runner starts the container with ATR_API_URL, ATR_BOT_ID and ATR_BOT_KEY_FILE, a
 * 0400 file in the SDK's own key-file format that holds the bot's trade key. The key is the bot's own, bound to its accounts and
 * limits; it is never the wallet key.
 */
export async function createBotClient(
  env: NodeJS.ProcessEnv = process.env,
): Promise<{ readonly botId: string; readonly client: Client }> {
  const { ATR_API_URL: baseUrl, ATR_BOT_ID: botId, ATR_BOT_KEY_FILE: path } = env
  if (baseUrl === undefined || botId === undefined || path === undefined)
    throw new TypeError('Not running as a hosted bot: ATR_API_URL, ATR_BOT_ID and ATR_BOT_KEY_FILE are not set')
  const { readKeyFile } = await import('./keys.ts')
  return { botId, client: createClient({ baseUrl, key: await readKeyFile(path) }) }
}

export interface BotContext {
  readonly botId: string
  readonly client: Client
  readonly accountIds: readonly string[]
}

/** The dead-man timeout a hosted bot arms on its accounts while it runs (the manifest default). */
export const BOT_DEADMAN_MS = 30_000

/**
 * The entry point of a hosted bot (09 §5.12): builds the bot's client, arms the dead-man on the bot's accounts and keeps it fresh
 * (LB-059: the only automatic cancellation of a stopped or crashed bot is that timer, so a bot that stops refreshing it leaves its
 * resting opening orders to be cancelled), opens the user stream and hands each event to `onEvent`. On SIGTERM or SIGINT it calls
 * `onStop` and ends. It never clears the timer: a stop cancels nothing by itself.
 */
export async function runBot(
  handlers: {
    readonly onStart?: (ctx: BotContext) => Promise<void> | void
    readonly onEvent: (ctx: BotContext, event: unknown) => Promise<void> | void
    readonly onStop?: (ctx: BotContext) => Promise<void> | void
  },
  env: NodeJS.ProcessEnv = process.env,
): Promise<void> {
  const { botId, client } = await createBotClient(env)
  const bot = await client.bots.get(botId)
  if (!bot.ok) throw new Error(`The bot cannot be read: ${bot.code}`)
  const ctx: BotContext = { botId, client, accountIds: bot.data.accountIds }
  const keepAlive = client.deadman.keepAlive(ctx.accountIds, BOT_DEADMAN_MS)
  try {
    const latency = await client.bots.latency(botId, { samples: 10 })
    process.stdout.write(
      `latency: hop p50 ${latency.hopMs.p50} ms p95 ${latency.hopMs.p95} ms (${latency.hopMs.samples} reads)` +
        (latency.venueAckMs === null
          ? '; venue ack not measured (under 20 orders)\n'
          : `; venue ack p50 ${latency.venueAckMs.p50} ms p95 ${latency.venueAckMs.p95} ms (${latency.venueAckMs.orders} orders)\n`),
    )
  } catch {
    // A latency read that fails never stops the bot.
  }
  await handlers.onStart?.(ctx)
  const handle = client.stream({
    channels: ['user'],
    onEvent: (event) => {
      void Promise.resolve(handlers.onEvent(ctx, event)).catch((error: unknown) => {
        process.stderr.write(`onEvent failed: ${error instanceof Error ? error.message : String(error)}\n`)
      })
    },
    onReset: () => undefined,
  })
  const stop = () => {
    void (async () => {
      try {
        await handlers.onStop?.(ctx)
      } finally {
        await keepAlive.stop()
        await handle.close()
      }
    })()
  }
  process.once('SIGTERM', stop)
  process.once('SIGINT', stop)
  await handle.done
}
