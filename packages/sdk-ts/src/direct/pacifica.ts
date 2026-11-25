// 06 §3.5–3.6 and V06-T48: direct orders use the approved Pacifica code and the caller's Ed25519 key.
import type { FeeField, OrderType, TimeInForce } from '@atr/contract'
import { AppError, type InstrumentClass, rate } from '@atr/core'
import {
  pacificaBody,
  pacificaCanonical,
  pacificaJson,
  pacificaPublicKey,
  pacificaSign,
} from '@atr/venue-pacifica/sign'
import { type DirectContext, type DirectModule, type DirectStart, directFees } from './template.ts'

export type PacificaOrderType = 'create_order' | 'create_market_order' | 'create_stop_order' | 'set_position_tpsl'
export type PacificaValue = string | boolean | null | number | bigint | readonly PacificaValue[] | PacificaData
export interface PacificaData {
  readonly [key: string]: PacificaValue
}

export interface PacificaSignedOrder {
  readonly type: PacificaOrderType
  readonly canonical: string
  readonly signature: string
  /** Flattened JSON that must be sent unchanged. */
  readonly body: string
}

export interface PacificaOrderInput {
  readonly operation: PacificaOrderType
  readonly account: string
  readonly key: Uint8Array
  readonly agent?: boolean
  readonly timestamp: number
  readonly data: PacificaData
}

const MAX_CAP = rate.fromBps('1')
const BASE = rate.fromBps('0.1')
const OPERATIONS = new Set<PacificaOrderType>([
  'create_order',
  'create_market_order',
  'create_stop_order',
  'set_position_tpsl',
])
const codeFormat = /^[a-zA-Z0-9]{3,16}$/
const reserved = new Set([
  'account',
  'agent_wallet',
  'signature',
  'timestamp',
  'expiry_window',
  'type',
  'data',
  'builder_code',
])

function bodyIncludesFee(value: PacificaValue): boolean {
  if (value === null || typeof value !== 'object') return false
  if (Array.isArray(value)) return value.some(bodyIncludesFee)
  return Object.entries(value).some(([key, field]) => key === 'builder_code' || bodyIncludesFee(field))
}

export function signPacificaOrder(input: PacificaOrderInput, fee: FeeField): PacificaSignedOrder {
  if (!OPERATIONS.has(input.operation) || !Number.isSafeInteger(input.timestamp) || input.timestamp < 0)
    throw new AppError('invalid_request', { issue: 'Invalid Pacifica order operation or timestamp' })
  if (Object.keys(input.data).some((name) => reserved.has(name)) || bodyIncludesFee(input.data))
    throw new AppError('invalid_request', { issue: 'Reserved Pacifica order field' })
  if (fee.kind !== 'none' && (fee.kind !== 'per_code' || !codeFormat.test(fee.code) || fee.rate !== BASE))
    throw new AppError('fee_code_missing')
  if (fee.kind === 'none' && input.operation !== 'set_position_tpsl' && input.data.reduce_only !== true)
    throw new AppError('fee_approval_missing')
  if (input.operation === 'set_position_tpsl' && !input.data.take_profit && !input.data.stop_loss)
    throw new AppError('invalid_request', { issue: 'Pacifica position TP/SL requires a trigger' })
  const signer = pacificaPublicKey(input.key)
  if (!input.agent && signer !== input.account) throw new AppError('account_not_bound')
  const header = { timestamp: input.timestamp, expiry_window: 5000, type: input.operation } as const
  const data = fee.kind === 'per_code' ? { ...input.data, builder_code: fee.code } : input.data
  const canonical = pacificaCanonical(header, data)
  const signature = pacificaSign(input.key, canonical)
  const body = pacificaJson(pacificaBody(input.account, input.agent === true ? signer : null, signature, header, data))
  return { type: input.operation, canonical, signature, body }
}

export class PacificaDirect implements DirectModule {
  readonly venue = 'pacifica'
  readonly #fees: ReturnType<typeof directFees>

  constructor(now?: () => number) {
    this.#fees = directFees({
      venue: this.venue,
      requiresApproval: true,
      capFor: () => MAX_CAP,
      ...(now === undefined ? {} : { now }),
      encode(row, code) {
        if (code === null || !codeFormat.test(code) || row.rate !== BASE) throw new AppError('fee_code_missing')
        return { kind: 'per_code', code, rate: row.rate }
      },
    })
  }

  start(context: DirectContext): Promise<DirectStart> {
    return this.#fees.start(context)
  }

  refresh(): Promise<DirectStart> {
    return this.#fees.refresh()
  }

  feeFields(cls: InstrumentClass, type: OrderType, tif: TimeInForce, reduceOnly: boolean): FeeField {
    if (cls !== 'perp' && cls !== 'rwa_perp')
      throw new AppError('invalid_request', { issue: 'Unsupported Pacifica class' })
    return this.#fees.feeFields(cls, type, tif, reduceOnly)
  }
}
