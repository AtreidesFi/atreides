"""Direct Pacifica order signing with the caller's official SDK signer."""

import json
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from decimal import Decimal
from typing import Literal

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from .._http import ApiError, unix_ms
from .template import DirectContext, DirectFees, DirectStart, FeeField, OrderType, PublishedRate, TimeInForce

Operation = Literal["create_order", "create_market_order", "create_stop_order", "set_position_tpsl"]
Value = str | bool | int | None | list["Value"] | dict[str, "Value"]
SdkSigner = Callable[[dict[str, str | int], dict[str, Value]], tuple[str, str]]
_BASE = Decimal("0.00001")
_CAP = Decimal("0.0001")
_CODE = re.compile(r"[a-zA-Z0-9]{3,16}")
_B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
_OPERATIONS = frozenset(("create_order", "create_market_order", "create_stop_order", "set_position_tpsl"))
_RESERVED = frozenset(
    ("account", "agent_wallet", "signature", "timestamp", "expiry_window", "type", "data", "builder_code")
)


@dataclass(frozen=True)
class SignedOrder:
    operation: Operation
    canonical: str
    signature: str
    body: str


def _decode(value: str, length: int) -> bytes:
    if not length <= len(value) <= (length * 138 + 99) // 100:
        raise ValueError("Invalid Pacifica base58 value length")
    number = 0
    for char in value:
        digit = _B58.find(char)
        if digit < 0:
            raise ValueError("Invalid Pacifica base58 value")
        number = number * 58 + digit
    leading = len(value) - len(value.lstrip("1"))
    result = bytes(leading) + number.to_bytes((number.bit_length() + 7) // 8, "big")
    if len(result) != length:
        raise ValueError("Invalid Pacifica base58 value length")
    return result


def _checked(value: Value) -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            if not isinstance(key, str) or key == "builder_code":
                raise ApiError("Reserved Pacifica order field", code="invalid_request")
            _checked(child)
    elif isinstance(value, list):
        for child in value:
            _checked(child)
    elif value is not None and not isinstance(value, (str, bool, int)):
        raise ApiError("Invalid Pacifica order value", code="invalid_request")


def sign_order(
    operation: Operation,
    account: str,
    timestamp: int,
    data: Mapping[str, Value],
    fee: FeeField,
    sign: SdkSigner,
    *,
    agent_wallet: str | None = None,
) -> SignedOrder:
    """Use `lambda header, data: sdk.sign_message(header, data, keypair)` for the SDK callback."""
    if operation not in _OPERATIONS or type(timestamp) is not int or not 0 <= timestamp < 2**53:
        raise ApiError("Invalid Pacifica order operation or timestamp", code="invalid_request")
    if _RESERVED.intersection(data):
        raise ApiError("Reserved Pacifica order field", code="invalid_request")
    payload = dict(data)
    _checked(payload)
    if operation == "set_position_tpsl" and not payload.get("take_profit") and not payload.get("stop_loss"):
        raise ApiError("Pacifica position TP/SL requires a trigger", code="invalid_request")
    kind = fee["kind"]
    if kind == "per_code":
        code = fee.get("code")
        if code is None or _CODE.fullmatch(code) is None or fee.get("rate") != _BASE:
            raise ApiError("Invalid Pacifica builder code or rate", code="fee_code_missing")
        payload["builder_code"] = code
    elif kind != "none":
        raise ApiError("Invalid Pacifica fee field", code="fee_code_missing")
    elif operation != "set_position_tpsl" and payload.get("reduce_only") is not True:
        raise ApiError("A Pacifica open requires fee approval", code="fee_approval_missing")
    header: dict[str, str | int] = {"type": operation, "timestamp": timestamp, "expiry_window": 5000}
    canonical = json.dumps({**header, "data": payload}, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    try:
        _decode(account, 32)
        public = Ed25519PublicKey.from_public_bytes(_decode(account if agent_wallet is None else agent_wallet, 32))
        message, signature = sign(dict(header), json.loads(json.dumps(payload)))
        if message != canonical:
            raise ValueError("Pacifica SDK changed the signed payload")
        public.verify(_decode(signature, 64), canonical.encode("utf-8"))
    except (InvalidSignature, TypeError, ValueError) as error:
        raise ApiError("Invalid Pacifica SDK signature", code="invalid_request") from error
    body: dict[str, Value] = {
        **payload,
        "account": account,
        "timestamp": timestamp,
        "expiry_window": 5000,
        "signature": signature,
    }
    if agent_wallet is not None:
        body["agent_wallet"] = agent_wallet
    return SignedOrder(operation, canonical, signature, json.dumps(body, sort_keys=True, separators=(",", ":")))


class PacificaDirect:
    venue = "pacifica"

    def __init__(self, *, now: Callable[[], int] = unix_ms) -> None:
        self._fees = DirectFees(self.venue, True, self._encode, cap_for=lambda _cls: _CAP, now=now)

    @staticmethod
    def _encode(rate: PublishedRate, code: str | None, _type: OrderType, _tif: TimeInForce) -> FeeField:
        if code is None or _CODE.fullmatch(code) is None or rate.rate != _BASE:
            raise ApiError("Pacifica builder code or rate is unavailable", code="fee_code_missing")
        return {"kind": "per_code", "code": code, "rate": rate.rate}

    def start(self, context: DirectContext) -> DirectStart:
        return self._fees.start(context)

    def refresh(self) -> DirectStart:
        return self._fees.refresh()

    def fee_fields(self, cls: str, order_type: OrderType, tif: TimeInForce, reduce_only: bool) -> FeeField:
        if cls not in ("perp", "rwa_perp"):
            raise ApiError("Unsupported Pacifica class", code="invalid_request")
        return self._fees.fee_fields(cls, order_type, tif, reduce_only)
