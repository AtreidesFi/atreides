import { mkdir } from 'node:fs/promises'
import { homedir } from 'node:os'
import { dirname, join } from 'node:path'
import { parseArgs } from 'node:util'
import type { QueryOf } from '@atr/api-types'
import { AppError, VENUE_IDS } from '@atr/core'
import { type ApiKeyFile, generateKeyPair, type KeyScope, keyRequestLink } from '../src/keys/index.ts'
import { ApiError } from '../src/transport.ts'
import { createClient } from './index.ts'
import { bindKeyFile, readKeyFile, writeKeyFile } from './keys.ts'
import { approveLighterWithBot, type LighterCliApproval } from './lighter-approve.ts'

const API_ORIGIN = 'https://api.atreides.finance'
const APP_ORIGIN = 'https://app.atreides.finance'
const USAGE = `atreides keys create --scopes read[,trade] --name <name> [--origin <url>] [--file <path>]
atreides keys bind <keyText> [--file <path>]
atreides keys show <keyText> [--file <path>]
atreides ping [--file <path>]
atreides stream [--since <seq>] [--channels user,venues] [--file <path>]
atreides fees [--venue <venue>] [--file <path>]
atreides bots list|get <botId>|start <botId>|stop <botId>|restart <botId>|logs <botId> [--since <n>] [--file <path>]
atreides lighter approve`

interface CliIo {
  readonly out: (line: string) => void
  readonly err: (line: string) => void
  readonly env: NodeJS.ProcessEnv
  readonly lighterApproval?: LighterCliApproval
}

const system: CliIo = {
  out: (line) => process.stdout.write(`${line}\n`),
  err: (line) => process.stderr.write(`${line}\n`),
  env: process.env,
}

function configuredKeyFile(env: NodeJS.ProcessEnv): string | undefined {
  return env.ATREIDES_KEY_FILE
}

function defaultFile(env: NodeJS.ProcessEnv): string {
  return join(env.XDG_CONFIG_HOME || join(homedir(), '.config'), 'atreides', 'key.json')
}

function clientFor(key: ApiKeyFile) {
  return createClient({ baseUrl: key.apiOrigin, key })
}

async function whoami(key: ApiKeyFile) {
  const result = await clientFor(key).raw.GET('/v1/whoami')
  if (result.data?.ok !== true || result.data.data.principal !== 'api_key' || result.data.data.keyText !== key.keyId) {
    throw new ApiError('The API did not authenticate this key', { status: result.response.status })
  }
  return result.data.data
}

function scopes(value: string | undefined): readonly KeyScope[] {
  if (value === 'read') return ['read']
  if (value === 'read,trade') return ['read', 'trade']
  throw new TypeError('Use --scopes read or --scopes read,trade')
}

function one(values: readonly string[], description: string): string {
  if (values.length !== 1 || values[0] === undefined) throw new TypeError(description)
  return values[0]
}

const file = { type: 'string' } as const

async function keys(command: string | undefined, args: readonly string[], io: CliIo): Promise<void> {
  if (command === 'create') {
    const { values } = parseArgs({
      args: [...args],
      options: { file, scopes: { type: 'string' }, name: { type: 'string' }, origin: { type: 'string' } },
    })
    const grants = scopes(values.scopes)
    if (values.name === undefined) throw new TypeError('Use --name to name the key')
    const key = await generateKeyPair(values.origin ?? io.env.ATREIDES_API_ORIGIN ?? API_ORIGIN)
    const url = keyRequestLink(io.env.ATREIDES_APP_ORIGIN ?? APP_ORIGIN, key, values.name, grants)
    const explicitFile = values.file ?? configuredKeyFile(io.env)
    const path = explicitFile ?? defaultFile(io.env)
    if (explicitFile === undefined) {
      await mkdir(dirname(path), { mode: 0o700, recursive: true })
    }
    await writeKeyFile(path, key)
    io.out(url)
    return
  }
  if (command !== 'bind' && command !== 'show') throw new TypeError('Use keys create, bind or show')
  const { values, positionals } = parseArgs({ args: [...args], options: { file }, allowPositionals: true })
  const keyText = one(positionals, 'Provide one key text')
  const path = values.file ?? configuredKeyFile(io.env) ?? defaultFile(io.env)
  if (command === 'bind') {
    try {
      await bindKeyFile(path, keyText)
    } catch (error) {
      if (error instanceof ApiError && error.code === 'signature_invalid') {
        throw new Error('The key text does not belong to this key')
      }
      throw error
    }
    io.out('Key bound')
    return
  }
  const key = await readKeyFile(path)
  if (key.keyId !== keyText) throw new TypeError('The key text is not the one bound to this file')
  const me = await whoami(key)
  io.out(
    JSON.stringify({ publicKey: key.publicKey, scopes: me.scopes, accounts: me.accounts, env: me.env, state: null }),
  )
}

async function stream(key: ApiKeyFile, channels: string, since: string | undefined, io: CliIo): Promise<void> {
  const names = channels.split(',')
  if (names.some((name) => !name) || (since !== undefined && !/^\d+$/.test(since))) {
    throw new TypeError('Expected a channel list and a decimal stream sequence')
  }
  const handle = clientFor(key).stream({
    channels: names,
    ...(since === undefined ? {} : { since }),
    onEvent: (event) => io.out(JSON.stringify({ type: 'event', channel: 'user', ...event })),
    onReset: (latestSeq) => io.out(JSON.stringify({ type: 'reset', channel: 'user', latestSeq })),
    onMarket: (channel, data) =>
      io.out(JSON.stringify({ type: channel === 'venues' ? 'broadcast' : 'market', channel, data })),
  })
  const stop = () => {
    void handle.close()
  }
  process.once('SIGINT', stop)
  process.once('SIGTERM', stop)
  try {
    await handle.done
  } finally {
    process.off('SIGINT', stop)
    process.off('SIGTERM', stop)
  }
}

async function bots(verb: string | undefined, args: readonly string[], io: CliIo): Promise<void> {
  const { values, positionals } = parseArgs({
    args: [...args],
    options: { file, since: { type: 'string' } },
    allowPositionals: true,
  })
  const client = clientFor(await readKeyFile(values.file ?? configuredKeyFile(io.env) ?? defaultFile(io.env)))
  const id = () => one(positionals, 'Provide one bot id')
  const reply =
    verb === 'list'
      ? await client.bots.list()
      : verb === 'get'
        ? await client.bots.get(id())
        : verb === 'start'
          ? await client.bots.start(id())
          : verb === 'stop'
            ? await client.bots.stop(id())
            : verb === 'restart'
              ? await client.bots.restart(id())
              : verb === 'logs'
                ? await client.bots.logs(id(), values.since === undefined ? {} : { since: values.since })
                : null
  if (reply === null) throw new TypeError('Use bots list, get, start, stop, restart or logs')
  if (!reply.ok) throw new ApiError(reply.message, { code: reply.code, requestId: reply.requestId })
  io.out(JSON.stringify(reply.data))
}

async function command(args: readonly string[], io: CliIo): Promise<void> {
  const [name, ...rest] = args
  if (name === undefined || name === '--help' || name === '-h') {
    io.out(USAGE)
    return
  }
  if (name === 'keys') return keys(rest[0], rest.slice(1), io)
  if (name === 'bots') return bots(rest[0], rest.slice(1), io)
  if (name === 'lighter') {
    if (rest.length !== 1 || rest[0] !== 'approve') throw new TypeError('Use atreides lighter approve')
    if (io.lighterApproval === undefined)
      throw new AppError('feature_unavailable', { feature: 'Lighter bot signer and approval exchange' })
    return approveLighterWithBot(io.lighterApproval, io.out)
  }
  if (name !== 'ping' && name !== 'fees' && name !== 'stream') throw new TypeError('Use atreides --help for commands')
  const { values } = parseArgs({
    args: rest,
    options: { file, venue: { type: 'string' }, channels: { type: 'string' }, since: { type: 'string' } },
  })
  if (
    (name !== 'fees' && values.venue !== undefined) ||
    (name !== 'stream' && (values.channels !== undefined || values.since !== undefined))
  ) {
    throw new TypeError('This option is unavailable for the command')
  }
  const key = await readKeyFile(values.file ?? configuredKeyFile(io.env) ?? defaultFile(io.env))
  if (name === 'stream') return stream(key, values.channels ?? 'user,venues', values.since, io)
  if (name === 'ping') {
    const before = Date.now()
    const answer = await whoami(key)
    const skew = Date.parse(answer.serverTime) - Math.floor((before + Date.now()) / 2)
    if (!Number.isFinite(skew)) throw new ApiError('The API returned an invalid server time')
    io.out(JSON.stringify({ skewMs: skew }))
    return
  }
  const venue = VENUE_IDS.find((candidate) => candidate === values.venue)
  if (values.venue !== undefined && venue === undefined) throw new TypeError('Unknown venue')
  const query: QueryOf<'getFees'> = venue === undefined ? {} : { venue }
  const reply = await clientFor(key).fees.schedule(query)
  if (!reply.ok) throw new ApiError(reply.message, { code: reply.code, requestId: reply.requestId })
  io.out(JSON.stringify(reply.data))
}

export async function runCli(args: readonly string[], io: CliIo = system): Promise<number> {
  try {
    await command(args, io)
    return 0
  } catch (error) {
    io.err(error instanceof Error ? error.message : 'The command failed')
    return 1
  }
}
