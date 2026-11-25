import type { components } from '@atr/api-types'
import type { SocketFactory, WireSocket } from './stream.ts'
import { ApiError, object, type Transport } from './transport.ts'

export type ReplayClientFrame = components['schemas']['ReplayClientFrame']
export type ReplayServerFrame = components['schemas']['ReplayServerFrame']
type Open = Omit<Extract<ReplayClientFrame, { t: 'open' }>, 't'>

export interface ReplayHandle {
  readonly done: Promise<string>
  send(frame: Exclude<ReplayClientFrame, { t: 'open' }>): void
  close(): void
}
const serverTypes = new Set([
  'opened',
  'bar',
  'trade',
  'book',
  'funding',
  'order_update',
  'fill',
  'account',
  'step_done',
  'finished',
  'error',
])

export function openReplay(
  transport: Transport,
  dial: SocketFactory,
  input: Open,
  onFrame: (frame: ReplayServerFrame) => void,
) {
  const target = '/v1/lab/replay'
  const uri = new URL(target, transport.origin)
  uri.protocol = uri.protocol === 'https:' ? 'wss:' : 'ws:'
  const headers = transport.headers('GET', target, new Uint8Array())
  const finish = Promise.withResolvers<string>()
  void finish.promise.catch(() => undefined)
  let wire: WireSocket | undefined
  let opened = false
  let terminal = false
  const fail = (error: unknown) => {
    terminal = true
    finish.reject(error)
    wire?.close()
  }
  const send = (frame: ReplayClientFrame) => {
    if (terminal || wire === undefined || (frame.t !== 'open' && !opened)) throw new ApiError('Replay is not open')
    wire.send(JSON.stringify(frame))
  }
  const connected = dial(uri.href, headers, {
    open: () => {
      queueMicrotask(() => {
        if (!terminal) {
          try {
            send({ ...input, t: 'open' })
          } catch (error) {
            fail(error)
          }
        }
      })
    },
    message: (raw) => {
      if (terminal) return
      try {
        const value = object(JSON.parse(raw))
        if (value === null || typeof value.t !== 'string' || !serverTypes.has(value.t))
          throw new ApiError('Malformed replay frame')
        const frame = value as ReplayServerFrame
        if (frame.t === 'opened') opened = true
        if (frame.t === 'finished') {
          if (typeof frame.resultId !== 'string') throw new ApiError('Malformed replay result')
          terminal = true
          finish.resolve(frame.resultId)
        }
        onFrame(frame)
        if (frame.t === 'error' && !opened) fail(new ApiError(frame.message, { code: frame.code }))
      } catch (error) {
        fail(error)
      }
    },
    close: () => {
      if (!terminal) fail(new ApiError('Replay socket closed before its result'))
    },
    error: () => {
      if (!terminal) fail(new ApiError('Replay transport failed'))
    },
  })
  wire = connected
  if (terminal) wire.close()
  return { done: finish.promise, send, close: () => send({ t: 'close' }) } satisfies ReplayHandle
}
