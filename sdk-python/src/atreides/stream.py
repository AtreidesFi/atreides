"""Resumable signed streams. The cursor advances only after the handler returns."""

import asyncio
import inspect
import json
import re
from collections.abc import Awaitable, Callable, Sequence
from contextlib import AbstractAsyncContextManager
from typing import Protocol, cast
from uuid import uuid4

from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed, InvalidStatus

from ._http import ApiError, HttpPolicy

_SEQ = re.compile(r"[0-9]+")


class StreamSocket(Protocol):
    async def send(self, message: str) -> None: ...
    async def recv(self) -> str | bytes: ...


Connect = Callable[[str, dict[str, str]], AbstractAsyncContextManager[StreamSocket]]
EventHandler = Callable[[dict[str, object]], None | Awaitable[None]]
ResetHandler = Callable[[str], None | Awaitable[None]]
MarketHandler = Callable[[str, object], None | Awaitable[None]]


class _NoRedirect(connect):
    def process_redirect(self, exc: Exception) -> Exception | str:
        # The upgrade is signed for this target and its configured origin.
        return exc


def _dial(uri: str, headers: dict[str, str]) -> AbstractAsyncContextManager[StreamSocket]:
    return _NoRedirect(uri, additional_headers=headers, proxy=None, open_timeout=10, max_size=1 << 20)


async def _await_callback(result: None | Awaitable[None]) -> None:
    if inspect.isawaitable(result):
        await result


def _seq(value: object) -> str:
    if not isinstance(value, str) or _SEQ.fullmatch(value) is None:
        raise ApiError("Stream sequence is not a decimal cursor")
    return value


def _later(a: str, b: str) -> bool:
    left = a.lstrip("0")
    right = b.lstrip("0")
    return (len(left), left) > (len(right), right)


class StreamHandle:
    def __init__(
        self,
        policy: HttpPolicy,
        channels: Sequence[str],
        *,
        since: str | None,
        on_event: EventHandler,
        on_reset: ResetHandler,
        on_market: MarketHandler | None,
        dial: Connect = _dial,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        if not channels:
            raise ValueError("At least one stream channel is required")
        self._policy = policy
        self._channels = tuple(channels)
        self._last_seq = _seq(since) if since is not None else None
        self._on_event = on_event
        self._on_reset = on_reset
        self._on_market = on_market
        self._dial = dial
        self._sleep = sleep
        self._socket: StreamSocket | None = None
        self._heartbeat: dict[str, asyncio.Future[set[str]]] = {}
        self._task = asyncio.get_running_loop().create_task(self._run())

    @property
    def last_seq(self) -> str | None:
        return self._last_seq

    def cancel(self) -> None:
        loop = self._task.get_loop()
        if not loop.is_closed():
            loop.call_soon_threadsafe(self._task.cancel)

    async def close(self) -> None:
        self._task.cancel()
        await asyncio.gather(self._task, return_exceptions=True)

    async def wait(self) -> None:
        await self._task

    async def heartbeat(self, account_ids: Sequence[str], timeout: float) -> set[str]:
        socket = self._socket
        if socket is None:
            return set()
        identifier = uuid4().hex
        future: asyncio.Future[set[str]] = asyncio.get_running_loop().create_future()
        self._heartbeat[identifier] = future
        try:
            await socket.send(json.dumps({"op": "heartbeat", "id": identifier, "accountIds": list(account_ids)}))
            return await asyncio.wait_for(future, timeout)
        except (ConnectionClosed, OSError, TimeoutError):
            return set()
        finally:
            self._heartbeat.pop(identifier, None)

    async def _subscribe(self, socket: StreamSocket, channels: Sequence[str]) -> None:
        frame: dict[str, object] = {"op": "subscribe", "id": uuid4().hex, "channels": list(channels)}
        if self._last_seq is not None and "user" in channels:
            frame["since"] = self._last_seq
        await socket.send(json.dumps(frame))

    def _resolve_heartbeat(self, frame: dict[str, object]) -> None:
        identifier = frame.get("id")
        future = self._heartbeat.get(identifier) if isinstance(identifier, str) else None
        if future is None or future.done():
            return
        accepted: set[str] = set()
        results = frame.get("results")
        if isinstance(results, list):
            for row in results:
                if isinstance(row, dict) and row.get("ok") is True and isinstance(row.get("accountId"), str):
                    accepted.add(row["accountId"])
        future.set_result(accepted)

    async def _message(self, socket: StreamSocket, frame: dict[str, object]) -> bool:
        kind = frame.get("type")
        if kind == "event" and frame.get("channel") == "user":
            seq = _seq(frame.get("seq"))
            if self._last_seq is None or _later(seq, self._last_seq):
                await _await_callback(self._on_event(frame))
                self._last_seq = seq
        elif kind == "reset" and frame.get("channel") == "user":
            latest = _seq(frame.get("latestSeq"))
            await _await_callback(self._on_reset(latest))
            self._last_seq = latest
            await self._subscribe(socket, ("user",))
        elif kind in {"market", "broadcast"} and self._on_market is not None:
            channel = frame.get("channel")
            if isinstance(channel, str):
                await _await_callback(self._on_market(channel, frame.get("data")))
        elif kind == "ack" and frame.get("op") == "heartbeat":
            self._resolve_heartbeat(frame)
        elif kind == "closing":
            if frame.get("reason") == "revoked":
                raise ApiError("Stream credentials were revoked", code="unauthenticated")
            return False
        return True

    async def _run(self) -> None:
        scheme = "wss" if self._policy.origin.startswith("https:") else "ws"
        origin = self._policy.origin.split(":", 1)[1]
        target = "/v1/stream"
        uri = f"{scheme}:{origin}{target}"
        delay = 0.25
        while True:
            headers = self._policy.signed_headers("GET", target, b"")
            reconnect_after = 0.0
            try:
                async with self._dial(uri, headers) as socket:
                    self._socket = socket
                    await self._subscribe(socket, self._channels)
                    while True:
                        message = await socket.recv()
                        try:
                            value: object = json.loads(message)
                        except (UnicodeError, ValueError):
                            raise ApiError("Malformed stream frame") from None
                        if not isinstance(value, dict):
                            raise ApiError("Malformed stream frame")
                        frame = cast(dict[str, object], value)
                        if not await self._message(socket, frame):
                            retry_ms = frame.get("reconnectAfterMs")
                            if type(retry_ms) is int and retry_ms >= 0:
                                reconnect_after = retry_ms / 1000
                            break
                        delay = 0.25
            except InvalidStatus as error:
                status = error.response.status_code
                if status not in {429, 502, 503, 504}:
                    raise ApiError("Stream upgrade failed", status=status) from error
            except ConnectionClosed as error:
                code = error.rcvd.code if error.rcvd is not None else None
                if code in {4001, 4003, 4009}:
                    refusal = "forbidden" if code == 4003 else "unauthenticated"
                    raise ApiError("Stream closed by its access policy", status=code, code=refusal) from error
            except (OSError, TimeoutError):
                pass
            finally:
                self._socket = None
                for future in self._heartbeat.values():
                    if not future.done():
                        future.set_result(set())
            await self._sleep(max(delay, reconnect_after))
            delay = min(30.0, delay * 2)
