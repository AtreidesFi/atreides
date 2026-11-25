import { keccak256, parseTransaction, recoverTransactionAddress } from 'viem'
import type { Client, PlaceOrderInput } from '../client.ts'
import {
  type Hex,
  RobinhoodSubmitRefused,
  retrySenderTransaction,
  runInSenderQueue,
  type SenderRpc,
  type SignedTransaction,
} from './robinhood-queue.ts'

export type { Hex, SenderRpc } from './robinhood-queue.ts'
export { RobinhoodNonceUncertain } from './robinhood-queue.ts'

const CHAIN_ID = 4663
const EXECUTE = '0x3593564c'
const ERC20_APPROVE = '0x095ea7b3'
const PERMIT2_APPROVE = '0x87517c45'
const BEFORE_BROADCAST = new Set(['invalid_request', 'relay_refused', 'relay_signer_mismatch', 'user_action_expired'])

type Orders = Pick<Client['orders'], 'place' | 'get' | 'submitTx'>
type Order = Extract<Awaited<ReturnType<Orders['get']>>, { ok: true }>['data']
type Cash = 'USDG' | 'WETH' | 'ETH'

export type RobinhoodSwapInput = Pick<PlaceOrderInput, 'accountId' | 'instrumentId' | 'slippageBps' | 'clientOrderId'> &
  (
    | { readonly side: 'buy'; readonly notional: NonNullable<PlaceOrderInput['notional']> }
    | { readonly side: 'sell'; readonly size: NonNullable<PlaceOrderInput['size']> }
  )

export interface RobinhoodCall {
  readonly to: Hex
  readonly data: Hex
  readonly value: bigint
}

export interface RobinhoodSigner {
  readonly address: Hex
  signTransaction(call: RobinhoodCall & { readonly chainId: 4663; readonly nonce: number }): Promise<Hex>
}

export interface RobinhoodRpc extends SenderRpc {
  sendRawTransaction(raw: Hex): Promise<Hex>
  waitForReceipt(hash: Hex): Promise<{ readonly transactionHash: Hex; readonly success: boolean }>
}

export interface RobinhoodChainOptions {
  readonly client: { readonly orders: Orders }
  readonly signer: RobinhoodSigner
  readonly rpc: RobinhoodRpc
  readonly router: Hex
  readonly permit2: Hex
  readonly usdg: Hex
  readonly weth: Hex
  readonly now?: () => number
}

export interface RobinhoodSwapQuote {
  readonly orderId: string
  readonly clientOrderId: string
  readonly floorOut: string
  readonly feeAmount: string
  readonly feeToken: Hex
  readonly expiresAt: number
}

type Plan = {
  readonly accountId: string
  readonly instrumentId: string
  readonly calls: readonly RobinhoodCall[]
  readonly approvals: Map<number, Hex>
  swapHash: Hex | null
}

function invalid(): never {
  throw new Error('Invalid prepared Robinhood swap')
}

function address(value: unknown): Hex {
  if (typeof value !== 'string' || !/^0x[0-9a-f]{40}$/iu.test(value)) return invalid()
  return value.toLowerCase() as Hex
}

function bytes(value: unknown): Hex {
  if (typeof value !== 'string' || !/^0x(?:[0-9a-f]{2})*$/iu.test(value)) return invalid()
  return value.toLowerCase() as Hex
}

function uint(value: unknown): bigint {
  if (typeof value !== 'string' || !/^(?:0|[1-9]\d{0,77})$/u.test(value)) return invalid()
  const n = BigInt(value)
  if (n >= 1n << 256n) return invalid()
  return n
}

function object(value: unknown): Readonly<Record<string, unknown>> {
  if (typeof value !== 'object' || value === null || Array.isArray(value)) return invalid()
  return value as Readonly<Record<string, unknown>>
}

function asset(instrument: string): { token: Hex; cash: Cash } {
  const match = /^robinhood-chain:(0x[0-9a-fA-F]{40}):(USDG|WETH|ETH)$/u.exec(instrument)
  if (match?.[1] === undefined || match[2] === undefined) return invalid()
  return { token: address(match[1]), cash: match[2] as Cash }
}

function word(data: Hex, index: number): string {
  return data.slice(10 + index * 64, 74 + index * 64)
}

function addressWord(data: Hex, index: number): Hex {
  const encoded = word(data, index)
  if (!/^0{24}[0-9a-f]{40}$/u.test(encoded)) return invalid()
  return address('0x' + encoded.slice(24))
}

function callFrom(value: unknown): RobinhoodCall {
  const call = object(value)
  return { to: address(call.to), data: bytes(call.data), value: uint(call.value) }
}

function uintWord(data: Hex, index: number): bigint {
  const encoded = word(data, index)
  if (!/^[0-9a-f]{64}$/u.test(encoded)) return invalid()
  return BigInt('0x' + encoded)
}

function time(value: unknown): number {
  if (typeof value !== 'string') return invalid()
  const result = Date.parse(value)
  if (!Number.isSafeInteger(result) || result < 0) return invalid()
  return result
}

function matchingOrder(order: Order, accountId: string, clientOrderId: string, instrumentId: string) {
  return (
    order.venue === 'robinhood-chain' &&
    order.accountId === accountId &&
    order.clientOrderId === clientOrderId &&
    order.instrumentId === instrumentId
  )
}

export class RobinhoodChainDirect {
  readonly #options: RobinhoodChainOptions
  readonly #sender: Hex
  readonly #router: Hex
  readonly #permit2: Hex
  readonly #usdg: Hex
  readonly #weth: Hex
  readonly #now: () => number
  readonly #plans = new WeakMap<RobinhoodSwapQuote, Plan>()

  constructor(options: RobinhoodChainOptions) {
    this.#options = options
    this.#sender = address(options.signer.address)
    this.#router = address(options.router)
    this.#permit2 = address(options.permit2)
    this.#usdg = address(options.usdg)
    this.#weth = address(options.weth)
    this.#now = options.now ?? Date.now
  }

  /** Fetch the validated transaction plan and quote before invoking the wallet. */
  async prepare(input: RobinhoodSwapInput): Promise<RobinhoodSwapQuote> {
    const { token, cash } = asset(input.instrumentId)
    const { clientOrderId, result } = this.#options.client.orders.place({ ...input, type: 'market' })
    const answer = await result
    if (!answer.ok) throw new Error(answer.message)
    const order = answer.data
    // A dry run answers `DryRunResult`, which is no order: nothing to sign.
    if ('wouldPlace' in order) return invalid()
    if (
      !matchingOrder(order, input.accountId, clientOrderId, input.instrumentId) ||
      order.side !== input.side ||
      order.state !== 'pending' ||
      order.userAction?.kind !== 'sign_transaction'
    )
      return invalid()
    const request = object(order.userAction.request)
    const quote = object(request.plan)
    if (
      request.kind !== 'evm_calls' ||
      request.chainId !== CHAIN_ID ||
      request.orderId !== order.orderId ||
      request.atomicRequired !== false ||
      typeof request.deadline !== 'number' ||
      !Number.isSafeInteger(request.deadline * 1_000) ||
      request.deadline !== quote.deadline ||
      !Array.isArray(request.calls) ||
      request.calls.length < 1 ||
      request.calls.length > 3
    )
      return invalid()
    const expiresAt = Math.min(time(order.userAction.expiresAt), time(request.expiresAt), request.deadline * 1_000)
    if (this.#now() >= expiresAt) return invalid()
    const expectedFeeToken = cash === 'USDG' ? this.#usdg : this.#weth
    const nativeBuy = cash === 'ETH' && input.side === 'buy'
    const inputToken = input.side === 'sell' ? token : expectedFeeToken
    const calls = request.calls.map(callFrom)
    const swap = calls.at(-1)
    if (
      swap === undefined ||
      swap.to !== this.#router ||
      !swap.data.startsWith(EXECUTE) ||
      uintWord(swap.data, 2) !== BigInt(request.deadline) ||
      (nativeBuy ? swap.value <= 0n || calls.length !== 1 : swap.value !== 0n)
    )
      return invalid()
    const approvalKinds = new Set<string>()
    for (const call of calls.slice(0, -1)) {
      if (call.value !== 0n) return invalid()
      const selector = call.data.slice(0, 10)
      if (approvalKinds.has(selector)) return invalid()
      approvalKinds.add(selector)
      if (selector === ERC20_APPROVE) {
        if (
          call.data.length !== 138 ||
          call.to !== inputToken ||
          addressWord(call.data, 0) !== this.#permit2 ||
          uintWord(call.data, 1) === 0n ||
          approvalKinds.has(PERMIT2_APPROVE)
        )
          return invalid()
      } else if (selector === PERMIT2_APPROVE) {
        if (
          call.data.length !== 266 ||
          call.to !== this.#permit2 ||
          addressWord(call.data, 0) !== inputToken ||
          addressWord(call.data, 1) !== this.#router ||
          uintWord(call.data, 2) >= 1n << 160n ||
          uintWord(call.data, 2) === 0n ||
          uintWord(call.data, 3) < BigInt(request.deadline) ||
          uintWord(call.data, 3) >= 1n << 48n
        )
          return invalid()
      } else return invalid()
    }
    const floor = uint(quote.floorOut)
    const feeAmount = uint(quote.feeAmount)
    if (floor === 0n || address(quote.feeToken) !== expectedFeeToken) return invalid()
    const view = Object.freeze({
      orderId: order.orderId,
      clientOrderId,
      floorOut: floor.toString(),
      feeAmount: feeAmount.toString(),
      feeToken: expectedFeeToken,
      expiresAt,
    })
    this.#plans.set(view, {
      accountId: order.accountId,
      instrumentId: order.instrumentId,
      calls,
      approvals: new Map(),
      swapHash: null,
    })
    return view
  }

  async #sign(call: RobinhoodCall, nonce: number): Promise<SignedTransaction> {
    const raw = bytes(await this.#options.signer.signTransaction({ ...call, chainId: CHAIN_ID, nonce }))
    if (!raw.startsWith('0x02')) throw new Error('The wallet must sign an EIP-1559 Robinhood transaction')
    const serialized = raw as `0x02${string}`
    const tx = parseTransaction(serialized)
    if (
      tx.type !== 'eip1559' ||
      tx.chainId !== CHAIN_ID ||
      tx.nonce !== nonce ||
      address(tx.to) !== call.to ||
      bytes(tx.data ?? '0x') !== call.data ||
      (tx.value ?? 0n) !== call.value ||
      address(await recoverTransactionAddress({ serializedTransaction: serialized })) !== this.#sender
    )
      throw new Error('The wallet signed different Robinhood transaction terms')
    return { raw, hash: keccak256(raw) }
  }

  async submit(quote: RobinhoodSwapQuote): Promise<Order> {
    const plan = this.#plans.get(quote)
    if (plan === undefined) return invalid()
    const { orders } = this.#options.client
    const { rpc } = this.#options
    return runInSenderQueue(this.#sender, rpc, this.#now, async (send) => {
      const current = await orders.get(plan.accountId, quote.clientOrderId)
      if (!current.ok) throw new Error(current.message)
      const order = current.data
      if (
        !matchingOrder(order, plan.accountId, quote.clientOrderId, plan.instrumentId) ||
        order.orderId !== quote.orderId
      )
        return invalid()
      if (order.userAction === null && order.venueOrderId !== null) return order
      if (order.state !== 'pending' || order.userAction?.kind !== 'sign_transaction' || plan.swapHash !== null)
        throw new Error('The Robinhood order is awaiting its submission outcome')
      const currentPlan = object(order.userAction.request)
      const swap = plan.calls.at(-1)
      if (swap === undefined) return invalid()
      if (
        currentPlan.kind !== 'evm_calls' ||
        currentPlan.chainId !== CHAIN_ID ||
        currentPlan.orderId !== quote.orderId ||
        currentPlan.atomicRequired !== false ||
        typeof currentPlan.deadline !== 'number' ||
        !Number.isSafeInteger(currentPlan.deadline) ||
        BigInt(currentPlan.deadline) !== uintWord(swap.data, 2) ||
        !Array.isArray(currentPlan.calls) ||
        currentPlan.calls.length !== plan.calls.length ||
        currentPlan.calls.map(callFrom).some((call, index) => {
          const expected = plan.calls[index]
          return (
            expected === undefined ||
            call.to !== expected.to ||
            call.data !== expected.data ||
            call.value !== expected.value
          )
        })
      )
        return invalid()
      const expiresAt = Math.min(quote.expiresAt, time(order.userAction.expiresAt), time(currentPlan.expiresAt))
      for (const [index, call] of plan.calls.slice(0, -1).entries()) {
        const hash =
          plan.approvals.get(index) ??
          (await send(
            expiresAt,
            (nonce) => this.#sign(call, nonce),
            async (tx) => {
              plan.approvals.set(index, tx.hash)
              if (bytes(await rpc.sendRawTransaction(tx.raw)) !== tx.hash)
                throw new Error('Unexpected approval transaction hash')
              return tx.hash
            },
          ))
        const receipt = await rpc.waitForReceipt(hash)
        if (!receipt.success || bytes(receipt.transactionHash) !== hash) throw new Error('Robinhood approval failed')
      }
      return send(
        expiresAt,
        (nonce) => this.#sign(swap, nonce),
        async (tx) => {
          plan.swapHash = tx.hash
          const reply = await orders.submitTx(plan.accountId, quote.clientOrderId, { kind: 'raw_tx', rawTx: tx.raw })
          if (!reply.ok) {
            if (BEFORE_BROADCAST.has(reply.code)) {
              plan.swapHash = null
              throw new RobinhoodSubmitRefused(reply.message)
            }
            throw new Error(reply.message)
          }
          if (
            !matchingOrder(reply.data, plan.accountId, quote.clientOrderId, plan.instrumentId) ||
            reply.data.orderId !== quote.orderId ||
            reply.data.userAction !== null ||
            (reply.data.venueOrderId?.toLowerCase() !== tx.hash && !(await rpc.hasTransaction(tx.hash)))
          )
            throw new Error('Robinhood submission returned no matching transaction')
          return reply.data
        },
      )
    })
  }

  swap(input: RobinhoodSwapInput): Promise<Order> {
    return this.prepare(input).then((quote) => this.submit(quote))
  }

  retryPending(): Promise<Hex | null> {
    return retrySenderTransaction(this.#sender, this.#options.rpc, this.#now)
  }
}
