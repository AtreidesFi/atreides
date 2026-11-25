export type Hex = `0x${string}`

export interface SignedTransaction {
  readonly raw: Hex
  readonly hash: Hex
}

export interface SenderRpc {
  pendingNonce(sender: Hex): Promise<number>
  /** True only when this exact hash is visible on the configured chain. */
  hasTransaction(hash: Hex): Promise<boolean>
}

interface Pending {
  readonly signed: SignedTransaction
  readonly nonce: number
  readonly expiresAt: number
  readonly publish: (tx: SignedTransaction) => Promise<unknown>
}

interface SenderState {
  tail: Promise<void>
  next: number | null
  pending: Pending | null
}

const senders = new Map<string, SenderState>()

export class RobinhoodNonceUncertain extends Error {
  override readonly name = 'RobinhoodNonceUncertain'
  constructor(
    readonly nonce: number,
    readonly hash: Hex,
    cause?: unknown,
  ) {
    super('The signed Robinhood transaction is awaiting confirmation', { cause })
  }
}

/** An authoritative pre-broadcast refusal from our submit endpoint. */
export class RobinhoodSubmitRefused extends Error {
  override readonly name = 'RobinhoodSubmitRefused'
}

function stateOf(sender: Hex): SenderState {
  const key = sender.toLowerCase()
  const existing = senders.get(key)
  if (existing !== undefined) return existing
  const state: SenderState = { tail: Promise.resolve(), next: null, pending: null }
  senders.set(key, state)
  return state
}

async function exclusively<T>(state: SenderState, work: () => Promise<T>): Promise<T> {
  const previous = state.tail
  const release = Promise.withResolvers<void>()
  state.tail = release.promise
  await previous
  try {
    return await work()
  } finally {
    release.resolve()
  }
}

function accepted(state: SenderState, nonce: number) {
  state.next = nonce + 1
  state.pending = null
}

async function seen(rpc: SenderRpc, pending: Pending): Promise<boolean> {
  try {
    return await rpc.hasTransaction(pending.signed.hash)
  } catch (error) {
    throw new RobinhoodNonceUncertain(pending.nonce, pending.signed.hash, error)
  }
}

type Send = <T>(
  expiresAt: number,
  sign: (nonce: number) => Promise<SignedTransaction>,
  publish: (tx: SignedTransaction) => Promise<T>,
) => Promise<T>

export function runInSenderQueue<T>(
  sender: Hex,
  rpc: SenderRpc,
  clock: () => number,
  work: (send: Send) => Promise<T>,
): Promise<T> {
  const state = stateOf(sender)
  return exclusively(state, async () => {
    if (state.pending !== null) {
      const pending = state.pending
      if (!(await seen(rpc, pending))) throw new RobinhoodNonceUncertain(pending.nonce, pending.signed.hash)
      accepted(state, pending.nonce)
    }
    return work(async (expiresAt, sign, publish) => {
      if (clock() >= expiresAt) throw new Error('The Robinhood swap quote expired')
      const observed = await rpc.pendingNonce(sender)
      if (!Number.isSafeInteger(observed) || observed < 0) throw new Error('Invalid Robinhood pending nonce')
      const nonce = Math.max(observed, state.next ?? 0)
      if (!Number.isSafeInteger(nonce + 1)) throw new Error('Robinhood nonce exceeds the SDK integer range')
      const signed = await sign(nonce)
      if (clock() >= expiresAt) throw new Error('The Robinhood swap quote expired during signing')
      try {
        const result = await publish(signed)
        accepted(state, nonce)
        return result
      } catch (error) {
        if (error instanceof RobinhoodSubmitRefused) throw error
        state.pending = { signed, nonce, expiresAt, publish }
        throw new RobinhoodNonceUncertain(nonce, signed.hash, error)
      }
    })
  })
}

export function retrySenderTransaction(sender: Hex, rpc: SenderRpc, clock: () => number): Promise<Hex | null> {
  const state = stateOf(sender)
  return exclusively(state, async () => {
    const pending = state.pending
    if (pending === null) return null
    if (!(await seen(rpc, pending))) {
      if (clock() >= pending.expiresAt) throw new RobinhoodNonceUncertain(pending.nonce, pending.signed.hash)
      try {
        await pending.publish(pending.signed)
      } catch (error) {
        throw new RobinhoodNonceUncertain(pending.nonce, pending.signed.hash, error)
      }
    }
    accepted(state, pending.nonce)
    return pending.signed.hash
  })
}
