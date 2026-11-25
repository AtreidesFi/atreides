import { randomUUID } from 'node:crypto'
import { constants } from 'node:fs'
import { link, open, rename, unlink } from 'node:fs/promises'
import { basename, dirname, join } from 'node:path'
import { createClient } from '../src/client.ts'
import { type ApiKeyFile, parseKeyFile } from '../src/keys/index.ts'
import { ApiError } from '../src/transport.ts'

export async function readKeyFile(path: string): Promise<ApiKeyFile> {
  const input = await open(path, constants.O_RDONLY | constants.O_NOFOLLOW)
  try {
    const info = await input.stat()
    if (!info.isFile() || (process.platform !== 'win32' && (info.mode & 0o077) !== 0)) {
      throw new Error('The key file must be a regular file with owner-only permissions')
    }
    return parseKeyFile(JSON.parse(await input.readFile('utf8')))
  } finally {
    await input.close()
  }
}

export async function writeKeyFile(path: string, value: ApiKeyFile, replaceExisting = false): Promise<void> {
  const payload = `${JSON.stringify(parseKeyFile(value), null, 2)}\n`
  const temporary = join(dirname(path), `.${basename(path)}.${randomUUID()}`)
  const output = await open(temporary, 'wx', 0o600)
  try {
    try {
      await output.writeFile(payload)
      await output.sync()
    } finally {
      await output.close()
    }
    if (replaceExisting) await rename(temporary, path)
    else await link(temporary, path)
  } finally {
    await unlink(temporary).catch((error: unknown) => {
      if ((error as { code?: string }).code !== 'ENOENT') throw error
    })
  }
}

export async function bindKeyFile(path: string, keyText: string, fetch?: typeof globalThis.fetch): Promise<ApiKeyFile> {
  const before = await readKeyFile(path)
  if (before.keyId !== null && before.keyId !== keyText) {
    throw new TypeError('This key file is already bound to another key')
  }
  const candidate = parseKeyFile({ ...before, keyId: keyText })
  const client = createClient({
    baseUrl: candidate.apiOrigin,
    key: candidate,
    ...(fetch === undefined ? {} : { fetch }),
  })
  const answer = await client.raw.GET('/v1/whoami')
  if (answer.data?.ok !== true || answer.data.data.principal !== 'api_key' || answer.data.data.keyText !== keyText) {
    throw new ApiError('The server did not authenticate this key', {
      requestId: answer.response.headers.get('X-Request-Id'),
    })
  }
  if (JSON.stringify(await readKeyFile(path)) !== JSON.stringify(before)) {
    throw new Error('The key file changed while its binding was verified')
  }
  if (before.keyId === null) await writeKeyFile(path, candidate, true)
  return candidate
}
