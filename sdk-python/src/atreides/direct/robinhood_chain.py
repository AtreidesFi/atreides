"""Prepare and submit wallet-signed Robinhood Chain swaps."""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from importlib import import_module
from threading import Lock
from typing import Literal, Protocol, cast
from weakref import WeakKeyDictionary

from .._endpoints import PlaceOrderInput
from .._http import Refusal, unix_ms
from ..client import Client, Reply
from ..models import Order
from ._robinhood_queue import (
    RobinhoodNonceUncertain as RobinhoodNonceUncertain,
)
from ._robinhood_queue import (
    RobinhoodSubmitRefused,
    Sender,
    SenderLease,
    SenderRpc,
    SignedTransaction,
)

_CHAIN_ID = 4663
_EXECUTE = "0x3593564c"
_ERC20_APPROVE = "0x095ea7b3"
_PERMIT2_APPROVE = "0x87517c45"
_BEFORE_BROADCAST = {"invalid_request", "relay_refused", "relay_signer_mismatch", "user_action_expired"}
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


@dataclass(frozen=True, slots=True)
class RobinhoodSwapInput:
    account_id: str
    instrument_id: str
    side: Literal["buy", "sell"]
    amount: Decimal | str  # Cash notional for a buy, token amount for a sell.
    client_order_id: str | None = None
    slippage_bps: int | None = None


@dataclass(frozen=True, slots=True)
class RobinhoodCall:
    to: str
    data: str
    value: int


class RobinhoodSigner(Protocol):
    @property
    def address(self) -> str: ...

    def sign_transaction(self, call: RobinhoodCall, *, chain_id: int, nonce: int) -> str: ...


@dataclass(frozen=True, slots=True)
class RobinhoodReceipt:
    transaction_hash: str
    success: bool


class RobinhoodRpc(SenderRpc, Protocol):
    def send_raw_transaction(self, raw: str) -> str: ...

    def wait_for_receipt(self, tx_hash: str) -> RobinhoodReceipt: ...


@dataclass(frozen=True, slots=True, weakref_slot=True, eq=False)
class RobinhoodSwapQuote:
    order_id: str
    client_order_id: str
    floor_out: str
    fee_amount: str
    fee_token: str
    expires_at_ms: int


@dataclass(slots=True)
class _Plan:
    account_id: str
    instrument_id: str
    calls: tuple[RobinhoodCall, ...]
    approvals: dict[int, str] = field(default_factory=dict)
    swap_hash: str | None = None


def _invalid() -> ValueError:
    return ValueError("Invalid prepared Robinhood swap")


def _address(value: object) -> str:
    if not isinstance(value, str) or re.fullmatch(r"0x[0-9a-fA-F]{40}", value) is None:
        raise _invalid()
    return value.lower()


def _hex(value: object) -> str:
    if not isinstance(value, str) or re.fullmatch(r"0x(?:[0-9a-fA-F]{2})*", value) is None:
        raise _invalid()
    return value.lower()


def _uint(value: object) -> int:
    if not isinstance(value, str) or re.fullmatch(r"(?:0|[1-9][0-9]{0,77})", value) is None:
        raise _invalid()
    result = int(value)
    if result >= 2**256:
        raise _invalid()
    return result


def _mapping(value: object) -> Mapping[str, object]:
    if not isinstance(value, dict):
        raise _invalid()
    return cast(Mapping[str, object], value)


def _asset(instrument: str) -> tuple[str, str]:
    match = re.fullmatch(r"robinhood-chain:(0x[0-9a-fA-F]{40}):(USDG|WETH|ETH)", instrument)
    if match is None:
        raise _invalid()
    return _address(match[1]), match[2]


def _word(data: str, index: int) -> str:
    encoded = data[10 + index * 64 : 74 + index * 64]
    if re.fullmatch(r"[0-9a-f]{64}", encoded) is None:
        raise _invalid()
    return encoded


def _address_word(data: str, index: int) -> str:
    encoded = _word(data, index)
    if encoded[:24] != "0" * 24:
        raise _invalid()
    return _address("0x" + encoded[24:])


def _uint_word(data: str, index: int) -> int:
    return int(_word(data, index), 16)


def _calls(value: object) -> tuple[RobinhoodCall, ...]:
    if not isinstance(value, list) or not 1 <= len(value) <= 3:
        raise _invalid()
    rows = (_mapping(item) for item in value)
    return tuple(RobinhoodCall(_address(row.get("to")), _hex(row.get("data")), _uint(row.get("value"))) for row in rows)


def _time(value: object) -> int:
    if isinstance(value, str):
        value = datetime.fromisoformat(value)
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise _invalid()
    delta = value - _EPOCH
    result = (delta.days * 86_400 + delta.seconds) * 1_000 + delta.microseconds // 1_000
    if result < 0:
        raise _invalid()
    return result


def _match(order: Order, account_id: str, order_id: str, client_order_id: str, instrument_id: str) -> bool:
    return (
        order.venue == "robinhood-chain"
        and str(order.accountId.root) == account_id
        and str(order.orderId.root) == order_id
        and order.clientOrderId.root == client_order_id
        and order.instrumentId == instrument_id
    )


def _order(answer: Reply) -> Order:
    if isinstance(answer, Refusal):
        raise RuntimeError(answer.message)
    return Order.model_validate(answer.data)


def _signed(raw: str, call: RobinhoodCall, sender: str, nonce: int) -> SignedTransaction:
    try:
        account = import_module("eth_account").Account
        typed = import_module("eth_account.typed_transactions").TypedTransaction
        keccak = import_module("eth_utils").keccak
        hex_bytes = import_module("hexbytes").HexBytes
    except ImportError as exc:
        raise RuntimeError("Install atreides[robinhood-chain] for Robinhood transaction validation") from exc

    raw = _hex(raw)
    encoded = hex_bytes(raw)
    tx = cast(Mapping[str, object], typed.from_bytes(encoded).as_dict())
    to = tx.get("to")
    data = tx.get("data")
    if (
        tx.get("type") != 2
        or tx.get("chainId") != _CHAIN_ID
        or tx.get("nonce") != nonce
        or not isinstance(to, bytes)
        or "0x" + to.hex() != call.to
        or not isinstance(data, bytes)
        or "0x" + data.hex() != call.data
        or tx.get("value") != call.value
        or _address(account.recover_transaction(raw)) != sender
    ):
        raise ValueError("The wallet signed different Robinhood transaction terms")
    return SignedTransaction(raw, "0x" + bytes(keccak(encoded)).hex())


class RobinhoodChainDirect:
    def __init__(
        self,
        client: Client,
        signer: RobinhoodSigner,
        rpc: RobinhoodRpc,
        *,
        router: str,
        permit2: str,
        usdg: str,
        weth: str,
        now: Callable[[], int] = unix_ms,
    ) -> None:
        self._client = client
        self._signer = signer
        self._address = _address(signer.address)
        self._rpc = rpc
        self._router = _address(router)
        self._permit2 = _address(permit2)
        self._usdg = _address(usdg)
        self._weth = _address(weth)
        self._now = now
        self._sender = Sender(self._address, rpc, now)
        self._plans: WeakKeyDictionary[RobinhoodSwapQuote, _Plan] = WeakKeyDictionary()
        self._plan_lock = Lock()

    def _validate_approvals(self, calls: tuple[RobinhoodCall, ...], input_token: str, deadline: int) -> None:
        seen: set[str] = set()
        for call in calls:
            selector = call.data[:10]
            if call.value != 0 or selector in seen:
                raise _invalid()
            seen.add(selector)
            if selector == _ERC20_APPROVE:
                if (
                    len(call.data) != 138
                    or call.to != input_token
                    or _address_word(call.data, 0) != self._permit2
                    or _uint_word(call.data, 1) == 0
                    or _PERMIT2_APPROVE in seen
                ):
                    raise _invalid()
            elif selector == _PERMIT2_APPROVE:
                if (
                    len(call.data) != 266
                    or call.to != self._permit2
                    or _address_word(call.data, 0) != input_token
                    or _address_word(call.data, 1) != self._router
                    or not 0 < _uint_word(call.data, 2) < 2**160
                    or not deadline <= _uint_word(call.data, 3) < 2**48
                ):
                    raise _invalid()
            else:
                raise _invalid()

    def prepare(self, swap: RobinhoodSwapInput) -> RobinhoodSwapQuote:
        token, cash = _asset(swap.instrument_id)
        request = PlaceOrderInput(
            accountId=swap.account_id, instrumentId=swap.instrument_id, side=swap.side, type="market"
        )
        if swap.side == "buy":
            request["notional"] = swap.amount
        else:
            request["size"] = swap.amount
        if swap.client_order_id is not None:
            request["clientOrderId"] = swap.client_order_id
        if swap.slippage_bps is not None:
            request["slippageBps"] = swap.slippage_bps
        placed = self._client.orders.place(request)
        order = _order(placed.result)
        order_id = str(order.orderId.root)
        action = order.userAction
        if (
            not _match(order, swap.account_id, order_id, placed.client_order_id, swap.instrument_id)
            or order.side != swap.side
            or order.state != "pending"
            or action is None
            or action.kind != "sign_transaction"
        ):
            raise _invalid()
        plan = _mapping(action.request)
        quote = _mapping(plan.get("plan"))
        deadline = plan.get("deadline")
        if (
            plan.get("kind") != "evm_calls"
            or plan.get("chainId") != _CHAIN_ID
            or plan.get("orderId") != order_id
            or plan.get("atomicRequired") is not False
            or type(deadline) is not int
            or not 0 < deadline < 2**43
            or quote.get("deadline") != deadline
        ):
            raise _invalid()
        expires_at = min(_time(action.expiresAt.root), _time(plan.get("expiresAt")), deadline * 1_000)
        if self._now() >= expires_at:
            raise _invalid()
        calls = _calls(plan.get("calls"))
        execution = calls[-1]
        native_buy = cash == "ETH" and swap.side == "buy"
        if (
            execution.to != self._router
            or not execution.data.startswith(_EXECUTE)
            or _uint_word(execution.data, 2) != deadline
            or (execution.value <= 0 or len(calls) != 1 if native_buy else execution.value != 0)
        ):
            raise _invalid()
        fee_token = self._usdg if cash == "USDG" else self._weth
        self._validate_approvals(calls[:-1], token if swap.side == "sell" else fee_token, deadline)
        floor_out = _uint(quote.get("floorOut"))
        if floor_out == 0 or _address(quote.get("feeToken")) != fee_token:
            raise _invalid()
        view = RobinhoodSwapQuote(
            order_id,
            placed.client_order_id,
            str(floor_out),
            str(_uint(quote.get("feeAmount"))),
            fee_token,
            expires_at,
        )
        with self._plan_lock:
            self._plans[view] = _Plan(str(order.accountId.root), order.instrumentId, calls)
        return view

    def _sign(self, call: RobinhoodCall, nonce: int) -> SignedTransaction:
        raw = self._signer.sign_transaction(call, chain_id=_CHAIN_ID, nonce=nonce)
        return _signed(raw, call, self._address, nonce)

    def _send_approval(self, lease: SenderLease, expires_at: int, plan: _Plan, index: int, call: RobinhoodCall) -> None:
        def publish(tx: SignedTransaction) -> str:
            plan.approvals[index] = tx.tx_hash
            if _hex(self._rpc.send_raw_transaction(tx.raw)) != tx.tx_hash:
                raise RuntimeError("Unexpected approval transaction hash")
            return tx.tx_hash

        tx_hash = plan.approvals.get(index)
        if tx_hash is None:
            tx_hash = lease.send(expires_at, lambda nonce: self._sign(call, nonce), publish)
        receipt = self._rpc.wait_for_receipt(tx_hash)
        if not receipt.success or _hex(receipt.transaction_hash) != tx_hash:
            raise RuntimeError("Robinhood approval failed")

    def submit(self, quote: RobinhoodSwapQuote) -> Order:
        with self._plan_lock:
            plan = self._plans.get(quote)
        if plan is None:
            raise _invalid()

        def submit_locked(lease: SenderLease) -> Order:
            order = _order(self._client.orders.get(plan.account_id, quote.client_order_id))
            if not _match(order, plan.account_id, quote.order_id, quote.client_order_id, plan.instrument_id):
                raise _invalid()
            action = order.userAction
            if action is None and order.venueOrderId is not None:
                return order
            if (
                order.state != "pending"
                or action is None
                or action.kind != "sign_transaction"
                or plan.swap_hash is not None
            ):
                raise RuntimeError("The Robinhood order is awaiting its submission outcome")
            current = _mapping(action.request)
            deadline = current.get("deadline")
            if (
                current.get("kind") != "evm_calls"
                or current.get("chainId") != _CHAIN_ID
                or current.get("orderId") != quote.order_id
                or current.get("atomicRequired") is not False
                or type(deadline) is not int
                or deadline != _uint_word(plan.calls[-1].data, 2)
                or _calls(current.get("calls")) != plan.calls
            ):
                raise _invalid()
            expires_at = min(quote.expires_at_ms, _time(action.expiresAt.root), _time(current.get("expiresAt")))
            for index, call in enumerate(plan.calls[:-1]):
                self._send_approval(lease, expires_at, plan, index, call)

            def publish(tx: SignedTransaction) -> Order:
                plan.swap_hash = tx.tx_hash
                answer = self._client.request(
                    "submitOrderTx",
                    path={"clientOrderId": quote.client_order_id},
                    body={"accountId": plan.account_id, "payload": {"kind": "raw_tx", "rawTx": tx.raw}},
                )
                if isinstance(answer, Refusal) and answer.code in _BEFORE_BROADCAST:
                    plan.swap_hash = None
                    raise RobinhoodSubmitRefused(answer.message)
                placed = _order(answer)
                if (
                    not _match(placed, plan.account_id, quote.order_id, quote.client_order_id, plan.instrument_id)
                    or placed.userAction is not None
                    or ((placed.venueOrderId or "").lower() != tx.tx_hash and not self._rpc.has_transaction(tx.tx_hash))
                ):
                    raise RuntimeError("Robinhood submission returned no matching transaction")
                return placed

            return lease.send(expires_at, lambda nonce: self._sign(plan.calls[-1], nonce), publish)

        return self._sender.run(submit_locked)

    def swap(self, swap: RobinhoodSwapInput) -> Order:
        return self.submit(self.prepare(swap))

    def retry_pending(self) -> str | None:
        return self._sender.retry()
