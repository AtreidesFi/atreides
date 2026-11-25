"""Direct Binance attribution (06 Binance 2.6.1, 2.7.2, 2.7.5, F3; BNC-010, BNC-011, BNC-013, BNC-014).

The caller keeps its own Binance client and signing; this module only writes our ``x-`` code into client ids.
"""

import hashlib
import json
import re
import secrets
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Literal

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from .._http import ApiError, unix_ms
from .template import DirectContext, DirectFees, DirectStart, FeeField, OrderType, TimeInForce

Product = Literal["spot", "usdm"]

_PREFIX = re.compile(r"x-[A-Za-z0-9_-]{1,18}")
_CODE = re.compile(r"[A-Za-z0-9_-]{1,18}")
_ID_CHARS = re.compile(r"[A-Za-z0-9_-]+")
_CUSTOMER_ID = re.compile(r"[A-Za-z0-9]{8,32}")
_BASE62 = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
_MAX_ID = 36
# Spot amend (PUT): without newClientOrderId Binance gives the order a random id, which drops our prefix.
_AMEND = "/api/v3/order/amend/keepPriority"

# Client id fields a POST must carry, per product and path.
_REQUIRED: dict[str, dict[str, tuple[str, ...]]] = {
    "spot": {
        "/api/v3/order": ("newClientOrderId",),
        "/api/v3/order/test": ("newClientOrderId",),
        "/api/v3/order/cancelReplace": ("newClientOrderId",),
        "/api/v3/sor/order": ("newClientOrderId",),
        "/api/v3/sor/order/test": ("newClientOrderId",),
        "/api/v3/order/oco": ("listClientOrderId", "limitClientOrderId", "stopClientOrderId"),
        "/api/v3/orderList/oco": ("listClientOrderId", "aboveClientOrderId", "belowClientOrderId"),
        "/api/v3/orderList/oto": ("listClientOrderId", "workingClientOrderId", "pendingClientOrderId"),
        "/api/v3/orderList/otoco": (
            "listClientOrderId",
            "workingClientOrderId",
            "pendingAboveClientOrderId",
            "pendingBelowClientOrderId",
        ),
        "/api/v3/orderList/opo": ("listClientOrderId", "workingClientOrderId", "pendingClientOrderId"),
        "/api/v3/orderList/opoco": (
            "listClientOrderId",
            "workingClientOrderId",
            "pendingAboveClientOrderId",
            "pendingBelowClientOrderId",
        ),
    },
    "usdm": {
        "/fapi/v1/order": ("newClientOrderId",),
        "/fapi/v1/algoOrder": ("clientAlgoId",),
        "/fapi/v1/batchOrders": (),
    },
}


@dataclass(frozen=True, slots=True)
class BinanceKeypair:
    public_key_pem: str  # SPKI PEM: the text the user pastes into API Management, Self-generated
    private_key_pem: str  # PKCS#8 PEM: stays with the bot and is never sent anywhere


def keygen() -> BinanceKeypair:
    """A fresh Ed25519 key pair, generated locally at call time (binance.md 2a step 1)."""
    key = Ed25519PrivateKey.generate()
    return BinanceKeypair(
        public_key_pem=key.public_key()
        .public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
        .decode("ascii"),
        private_key_pem=key.private_bytes(
            serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
        ).decode("ascii"),
    )


def _base62(data: bytes) -> str:
    n = int.from_bytes(data, "big")
    out = ""
    while True:
        n, digit = divmod(n, 62)
        out = _BASE62[digit] + out
        if n == 0:
            return out


def _random_suffix() -> str:
    return _base62(secrets.token_bytes(16)).rjust(22, "0")


def ccxt_broker_options(
    spot: str | None, usdm: str | None, known: Mapping[str, object] | None = None
) -> dict[str, dict[str, str]]:
    """CCXT's broker option; pass ``exchange.options['broker']`` as ``known`` to check the option names exist."""
    broker: dict[str, str] = {}
    for key, code in (("spot", spot), ("margin", spot), ("future", usdm), ("swap", usdm)):
        if code is None:
            continue
        if _CODE.fullmatch(code) is None:
            raise ApiError("Binance client code is malformed", code="fee_code_missing")
        if known is not None and key not in known:
            raise ApiError(f"CCXT has no broker option {key}", code="invalid_request")
        broker[key] = f"x-{code}"
    return {"broker": broker}


class BinanceDirect(DirectFees):
    def __init__(self, product: Product, *, now: Callable[[], int] = unix_ms) -> None:
        super().__init__("binance", False, _no_encoder, now=now)
        self.product = product
        self._prefix: str | None = None
        self._ids: dict[str, str] = {}

    def _read(self, context: DirectContext) -> DirectStart:
        start = super()._read(context)
        prefix = start.builder_ref or None
        if prefix is not None and _PREFIX.fullmatch(prefix) is None:
            raise ApiError("Binance prefix from the fee schedule is malformed", code="fee_code_missing")
        self._prefix = prefix
        return start

    def client_prefix(self) -> str | None:
        """``x-<code>``, or None while no client code exists: then no order carries an ``x-`` prefix (BNC-013)."""
        return self._prefix

    def fee_fields(self, cls: str, order_type: OrderType, tif: TimeInForce, reduce_only: bool) -> FeeField:
        if self._prefix is None:
            return {"kind": "none", "reason": "venue_has_no_fee_path"}
        return {"kind": "prefixed_client_id", "prefix": self._prefix}

    def client_order_id(self, user_id: str | None = None) -> str:
        """``x-<code>`` plus the bot's id if it fits, else the last characters of base62(SHA-256(id)).

        Without an id, a random 22-character base62 suffix. The map back to the bot's id stays in memory.
        """
        prefix = self._prefix
        if prefix is None:
            return user_id if user_id is not None else _random_suffix()
        room = _MAX_ID - len(prefix)
        if user_id is None:
            return prefix + _random_suffix()[-min(22, room) :]
        if user_id.startswith(prefix) and _ID_CHARS.fullmatch(user_id) and len(user_id) <= _MAX_ID:
            return user_id
        if _ID_CHARS.fullmatch(user_id) and len(user_id) <= room:
            return self._remember(prefix + user_id, user_id)
        digest = _base62(hashlib.sha256(user_id.encode()).digest()).rjust(room, "0")
        return self._remember(prefix + digest[-room:], user_id)

    def bot_id(self, venue_client_order_id: str) -> str | None:
        return self._ids.get(venue_client_order_id)

    def _remember(self, venue_id: str, user_id: str) -> str:
        known = self._ids.get(venue_id)
        if known is not None and known != user_id:
            raise ApiError("Binance client id collision", code="invalid_request")
        self._ids[venue_id] = user_id
        return venue_id

    def tag_params(self, method: str, path: str, params: Mapping[str, str]) -> dict[str, str]:
        """Params of one request with every client id field carrying our prefix (BNC-011). Returns a copy.

        The spot amend (PUT) keeps the original id when it already carries our prefix. Cancels, USD-M modifies and
        reads pass through unchanged: they name the venue id.
        """
        out = dict(params)
        prefix = self._prefix
        if prefix is None:
            return out
        verb = method.upper()
        if self.product == "spot" and verb == "PUT" and path == _AMEND:
            orig = params.get("origClientOrderId")
            own = params.get("newClientOrderId")
            if own is None and orig is not None and orig.startswith(prefix):
                own = orig
            out["newClientOrderId"] = self.client_order_id(own)
            return out
        required = _REQUIRED[self.product].get(path)
        if verb != "POST" or required is None:
            return out
        for field in required:
            out[field] = self.client_order_id(params.get(field))
        if path == "/fapi/v1/batchOrders":
            raw = params.get("batchOrders")
            items: Any = json.loads(raw) if raw is not None else None
            if not isinstance(items, list):
                raise ApiError("batchOrders must be a JSON array", code="invalid_request")
            out["batchOrders"] = json.dumps(
                [{**item, "newClientOrderId": self.client_order_id(_own_id(item))} for item in items],
                separators=(",", ":"),
            )
        return out

    def customer_id_request(
        self, op: Literal["set", "get"], customer_id: str | None = None
    ) -> tuple[str, str, dict[str, str]]:
        """The unsigned Customer ID request (BNC-014) as (method, path, params); the caller signs it."""
        prefix = self._prefix
        if prefix is None:
            raise ApiError("No Binance client code", code="fee_code_missing")
        code = prefix[2:]
        if self.product == "spot":
            path, field = "/sapi/v1/apiReferral/userCustomization", "apiAgentCode"
        else:
            path, field = "/fapi/v1/apiReferral/userCustomization", "brokerId"
        if op == "get":
            return "GET", path, {field: code}
        if customer_id is None or _CUSTOMER_ID.fullmatch(customer_id) is None:
            raise ApiError("Customer ID must be 8 to 32 letters or digits", code="invalid_request")
        return "POST", path, {"customerId": customer_id, field: code}


def _own_id(item: Mapping[str, Any]) -> str | None:
    own = item.get("newClientOrderId")
    return own if isinstance(own, str) else None


def _no_encoder(*_: object) -> FeeField:  # Binance carries no per-order rate of ours (06 2.6.3)
    raise ApiError("Binance carries no per-order rate", code="fee_code_missing")
