import type { Client } from '@atreides/sdk'

// The venue-specific writer must map a confirmed fee schedule to the CCXT adapter's documented fields.
export async function ccxtParameters(
  client: Client,
  venue: NonNullable<NonNullable<Parameters<Client['fees']['schedule']>[0]>['venue']>,
  accountId: string,
  encode: (fee: { field: string; builderRef: string }) => Readonly<Record<string, string>>,
) {
  const result = await client.fees.schedule({ venue, accountId })
  if (!result.ok) throw new Error(`${result.code}: ${result.message}`)
  const schedule = result.data.find((entry) => entry.venue === venue)
  if (schedule === undefined) throw new Error('The API returned no fee schedule for this venue')
  const { approval, builderRef, field, validUntil } = schedule
  if (
    (approval?.state !== 'approved' && approval?.state !== 'not_needed') ||
    builderRef === null ||
    !field ||
    !(Date.parse(validUntil) > Date.now())
  ) {
    throw new Error('A current approved fee schedule is required')
  }
  const parameters = encode({ field, builderRef })
  if (Object.keys(parameters).length === 0) throw new Error('The CCXT venue mapping returned no parameters')
  return parameters
}
