"""Direct Aster Futures/Spot v3 attribution and exact Message signing."""

import json
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from decimal import Decimal, InvalidOperation
from threading import RLock
from typing import Literal
from urllib.parse import urlencode

from .._http import ApiError, unix_ms
from ..dec import format_dec, parse_dec
from .template import DirectContext, DirectFees, DirectStart, FeeField, OrderType, PublishedRate, TimeInForce

Product = Literal["futures", "spot"]
Network = Literal["mainnet", "testnet"]
BuilderPath = Literal["/fapi/v3/builder", "/api/v3/builder"]
MessageParams = Sequence[tuple[str, str | int | bool]]
MessageSigner = Callable[[Mapping[str, object]], str]
_CLASS_CAP = Decimal("0.0002")
_STEP = Decimal("0.00001")
_ZERO = "0x" + "0" * 40
_VENUE_CAP = {"futures": Decimal("0.001"), "spot": Decimal("0.01")}


@dataclass(frozen=True)
class BuilderRequest:
    method: Literal["GET"]
    path: BuilderPath
    user: str


@dataclass(frozen=True)
class SignedMessage:
    msg: str
    signature: str
    chain_id: int
    body: str


def _address(raw: object) -> str | None:
    if isinstance(raw, str) and raw != _ZERO and re.fullmatch(r"0x[0-9a-fA-F]{40}", raw):
        return raw.lower()
    return None


def _approval(body: str, user: str, builder: str, product: Product) -> Decimal | None:
    if len(body) > 1_000_000:
        return None
    rows = json.loads(body, parse_float=str, parse_int=str)
    if not isinstance(rows, list):
        return None
    matches = [
        row
        for row in rows
        if isinstance(row, dict)
        and _address(row.get("userAddress")) == user
        and _address(row.get("builderAddress")) == builder
    ]
    if len(matches) != 1:
        return None
    raw = matches[0].get("maxFeeRate")
    if (
        not isinstance(raw, str)
        or len(raw) > 64
        or not re.fullmatch(r"[0-9]+(?:\.[0-9]+)?(?:[eE][+-]?[0-9]{1,3})?", raw)
    ):
        return None
    try:
        cap = Decimal(raw)
    except InvalidOperation:
        return None
    scale = cap.as_tuple().exponent
    return cap if isinstance(scale, int) and -18 <= scale <= 0 and 0 < cap <= _VENUE_CAP[product] else None


def sign_message_request(sign: MessageSigner, params: MessageParams, network: Network) -> SignedMessage:
    """Sign native order/read parameters in their original order, using the caller's EIP-712 wallet."""
    if network not in ("mainnet", "testnet"):
        raise ValueError("Invalid Aster Message network")
    chain_id = 1666 if network == "mainnet" else 714
    seen: set[str] = set()
    wire: list[tuple[str, str]] = []
    for key, value in params:
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*", key) or key == "signature" or key in seen:
            raise ValueError("Invalid or duplicate Aster parameter")
        if not isinstance(value, (str, int)) or (isinstance(value, int) and not 0 <= value < 1 << 256):
            raise ValueError("Invalid Aster parameter value")
        if key == "asterChain" and value != ("Mainnet" if network == "mainnet" else "Testnet"):
            raise ValueError("Aster Message network mismatch")
        seen.add(key)
        wire.append((key, str(value)))
    msg = urlencode(wire)
    typed_data = {
        "types": {
            "EIP712Domain": [
                {"name": "name", "type": "string"},
                {"name": "version", "type": "string"},
                {"name": "chainId", "type": "uint256"},
                {"name": "verifyingContract", "type": "address"},
            ],
            "Message": [{"name": "msg", "type": "string"}],
        },
        "primaryType": "Message",
        "domain": {"name": "AsterSignTransaction", "version": "1", "chainId": chain_id, "verifyingContract": _ZERO},
        "message": {"msg": msg},
    }
    signature = sign(typed_data)
    if not re.fullmatch(r"0x[0-9a-fA-F]{130}", signature):
        raise ValueError("Invalid Aster Message signature")
    return SignedMessage(msg, signature, chain_id, f"{msg}&signature={signature}")


def _encoded(row: PublishedRate) -> bool:
    try:
        return parse_dec(row.encoded) == row.rate
    except ValueError:
        return False


def _no_fee(reduce_only: bool, code: str) -> FeeField:
    if reduce_only:
        return {"kind": "none", "reason": "close_without_approval"}
    raise ApiError("Aster builder attribution is unavailable", code=code)


class AsterDirect:
    venue = "aster"

    def __init__(
        self,
        product: Product,
        read_builders: Callable[[BuilderRequest], str],
        *,
        now: Callable[[], int] = unix_ms,
    ) -> None:
        self._product = product
        self._read_builders = read_builders
        self._now = now
        self._fees = DirectFees(
            self.venue,
            True,
            self._encode,
            cap_for=lambda _cls: _CLASS_CAP,
            normalize_account=lambda address: _address(address) or "",
            now=now,
        )
        self._context: DirectContext | None = None
        self._snapshot: DirectStart | None = None
        self._lock = RLock()

    @staticmethod
    def _encode(row: PublishedRate, builder_ref: str | None, _type: OrderType, _tif: TimeInForce) -> FeeField:
        builder = _address(builder_ref)
        if builder is None or row.rate <= 0 or row.rate % _STEP != 0 or not _encoded(row):
            raise ApiError("Invalid Aster builder fee publication", code="fee_code_missing")
        return {"kind": "order_field", "builder": builder, "rate": row.rate, "encodedRate": format_dec(row.rate)}

    def _joined(self, ctx: DirectContext, snapshot: DirectStart) -> DirectStart:
        builder = _address(snapshot.builder_ref)
        cap: Decimal | None = None
        if builder is not None and snapshot.approved_cap is not None and snapshot.approved_cap > 0:
            path: BuilderPath = (
                "/api/v3/builder" if self._product == "spot" else "/fapi/v3/builder"
            )
            try:
                body = self._read_builders(BuilderRequest("GET", path, ctx.account_ref))
                live = _approval(body, ctx.account_ref, builder, self._product)
                if live is not None:
                    cap = min(live, snapshot.approved_cap)
            except Exception:
                # An unavailable or malformed native read cannot prove approval.
                cap = None
        self._snapshot = replace(snapshot, approved_cap=cap)
        return self._snapshot

    def start(self, context: DirectContext) -> DirectStart:
        with self._lock:
            self._context = None
            self._snapshot = None
            account = _address(context.account_ref)
            if account is None:
                raise ApiError("Invalid Aster wallet", code="account_not_bound")
            ctx = DirectContext(context.client, account)
            self._context = ctx
            return self._joined(ctx, self._fees.start(ctx))

    def refresh(self) -> DirectStart:
        with self._lock:
            if self._snapshot is not None and self._snapshot.valid_until > self._now():
                return self._snapshot
            if self._context is None:
                raise ApiError("Start the Aster direct module before signing", code="fee_code_missing")
            return self._joined(self._context, self._fees.refresh())

    def fee_fields(self, cls: str, order_type: OrderType, tif: TimeInForce, reduce_only: bool) -> FeeField:
        with self._lock:
            correct = cls == "spot" if self._product == "spot" else cls in ("perp", "rwa_perp")
            if not correct:
                raise ApiError("Aster class is outside the configured product", code="invalid_request")
            snapshot = self._snapshot
            if snapshot is None or snapshot.valid_until <= self._now():
                return _no_fee(reduce_only, "fee_code_missing")
            if snapshot.approved_cap is None:
                return _no_fee(reduce_only, "fee_approval_missing")
            field = self._fees.fee_fields(cls, order_type, tif, reduce_only)
            if field["kind"] != "order_field":
                return field
            maker = next((item for item in snapshot.rates if item.cls == cls and item.liquidity == "maker"), None)
            taker = next((item for item in snapshot.rates if item.cls == cls and item.liquidity == "taker"), None)
            if maker is None or taker is None or maker.rate != taker.rate or not _encoded(maker) or not _encoded(taker):
                return _no_fee(reduce_only, "fee_code_missing")
            if field["rate"] > snapshot.approved_cap:
                return _no_fee(reduce_only, "fee_above_cap")
            return field

    def order_fields(self, cls: str, order_type: OrderType, tif: TimeInForce, reduce_only: bool) -> dict[str, str]:
        field = self.fee_fields(cls, order_type, tif, reduce_only)
        if field["kind"] != "order_field":
            return {}
        return {"builder": field["builder"], "feeRate": field["encodedRate"]}
