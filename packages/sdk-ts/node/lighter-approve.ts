import { createHash } from 'node:crypto'
import { verifyGrant } from '@atr/api-types/auth'
import { AppError } from '@atr/core'
import type { LighterApproval, LighterDirect, LighterSigned } from '../src/direct/lighter.ts'

export interface LighterCliApproval {
  readonly module: Pick<LighterDirect, 'prepareFeeApproval' | 'refreshAccount'>
  readonly owner: string
  readonly accountIndex: number
  readonly nonce: number
  readonly approvalExpiresAt: number
  readonly appOrigin: string
  readonly signal: AbortSignal
  /** Authenticated API call with a one-time reference to the expected message and terms. */
  register(
    value: {
      readonly ref: string
      readonly message: string
      readonly owner: string
      readonly accountIndex: number
      readonly approval: LighterApproval
    },
    signal: AbortSignal,
  ): Promise<void>
  /** Wait on the original authenticated CLI session and consume the signature once. */
  wait(ref: string, signal: AbortSignal): Promise<string>
  /** Reserve one send attempt in the bot's nonce journal before egress. */
  claim(txHash: string, signal: AbortSignal): Promise<boolean>
  /** Only the bot submits; refusal must be definite. An ambiguous response is resolved through receipt. */
  submit(tx: LighterSigned, signal: AbortSignal): Promise<'submitted' | 'refused' | 'unknown'>
  receipt(txHash: string, nonce: number, signal: AbortSignal): Promise<'executed' | 'failed' | 'not_found' | 'unknown'>
}

export async function approveLighterWithBot(
  ports: LighterCliApproval,
  print: (line: string) => void,
  now: () => number = Date.now,
): Promise<void> {
  const { signal } = ports
  signal.throwIfAborted()
  const app = URL.parse(ports.appOrigin)
  if (
    app === null ||
    app.protocol !== 'https:' ||
    app.username ||
    app.password ||
    app.pathname !== '/' ||
    app.search ||
    app.hash ||
    !/^0x[0-9a-f]{40}$/iu.test(ports.owner)
  ) {
    throw new AppError('invalid_request')
  }
  const draft = await ports.module.prepareFeeApproval(ports.nonce, ports.approvalExpiresAt)
  const ref = createHash('sha256').update(draft.messageToSign, 'utf8').digest('hex')
  const done = async () => {
    const account = await ports.module.refreshAccount()
    if (account.accountIndex !== ports.accountIndex || account.tier === 'standard')
      throw new AppError('account_not_bound')
    const expected = draft.approval
    const rows = account.approvals.filter(({ integratorIndex }) => integratorIndex === expected.integratorIndex)
    const result = rows[0]
    if (
      rows.length !== 1 ||
      result === undefined ||
      result.expiresAt !== expected.expiresAt ||
      result.expiresAt <= now() ||
      result.maxPerpsTakerFee !== expected.maxPerpsTakerFee ||
      result.maxPerpsMakerFee !== expected.maxPerpsMakerFee ||
      result.maxSpotTakerFee !== expected.maxSpotTakerFee ||
      result.maxSpotMakerFee !== expected.maxSpotMakerFee
    ) {
      throw new AppError('fee_approval_missing')
    }
  }
  const readReceipt = () => ports.receipt(draft.txHash, ports.nonce, signal)
  const existing = await readReceipt()
  if (existing === 'executed') return done()
  if (existing === 'failed') throw new AppError('venue_rejected')
  if (existing !== 'not_found') throw new AppError('unknown_outcome')

  await ports.register(
    {
      ref,
      message: draft.messageToSign,
      owner: ports.owner,
      accountIndex: ports.accountIndex,
      approval: draft.approval,
    },
    signal,
  )
  const url = new URL('/connections/lighter/cli-approve', app)
  url.searchParams.set('ref', ref)
  print(url.href)
  const signature = await ports.wait(ref, signal)
  if (!verifyGrant(draft.messageToSign, signature, ports.owner, 'eip191')) throw new AppError('signer_mismatch')
  if (now() >= draft.approval.expiresAt) throw new AppError('offer_expired')
  signal.throwIfAborted()
  if (await ports.claim(draft.txHash, signal)) {
    signal.throwIfAborted()
    const txInfo = JSON.stringify({ ...JSON.parse(draft.txInfo), L1Sig: signature })
    if ((await ports.submit({ txType: draft.txType, txHash: draft.txHash, txInfo }, signal)) === 'refused') {
      throw new AppError('venue_rejected')
    }
  }
  const receipt = await readReceipt()
  if (receipt === 'failed') throw new AppError('venue_rejected')
  if (receipt !== 'executed') throw new AppError('unknown_outcome')
  await done()
}
