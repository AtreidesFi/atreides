import { mkdir, open, readFile, rename, unlink } from 'node:fs/promises'
import { dirname } from 'node:path'
import { type CompiledStrategy, type EngineState, initState } from '@atr/strategy'

export interface PendingOrder {
  readonly id: string
  readonly nativeId: string | null
  readonly instrument: string
  readonly reduceOnly: boolean
  readonly at: number
}

export interface HoldingState {
  readonly side: 'long' | 'short'
  readonly bars: number
  readonly extreme: string
}

export interface RunnerState {
  readonly runId: string
  readonly specHash: string
  sequence: string
  engine: EngineState
  lastClose: number | null
  openingPaused: boolean
  holdings: Record<string, HoldingState>
  pending: PendingOrder[]
}

export async function readState(path: string, runId: string, compiled: CompiledStrategy): Promise<RunnerState> {
  const specHash = Buffer.from(compiled.sha256).toString('hex')
  try {
    const loaded = JSON.parse(await readFile(path, 'utf8')) as RunnerState
    if (
      loaded.runId !== runId ||
      loaded.specHash !== specHash ||
      !/^(0|[1-9]\d*)$/u.test(loaded.sequence) ||
      !Array.isArray(loaded.pending) ||
      loaded.holdings === null ||
      typeof loaded.holdings !== 'object'
    )
      throw new Error('State belongs to a different run or is invalid')
    return loaded
  } catch (error) {
    if ((error as NodeJS.ErrnoException).code !== 'ENOENT') throw error
    return {
      runId,
      specHash,
      sequence: '0',
      engine: initState(compiled),
      lastClose: null,
      openingPaused: false,
      holdings: {},
      pending: [],
    }
  }
}

export async function writeState(path: string, state: RunnerState): Promise<void> {
  const directory = dirname(path)
  await mkdir(directory, { recursive: true, mode: 0o700 })
  const temp = `${path}.${crypto.randomUUID()}.tmp`
  const output = await open(temp, 'wx', 0o600)
  try {
    await output.writeFile(JSON.stringify(state) + '\n')
    await output.sync()
    await output.close()
    await rename(temp, path)
  } finally {
    await output.close()
    await unlink(temp).catch(() => undefined)
  }
}
