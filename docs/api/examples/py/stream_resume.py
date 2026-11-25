from collections.abc import Awaitable, Callable

from atreides import AsyncClient, Client
from atreides.stream import StreamHandle


def resume_user_stream(
    client: Client | AsyncClient,
    cursor: str | None,
    apply: Callable[[dict[str, object]], None | Awaitable[None]],
    refresh: Callable[[str], None | Awaitable[None]],
) -> StreamHandle:
    # Apply updates and persist cursors before returning. On reset, first re-read orders and fills.
    return client.stream(['user'], since=cursor, on_event=apply, on_reset=refresh)
