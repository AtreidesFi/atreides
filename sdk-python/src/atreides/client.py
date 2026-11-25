"""Synchronous signed API client."""

import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from types import TracebackType
from typing import TypedDict

import httpx

from ._endpoints import DeadMan, Fees, Instruments, Orders, PlaceOrderInput, Venues, order_body
from ._http import Answer, ApiError, Body, HttpPolicy, Prepared, Query, Refusal, RequestKey, Retry, unix_ms
from ._keep_alive import KeepAlive, heartbeat_inputs
from ._route_endpoints import Routes
from .keys import ApiKeyFile, bound_key
from .models import AccountLinkChallenge
from .replay import StrategyReplay
from .stream import EventHandler, MarketHandler, ResetHandler, StreamHandle

Reply = Answer[object]


class WalletProof(TypedDict):
    signature: str
    scheme: str


def page_items(answer: Reply, seen: set[str]) -> tuple[list[object], str | None]:
    if isinstance(answer, Refusal):
        raise ApiError(answer.message, code=answer.code, request_id=answer.request_id)
    if not isinstance(answer.data, list):
        raise ApiError("Paginated API data must be a list", request_id=answer.request_id)
    next_cursor = answer.next_cursor
    if next_cursor is not None:
        if next_cursor in seen:
            raise ApiError("API pagination repeated a cursor", request_id=answer.request_id)
        seen.add(next_cursor)
    return answer.data, next_cursor


class Accounts:
    def __init__(self, client: "Client") -> None:
        self._client = client

    def list(self) -> Reply:
        return self._client.request("listAccounts")

    def link(self, venue: str, account_ref: str, sign: Callable[[str], WalletProof]) -> Reply:
        fields = {"venue": venue, "accountRef": account_ref}
        challenge = self._client.request("createAccountLinkChallenge", body=fields)
        if isinstance(challenge, Refusal):
            return challenge
        if not isinstance(challenge.data, AccountLinkChallenge):
            raise ApiError("Malformed account link challenge", request_id=challenge.request_id)
        message = challenge.data.message
        return self._client.request("linkAccount", body={**fields, "message": message, **sign(message)})


class SyncDeadMan(DeadMan[Reply]):
    def __init__(self, client: "Client") -> None:
        super().__init__(client.request)
        self._active: set[KeepAlive] = set()

    def keep_alive(self, account_ids: Sequence[str], timeout_ms: int) -> KeepAlive:
        accounts = heartbeat_inputs(account_ids, timeout_ms)
        handle = KeepAlive(accounts, timeout_ms, self.arm, self._active.discard)
        self._active.add(handle)
        return handle

    def stop_all(self) -> None:
        for handle in tuple(self._active):
            handle.stop()


class Client:
    def __init__(
        self,
        base_url: str,
        key: RequestKey | ApiKeyFile | None = None,
        *,
        timeout: float = 10.0,
        attempts: int = 3,
        base_delay: float = 0.25,
        transport: httpx.BaseTransport | None = None,
        now: Callable[[], int] = unix_ms,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._policy = HttpPolicy(base_url, bound_key(base_url, key), now, attempts, base_delay)
        self._client = httpx.Client(timeout=timeout, transport=transport, follow_redirects=False, trust_env=False)
        self._sleep = sleep
        self.raw = self
        self.strategy = StrategyReplay(self._policy)
        self.route = Routes(self.request, self._pages, now, self._sleep)
        self.venues: Venues[Reply] = Venues(self.request)
        self.instruments: Instruments[Reply, Iterator[object]] = Instruments(self.request, self._pages)
        self.fees: Fees[Reply] = Fees(self.request)
        self.accounts = Accounts(self)
        self.orders: Orders[Reply, Iterator[object]] = Orders(
            self.request, self._pages, self._prepare_order, self._send
        )
        self.deadman = SyncDeadMan(self)
        self._stream: StreamHandle | None = None

    @property
    def clock_skew_ms(self) -> int:
        return self._policy.clock_skew_ms

    def __enter__(self) -> "Client":
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()

    def close(self) -> None:
        self.deadman.stop_all()
        if self._stream is not None:
            self._stream.cancel()
        self._client.close()

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

    def request(
        self,
        operation_id: str,
        *,
        path: Mapping[str, str] | None = None,
        query: Query | None = None,
        body: Body = None,
        idempotency_key: str | None = None,
    ) -> Reply:
        return self._send(self._policy.prepare(operation_id, path, query, body, idempotency_key))

    def _send(self, prepared: Prepared) -> Reply:
        attempt = 0
        resynced = False
        while True:
            sent_at = self._policy.now()
            try:
                response = self._client.send(self._policy.request(prepared))
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
            self._sleep(retry.after)

    def _pages(self, operation_id: str, query: Query | None) -> Iterator[object]:
        params = dict(query or {})
        first = params.get("cursor")
        seen = {first} if isinstance(first, str) else set()
        while True:
            items, cursor = page_items(self.request(operation_id, query=params), seen)
            yield from items
            if cursor is None:
                return
            params["cursor"] = cursor

    def positions(self, query: Query | None = None) -> Reply:
        return self.request("listPositions", query=query)

    def balances(self, query: Query | None = None) -> Reply:
        return self.request("listBalances", query=query)

    def fills(self, query: Query | None = None) -> Iterator[object]:
        return self._pages("listFills", query)

    def equity(self, query: Query | None = None) -> Reply:
        return self.request("getEquity", query=query)
