"""Serialisation and uncertain-outcome recovery for a Robinhood Chain sender."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from threading import Lock
from typing import Protocol, TypeVar

T = TypeVar("T")


class RobinhoodNonceUncertain(RuntimeError):
    def __init__(self, nonce: int, tx_hash: str) -> None:
        super().__init__("The signed Robinhood transaction is awaiting confirmation")
        self.nonce = nonce
        self.tx_hash = tx_hash


class RobinhoodSubmitRefused(RuntimeError):
    """An authoritative pre-broadcast refusal from our submit endpoint."""


@dataclass(frozen=True)
class SignedTransaction:
    raw: str
    tx_hash: str


class SenderRpc(Protocol):
    def pending_nonce(self, sender: str) -> int: ...

    def has_transaction(self, tx_hash: str) -> bool: ...


@dataclass(frozen=True)
class _Pending:
    signed: SignedTransaction
    nonce: int
    expires_at_ms: int
    publish: Callable[[SignedTransaction], object]


@dataclass
class _SenderState:
    lock: Lock = field(default_factory=Lock)
    next_nonce: int | None = None
    pending: _Pending | None = None

    def accept(self, nonce: int) -> None:
        self.next_nonce = nonce + 1
        self.pending = None


_senders: dict[str, _SenderState] = {}
_senders_lock = Lock()


def _state(sender: str) -> _SenderState:
    key = sender.lower()
    with _senders_lock:
        if key not in _senders:
            _senders[key] = _SenderState()
        return _senders[key]


def _seen(rpc: SenderRpc, pending: _Pending) -> bool:
    try:
        return rpc.has_transaction(pending.signed.tx_hash)
    except Exception as exc:
        raise RobinhoodNonceUncertain(pending.nonce, pending.signed.tx_hash) from exc


class Sender:
    def __init__(self, sender: str, rpc: SenderRpc, clock: Callable[[], int]) -> None:
        self._sender = sender
        self._rpc = rpc
        self._clock = clock

    def run(self, work: Callable[[SenderLease], T]) -> T:
        state = _state(self._sender)
        with state.lock:
            if (pending := state.pending) is not None:
                if not _seen(self._rpc, pending):
                    raise RobinhoodNonceUncertain(pending.nonce, pending.signed.tx_hash)
                state.accept(pending.nonce)
            return work(SenderLease(self._sender, self._rpc, self._clock, state))

    def retry(self) -> str | None:
        state = _state(self._sender)
        with state.lock:
            pending = state.pending
            if pending is None:
                return None
            if not _seen(self._rpc, pending):
                if self._clock() >= pending.expires_at_ms:
                    raise RobinhoodNonceUncertain(pending.nonce, pending.signed.tx_hash)
                try:
                    pending.publish(pending.signed)
                except Exception as exc:
                    raise RobinhoodNonceUncertain(pending.nonce, pending.signed.tx_hash) from exc
            state.accept(pending.nonce)
            return pending.signed.tx_hash


class SenderLease:
    def __init__(self, sender: str, rpc: SenderRpc, clock: Callable[[], int], state: _SenderState) -> None:
        self._sender = sender
        self._rpc = rpc
        self._clock = clock
        self._state = state

    def send(
        self,
        expires_at_ms: int,
        sign: Callable[[int], SignedTransaction],
        publish: Callable[[SignedTransaction], T],
    ) -> T:
        if self._clock() >= expires_at_ms:
            raise RuntimeError("The Robinhood swap quote expired")
        observed = self._rpc.pending_nonce(self._sender)
        if type(observed) is not int or not 0 <= observed < 2**64 - 1:
            raise ValueError("Invalid Robinhood pending nonce")
        nonce = max(observed, self._state.next_nonce or 0)
        if nonce >= 2**64 - 1:
            raise ValueError("Invalid Robinhood next nonce")
        signed = sign(nonce)
        if self._clock() >= expires_at_ms:
            raise RuntimeError("The Robinhood swap quote expired during signing")
        try:
            result = publish(signed)
        except RobinhoodSubmitRefused:
            raise
        except Exception as exc:
            self._state.pending = _Pending(signed, nonce, expires_at_ms, publish)
            raise RobinhoodNonceUncertain(nonce, signed.tx_hash) from exc
        self._state.accept(nonce)
        return result
