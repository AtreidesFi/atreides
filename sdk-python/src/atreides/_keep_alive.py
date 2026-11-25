"""Periodic dead-man refresh with explicit ownership and cleanup."""

import asyncio
from collections.abc import Awaitable, Callable, Sequence
from threading import Event, Thread

from ._http import Answer, ApiError, Refusal
from .stream import StreamHandle


def heartbeat_inputs(account_ids: Sequence[str], timeout_ms: int) -> tuple[str, ...]:
    if type(timeout_ms) is not int or not 5000 <= timeout_ms <= 600000:
        raise ValueError("Dead-man timeout must be from 5000 to 600000 milliseconds")
    accounts = tuple(dict.fromkeys(account_ids))
    if not accounts or any(not account for account in accounts):
        raise ValueError("A dead-man heartbeat needs account IDs")
    return accounts


def _refusal(answer: Answer[object]) -> ApiError | None:
    if isinstance(answer, Refusal):
        return ApiError(answer.message, code=answer.code, request_id=answer.request_id)
    return None


class KeepAlive:
    def __init__(
        self,
        accounts: tuple[str, ...],
        timeout_ms: int,
        arm: Callable[[str, int], Answer[object]],
        remove: Callable[["KeepAlive"], None],
    ) -> None:
        self.last_error: ApiError | None = None
        self._stopped = Event()
        self._remove = remove

        def run() -> None:
            while not self._stopped.is_set():
                failure: ApiError | None = None
                for account in accounts:
                    if self._stopped.is_set():
                        return
                    try:
                        failure = _refusal(arm(account, timeout_ms)) or failure
                    except ApiError as error:
                        failure = error
                self.last_error = failure
                if self._stopped.wait(timeout_ms / 3000):
                    return

        self._thread = Thread(target=run, daemon=True, name="atreides-deadman")
        self._thread.start()

    def stop(self) -> None:
        self._stopped.set()
        self._thread.join()
        self._remove(self)


class AsyncKeepAlive:
    def __init__(
        self,
        accounts: tuple[str, ...],
        timeout_ms: int,
        arm: Callable[[str, int], Awaitable[Answer[object]]],
        stream: Callable[[], StreamHandle | None],
        remove: Callable[["AsyncKeepAlive"], None],
    ) -> None:
        self.last_error: ApiError | None = None
        self._remove = remove

        async def run() -> None:
            armed = False
            while True:
                handle = stream()
                # The first REST call sets the requested timeout. Later heartbeats can refresh it.
                acknowledged = (
                    await handle.heartbeat(accounts, min(2.0, timeout_ms / 6000)) if armed and handle else set()
                )
                failure: ApiError | None = None
                for account in accounts:
                    if account in acknowledged:
                        continue
                    try:
                        failure = _refusal(await arm(account, timeout_ms)) or failure
                    except ApiError as error:
                        failure = error
                armed = failure is None
                self.last_error = failure
                await asyncio.sleep(timeout_ms / 3000)

        self._task = asyncio.get_running_loop().create_task(run())

    async def stop(self) -> None:
        self._task.cancel()
        await asyncio.gather(self._task, return_exceptions=True)
        self._remove(self)
