import { ApiError, object, type Transport } from './transport.ts'

export interface StreamEvent {
  readonly seq: string
  readonly kind: string
  readonly data: unknown
}

export interface StreamOptions {
  readonly channels: readonly string[]
  readonly since?: string
  readonly onEvent: (event: StreamEvent) => void | Promise<void>
  readonly onReset: (latestSeq: string) => void | Promise<void>
  readonly onMarket?: (channel: string, data: unknown) => void
}

export interface StreamHandle {
  readonly lastSeq: () => string | null
  readonly done: Promise<void>
  close(): Promise<void>
}

export interface SocketCallbacks {
  readonly open: () => void
  readonly message: (data: string) => void
  readonly close: (code: number) => void
  readonly error: () => void
}

export interface WireSocket {
  send(frame: string): void
  close(): void
}

export type SocketFactory = (
  address: string,
  headers: Readonly<Record<string, string>>,
  callbacks: SocketCallbacks,
) => WireSocket

export const browserSocket: SocketFactory = (address, _headers, callbacks) => {
  const socket = new WebSocket(address)
  socket.addEventListener('open', callbacks.open)
  socket.addEventListener('message', (event) => {
    if (typeof event.data === 'string') callbacks.message(event.data)
    else callbacks.close(1003)
  })
  socket.addEventListener('close', (event) => callbacks.close(event.code))
  socket.addEventListener('error', callbacks.error)
  return socket
}

const SEQUENCE = /^[0-9]+$/
const MAX_PENDING_FRAMES = 1024
const ACCESS_CLOSURES = new Map([
  [4001, 'unauthenticated'],
  [4003, 'forbidden'],
  [4009, 'unauthenticated'],
])

function sequence(value: unknown): string {
  if (typeof value !== 'string' || !SEQUENCE.test(value)) throw new ApiError('Invalid stream sequence')
  return value
}

function later(next: string, previous: string): boolean {
  const a = next.replace(/^0+/, '')
  const b = previous.replace(/^0+/, '')
  return a.length === b.length ? a > b : a.length > b.length
}

interface Heartbeat {
  readonly settle: (accounts: Set<string>) => void
}

export class StreamSession implements StreamHandle {
  readonly done: Promise<void>
  #last: string | null
  #wire: WireSocket | null = null
  #abort = new AbortController()
  #heartbeat = new Map<string, Heartbeat>()

  constructor(
    private readonly transport: Transport,
    private readonly options: StreamOptions,
    private readonly factory: SocketFactory,
  ) {
    if (options.channels.length === 0) throw new TypeError('A stream requires at least one channel')
    this.#last = options.since === undefined ? null : sequence(options.since)
    this.done = this.#run()
    void this.done.catch(() => undefined)
  }

  lastSeq = (): string | null => this.#last

  async close(): Promise<void> {
    this.#abort.abort()
    await this.done.catch(() => undefined)
  }

  async heartbeat(accountIds: readonly string[], timeoutMs: number): Promise<Set<string>> {
    const wire = this.#wire
    if (wire === null) return new Set()
    const id = crypto.randomUUID()
    const { resolve, promise } = Promise.withResolvers<Set<string>>()
    const timer = setTimeout(() => resolve(new Set()), timeoutMs)
    this.#heartbeat.set(id, { settle: resolve })
    try {
      wire.send(JSON.stringify({ op: 'heartbeat', id, accountIds }))
      return await promise
    } catch {
      return new Set()
    } finally {
      clearTimeout(timer)
      this.#heartbeat.delete(id)
    }
  }

  #subscribe(wire: WireSocket, channels: readonly string[]): void {
    wire.send(
      JSON.stringify({
        op: 'subscribe',
        id: crypto.randomUUID(),
        channels,
        ...(channels.includes('user') && this.#last !== null ? { since: this.#last } : {}),
      }),
    )
  }

  async #frame(text: string, wire: WireSocket): Promise<number | null> {
    let frame: Record<string, unknown> | null
    try {
      frame = object(JSON.parse(text))
    } catch {
      throw new ApiError('Malformed stream frame')
    }
    if (frame === null) throw new ApiError('Malformed stream frame')
    if (frame.type === 'event' && frame.channel === 'user') {
      const seq = sequence(frame.seq)
      if (typeof frame.kind !== 'string') throw new ApiError('Malformed user stream event')
      if (this.#last === null || later(seq, this.#last)) {
        await this.options.onEvent({ seq, kind: frame.kind, data: frame.data })
        this.#last = seq
      }
    } else if (frame.type === 'reset' && frame.channel === 'user') {
      const latest = sequence(frame.latestSeq)
      await this.options.onReset(latest)
      this.#last = latest
      if (this.#wire === wire) this.#subscribe(wire, ['user'])
    } else if ((frame.type === 'market' || frame.type === 'broadcast') && typeof frame.channel === 'string') {
      this.options.onMarket?.(frame.channel, frame.data)
    } else if (frame.type === 'ack' && frame.op === 'heartbeat' && typeof frame.id === 'string') {
      const accounts = new Set<string>()
      if (Array.isArray(frame.results)) {
        for (const value of frame.results) {
          const row = object(value)
          if (row?.ok === true && typeof row.accountId === 'string') accounts.add(row.accountId)
        }
      }
      this.#heartbeat.get(frame.id)?.settle(accounts)
    } else if (frame.type === 'closing') {
      if (frame.reason === 'revoked') throw new ApiError('Stream credentials were revoked', { code: 'unauthenticated' })
      const wait = frame.reconnectAfterMs
      return typeof wait === 'number' && Number.isFinite(wait) && wait >= 0 ? wait : 0
    }
    return null
  }

  async #run(): Promise<void> {
    const signal = this.#abort.signal
    let delay = 250
    while (!signal.aborted) {
      const result = await this.#connection()
      const access = ACCESS_CLOSURES.get(result.code)
      if (access !== undefined)
        throw new ApiError('Stream closed by its access policy', { status: result.code, code: access })
      if (signal.aborted) return
      if (result.received) delay = 250
      const wait = Math.max(delay, result.reconnectAfter)
      await new Promise<void>((resolve) => {
        const finish = () => {
          clearTimeout(timer)
          signal.removeEventListener('abort', finish)
          resolve()
        }
        const timer = setTimeout(finish, Math.min(wait, 2_147_483_647))
        signal.addEventListener('abort', finish, { once: true })
      })
      delay = Math.min(30_000, delay * 2)
    }
  }

  async #connection(): Promise<{ code: number; reconnectAfter: number; received: boolean }> {
    const { resolve, promise } = Promise.withResolvers<number>()
    const signal = this.#abort.signal
    const stop = () => resolve(1000)
    signal.addEventListener('abort', stop, { once: true })
    let socket: WireSocket | undefined
    let pending = Promise.resolve()
    let queued = 0
    let accepting = true
    let received = false
    let reconnectAfter = 0
    let failure: unknown
    const target = '/v1/stream'
    const address = `${this.transport.origin.replace(/^http/, 'ws')}${target}`
    const headers = this.transport.headers('GET', target, new Uint8Array())
    try {
      socket = this.factory(address, headers, {
        open: () => {
          if (socket === undefined || signal.aborted) return
          try {
            this.#subscribe(socket, this.options.channels)
            this.#wire = socket
          } catch {
            resolve(1006)
          }
        },
        message: (data) => {
          const wire = socket
          if (wire === undefined || signal.aborted || !accepting) return
          if (queued >= MAX_PENDING_FRAMES) {
            resolve(4008)
            return
          }
          queued++
          pending = pending
            .then(async () => {
              if (failure !== undefined || signal.aborted) return
              const wait = await this.#frame(data, wire)
              if (wait !== null) {
                reconnectAfter = wait
                resolve(1012)
              }
              received = true
            })
            .catch((error: unknown) => {
              failure = error instanceof Error ? error : new ApiError('A stream callback failed', { cause: error })
              resolve(1003)
            })
            .finally(() => {
              queued--
            })
        },
        close: resolve,
        error: () => resolve(1006),
      })
      const code = await promise
      accepting = false
      this.#wire = null
      socket.close()
      await pending
      if (failure !== undefined) throw failure
      return { code, reconnectAfter, received }
    } finally {
      this.#wire = null
      signal.removeEventListener('abort', stop)
      for (const heartbeat of this.#heartbeat.values()) heartbeat.settle(new Set())
    }
  }
}
