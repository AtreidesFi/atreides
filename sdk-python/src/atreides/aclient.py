"""Asynchronous signed API client."""

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence
from types import TracebackType

import httpx

from ._endpoints import DeadMan, Fees, Instruments, Orders, PlaceOrderInput, Venues, order_body
from ._http import ApiError, Body, HttpPolicy, Prepared, Query, Refusal, RequestKey, Retry, unix_ms
from ._keep_alive import AsyncKeepAlive, heartbeat_inputs
from ._route_endpoints import AsyncRoutes
from .client import Reply, WalletProof, page_items
from .keys import ApiKeyFile, bound_key
from .models import AccountLinkChallenge
from .replay import StrategyReplay
from .stream import EventHandler, MarketHandler, ResetHandler, StreamHandle

AsyncReply = Awaitable[Reply]


class AsyncAccounts:
    def __init__(self, client: "AsyncClient") -> None:
        self._client = client

    async def list(self) -> Reply:
        return await self._client.request("listAccounts")

    async def link(self, venue: str, account_ref: str, sign: Callable[[str], Awaitable[WalletProof]]) -> Reply:
        fields = {"venue": venue, "accountRef": account_ref}
        challenge = await self._client.request("createAccountLinkChallenge", body=fields)
        if isinstance(challenge, Refusal):
            return challenge
        if not isinstance(challenge.data, AccountLinkChallenge):
            raise ApiError("Malformed account link challenge", request_id=challenge.request_id)
        message = challenge.data.message
        proof = await sign(message)
        return await self._client.request("linkAccount", body={**fields, "message": message, **proof})


class AsyncDeadMan(DeadMan[AsyncReply]):
    def __init__(self, client: "AsyncClient") -> None:
        super().__init__(client.request)
        self._stream = client._stream_handle
        self._active: set[AsyncKeepAlive] = set()

    def keep_alive(self, account_ids: Sequence[str], timeout_ms: int) -> AsyncKeepAlive:
        accounts = heartbeat_inputs(account_ids, timeout_ms)
        handle = AsyncKeepAlive(accounts, timeout_ms, self.arm, self._stream, self._active.discard)
        self._active.add(handle)
        return handle

    async def stop_all(self) -> None:
        await asyncio.gather(*(handle.stop() for handle in tuple(self._active)))


class AsyncClient:
    def __init__(
        self,
        base_url: str,
        key: RequestKey | ApiKeyFile | None = None,
        *,
        timeout: float = 10.0,
        attempts: int = 3,
        base_delay: float = 0.25,
        transport: httpx.AsyncBaseTransport | None = None,
        now: Callable[[], int] = unix_ms,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._policy = HttpPolicy(base_url, bound_key(base_url, key), now, attempts, base_delay)
        self._client = httpx.AsyncClient(timeout=timeout, transport=transport, follow_redirects=False, trust_env=False)
        self._sleep = sleep
        self.raw = self
        self.strategy = StrategyReplay(self._policy)
        self.route = AsyncRoutes(self.request, self._pages, now, self._sleep)
        self.venues: Venues[AsyncReply] = Venues(self.request)
        self.instruments: Instruments[AsyncReply, AsyncIterator[object]] = Instruments(self.request, self._pages)
        self.fees: Fees[AsyncReply] = Fees(self.request)
        self.accounts = AsyncAccounts(self)
        self.orders: Orders[AsyncReply, AsyncIterator[object]] = Orders(
            self.request, self._pages, self._prepare_order, self._send
        )
        self._stream: StreamHandle | None = None
        self.deadman = AsyncDeadMan(self)

    @property
    def clock_skew_ms(self) -> int:
        return self._policy.clock_skew_ms

    async def __aenter__(self) -> "AsyncClient":
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        await self.close()

    async def close(self) -> None:
        await self.deadman.stop_all()
        if self._stream is not None:
            await self._stream.close()
        await self._client.aclose()

    def _stream_handle(self) -> StreamHandle | None:
        return self._stream

    def stream(
        self,
        channels: Sequence[str],
        *,
        on_event: EventHandler,
        on_reset: ResetHandler,
        since: str | None = None,
        on_market: MarketHandler | None = None,
    ) -> StreamHandle:
        if self._stream is not None:
            self._stream.cancel()
        self._stream = StreamHandle(
            self._policy, channels, since=since, on_event=on_event, on_reset=on_reset, on_market=on_market
        )
        return self._stream

    def _prepare_order(self, order: PlaceOrderInput) -> Prepared:
        return self._policy.prepare("placeOrder", None, None, order_body(order), None)

    async def request(
        self,
        operation_id: str,
        *,
        path: Mapping[str, str] | None = None,
        query: Query | None = None,
        body: Body = None,
        idempotency_key: str | None = None,
    ) -> Reply:
        return await self._send(self._policy.prepare(operation_id, path, query, body, idempotency_key))

    async def _send(self, prepared: Prepared) -> Reply:
        attempt = 0
        resynced = False
        while True:
            sent_at = self._policy.now()
            try:
                response = await self._client.send(self._policy.request(prepared))
            except httpx.TransportError as error:
                retry = self._policy.network_retry(prepared, attempt)
                if retry is None:
                    raise ApiError("API transport failed", client_order_id=prepared.client_order_id) from error
            else:
                answer = self._policy.answer(response, prepared, attempt, resynced, sent_at)
                if not isinstance(answer, Retry):
                    return answer
                retry = answer
            resynced = resynced or retry.resync
            attempt += int(not retry.resync)
            await self._sleep(retry.after)

    async def _pages(self, operation_id: str, query: Query | None) -> AsyncIterator[object]:
        params = dict(query or {})
        first = params.get("cursor")
        seen = {first} if isinstance(first, str) else set()
        while True:
            items, cursor = page_items(await self.request(operation_id, query=params), seen)
            for item in items:
                yield item
            if cursor is None:
                return
            params["cursor"] = cursor

    async def positions(self, query: Query | None = None) -> Reply:
        return await self.request("listPositions", query=query)

    async def balances(self, query: Query | None = None) -> Reply:
        return await self.request("listBalances", query=query)

    def fills(self, query: Query | None = None) -> AsyncIterator[object]:
        return self._pages("listFills", query)

    async def equity(self, query: Query | None = None) -> Reply:
        return await self.request("getEquity", query=query)
