from collections.abc import Callable
from typing import TypeVar

from atreides import Client, Refusal
from atreides.models import DeadMan

T = TypeVar('T')


def with_heartbeat(client: Client, account_id: str, work: Callable[[], T]) -> T:
    timeout_ms = 15_000
    armed = client.deadman.arm(account_id, timeout_ms)
    if isinstance(armed, Refusal) or not isinstance(armed.data, DeadMan) or not armed.data.armed:
        raise RuntimeError('Dead-man arming was not confirmed')
    heartbeat = client.deadman.keep_alive([account_id], timeout_ms)
    try:
        result = work()
    finally:
        heartbeat.stop()
    if heartbeat.last_error is not None:
        raise heartbeat.last_error
    # Clear only after work has confirmed that its orders are terminal.
    cleared = client.deadman.clear(account_id)
    if isinstance(cleared, Refusal) or not isinstance(cleared.data, DeadMan) or cleared.data.armed:
        raise RuntimeError('Dead-man clearing was not confirmed')
    return result
