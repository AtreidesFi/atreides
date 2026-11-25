import type { Client, StreamEvent } from '@atreides/sdk'

export function resumeUserStream(
  client: Client,
  cursor: string | undefined,
  apply: (event: StreamEvent) => Promise<void>,
  refresh: (latest: string) => Promise<void>,
) {
  return client.stream({
    channels: ['user'],
    ...(cursor === undefined ? {} : { since: cursor }),
    // Apply the update and persist its cursor together. The SDK resumes after this handler resolves.
    onEvent: apply,
    // Re-read the account's orders and fills, then persist the replacement cursor.
    onReset: refresh,
  })
}
