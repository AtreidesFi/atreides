export { type Dec, dec } from '@atr/core'
export {
  type Answer,
  type Client,
  type ClientOptions,
  createClient,
  type KeepAliveHandle,
  type OrderTriggerInput,
  type PlaceOrderInput,
  type RawClient,
  type WalletProof,
} from './client.ts'
export type { ApiKeyFile, KeyScope } from './keys/index.ts'
export type { ReplayClientFrame, ReplayHandle, ReplayServerFrame } from './replay.ts'
export type { PlaceLeg, RouteAnswer, RouteExecuteInput, RouteInput, RoutePlaceInput } from './route-execution.ts'
export type { SocketCallbacks, SocketFactory, StreamEvent, StreamHandle, StreamOptions, WireSocket } from './stream.ts'
export { ApiError, type Key } from './transport.ts'
