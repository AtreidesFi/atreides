"""Signed, lockstep historical bot replay."""

import asyncio
import json
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from typing import cast

from ._http import ApiError, HttpPolicy
from .stream import Connect, StreamSocket, _dial

_MAX_FRAME = 1 << 20


class Replay:
    def __init__(self, socket: StreamSocket) -> None:
        self._socket = socket
        self._reading = False
        self._finished = False
        self.run_id: str | None = None
        self.result_id: str | None = None

    async def send(self, frame: Mapping[str, object]) -> None:
        if self.run_id is None or self._finished or frame.get("t") not in {"advance", "order", "cancel", "close"}:
            raise ApiError("Replay must be open to send this frame")
        await self._socket.send(json.dumps(dict(frame), allow_nan=False, separators=(",", ":")))

    async def receive(self) -> dict[str, object]:
        if self._reading or self._finished:
            raise ApiError("Replay already has a reader or is finished")
        self._reading = True
        try:
            raw = await self._socket.recv()
            if not isinstance(raw, str) or len(raw) > _MAX_FRAME:
                raise ApiError("Malformed replay frame")
            try:
                value: object = json.loads(raw)
            except ValueError:
                raise ApiError("Malformed replay frame") from None
            if not isinstance(value, dict) or not isinstance(value.get("t"), str):
                raise ApiError("Malformed replay frame")
            frame = cast(dict[str, object], value)
            if frame["t"] == "opened":
                if self.run_id is not None or not isinstance(frame.get("runId"), str):
                    raise ApiError("Malformed replay admission")
                self.run_id = cast(str, frame["runId"])
            if frame["t"] == "finished":
                if not isinstance(frame.get("resultId"), str):
                    raise ApiError("Malformed replay result")
                self.result_id = cast(str, frame["resultId"])
                self._finished = True
            return frame
        finally:
            self._reading = False

    async def finish(self) -> str:
        if self.result_id is None:
            await self.send({"t": "close"})
        while self.result_id is None:
            frame = await self.receive()
            if frame["t"] == "error":
                raise _refusal(frame)
        return self.result_id


def _refusal(frame: Mapping[str, object]) -> ApiError:
    code = frame.get("code")
    message = frame.get("message")
    return ApiError(
        message if isinstance(message, str) else "Replay refused",
        code=code if isinstance(code, str) else None,
    )


class StrategyReplay:
    def __init__(self, policy: HttpPolicy, dial: Connect = _dial) -> None:
        self._policy = policy
        self._dial = dial

    @asynccontextmanager
    async def replay(self, settings: Mapping[str, object]) -> AsyncIterator[Replay]:
        if self._policy.key is None:
            raise ApiError("Replay requires a signed API key")
        target = "/v1/lab/replay"
        scheme = "wss" if self._policy.origin.startswith("https:") else "ws"
        uri = f"{scheme}:{self._policy.origin.split(':', 1)[1]}{target}"
        headers = self._policy.signed_headers("GET", target, b"")
        async with self._dial(uri, headers) as socket:
            session = Replay(socket)
            async with asyncio.timeout(30):
                await socket.send(json.dumps({**settings, "t": "open"}, allow_nan=False, separators=(",", ":")))
                opened = await session.receive()
            if opened["t"] == "error":
                raise _refusal(opened)
            if session.run_id is None:
                raise ApiError("Expected replay admission")
            yield session
