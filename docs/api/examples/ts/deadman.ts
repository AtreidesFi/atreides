import type { Client } from '@atreides/sdk'

export async function withHeartbeat<T>(client: Client, accountId: string, work: () => Promise<T>): Promise<T> {
  const timeoutMs = 15_000
  const armed = await client.deadman.arm(accountId, timeoutMs)
  if (!armed.ok || !armed.data.armed) throw new Error('Dead-man arming was not confirmed')
  const heartbeat = client.deadman.keepAlive([accountId], timeoutMs)
  let result: T
  try {
    result = await work()
  } finally {
    await heartbeat.stop()
  }
  const failure = heartbeat.lastError()
  if (failure !== null) throw failure
  // Clear only after work has confirmed that its orders are terminal.
  const cleared = await client.deadman.clear(accountId)
  if (!cleared.ok || cleared.data.armed) throw new Error('Dead-man clearing was not confirmed')
  return result
}
