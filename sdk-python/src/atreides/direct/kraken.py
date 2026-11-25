"""Direct Kraken tagging (06-venue-cex.md section 8; CEX-504).

Kraken has no official Python client. ``prepare`` adds our ``broker`` and the client id prefix to the parameters of an
order request; ``sign_spot`` and ``sign_futures`` then sign exactly those bytes (``broker`` is inside the signed body).
The nonce is the caller's: it must strictly increase per API key, or Kraken answers ``EAPI:Invalid nonce``.
"""

import base64
import hashlib
import hmac
import json
import re
import warnings
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal
from urllib.parse import quote

Product = Literal["spot", "futures"]
Params = Sequence[tuple[str, str]]

_SPOT_BYTES = 18
_FUTURES_BYTES = 100
# IIBAN: ``XXXX YYYY ZZZZ WWWW`` or ``XXXXYYYYZZZZWWWW`` (futures send-order page).
_BROKER = re.compile(r"[0-9A-Za-z]{4}( ?)[0-9A-Za-z]{4}\1[0-9A-Za-z]{4}\1[0-9A-Za-z]{4}")
_ASCII = re.compile(r"[\x20-\x7e]*")
_UUID = re.compile(r"[0-9a-f]{8}(-?)[0-9a-f]{4}\1[0-9a-f]{4}\1[0-9a-f]{4}\1[0-9a-f]{12}", re.IGNORECASE)
_SPOT_ORDER = frozenset({"/0/private/AddOrder", "/0/private/AddOrderBatch"})
_FUTURES_ORDER = frozenset({"/derivatives/api/v3/sendorder", "/derivatives/api/v3/batchorder"})
_U64_MAX = 2**64 - 1
_HEADER_TOKEN = re.compile(r"[!-~]+")


def kraken_client_id(product: Product, prefix: str, client_id: str) -> str:
    """Prefix + the bot's id cut to the venue's length. UUID-shaped spot ids are left alone."""
    if not _ASCII.fullmatch(client_id) or not _ASCII.fullmatch(prefix):
        raise ValueError("invalid Kraken client id")
    if product == "spot" and _UUID.fullmatch(client_id):
        return client_id
    joined = client_id if client_id.startswith(prefix) else prefix + client_id
    return joined[: _SPOT_BYTES if product == "spot" else _FUTURES_BYTES]


def _encode(text: str) -> str:
    return quote(text, safe="")


def _encode_params(params: Params) -> str:
    return "&".join(f"{_encode(name)}={_encode(value)}" for name, value in params)


@dataclass
class KrakenDirect:
    broker: str
    """Our partner IIBAN from config, sent only when ``program_live`` is true (CEX-504)."""
    program_live: bool
    prefix_client_ids: bool = True
    client_prefix: str = "a1"
    warn: Callable[[str], None] = field(default=lambda message: warnings.warn(message, stacklevel=3))
    venue: Literal["kraken"] = field(default="kraken", init=False)
    _warned: bool = field(default=False, init=False, repr=False)

    def __post_init__(self) -> None:
        if self.program_live and not _BROKER.fullmatch(self.broker):
            raise ValueError("invalid Kraken broker")
        if not _ASCII.fullmatch(self.client_prefix) or len(self.client_prefix) > _SPOT_BYTES - 1:
            raise ValueError("invalid Kraken client prefix")

    def tag(self) -> dict[str, Any]:
        value = self.broker if self.program_live else ""
        return {"venue": "kraken", "kind": "body_field", "name": "broker", "value": value, "signed": True}

    def prepare(self, product: Product, path: str, params: Params) -> Sequence[tuple[str, str]]:
        """Other paths are returned unchanged.

        While the program is live our ``broker`` replaces one the bot set (with a one-time notice); while it is not, the
        bot's own ``broker`` is left as it is. AddOrderBatch goes to Kraken as a JSON body: pass ``orders`` as its JSON
        text and put the parsed array back into that body (``sign_spot`` signs form bodies only).
        """
        if path not in (_SPOT_ORDER if product == "spot" else _FUTURES_ORDER):
            return params
        live = self.program_live
        if live and not self._warned and any(n == "broker" and v != self.broker for n, v in params):
            self._warned = True
            self.warn("Kraken: the bot set its own broker; the SDK sends its own partner IIBAN instead.")
        out: list[tuple[str, str]] = []
        for name, value in params:
            if live and name == "broker":
                continue
            if self.prefix_client_ids:
                value = self._prefixed(product, name, value)
            out.append((name, value))
        if live:
            out.append(("broker", self.broker))
        return out

    def _prefixed(self, product: Product, name: str, value: str) -> str:
        prefix = self.client_prefix
        if product == "spot" and name == "cl_ord_id":
            return kraken_client_id("spot", prefix, value)
        if product == "futures" and name == "cliOrdId":
            return kraken_client_id("futures", prefix, value)
        if product == "spot" and name == "orders":
            orders = json.loads(value)
            if not isinstance(orders, list):
                return value
            fixed = [
                {**o, "cl_ord_id": kraken_client_id("spot", prefix, o["cl_ord_id"])}
                if isinstance(o, dict) and isinstance(o.get("cl_ord_id"), str)
                else o
                for o in orders
            ]
            return value if fixed == orders else json.dumps(fixed, separators=(",", ":"), ensure_ascii=False)
        if product == "futures" and name == "json":
            batch = json.loads(value)
            entries = batch.get("batchOrder") if isinstance(batch, dict) else None
            if not isinstance(entries, list):
                return value
            fixed = [
                {**e, "cliOrdId": kraken_client_id("futures", prefix, e["cliOrdId"])}
                if isinstance(e, dict) and e.get("order") == "send" and isinstance(e.get("cliOrdId"), str)
                else e
                for e in entries
            ]
            if fixed == entries:
                return value
            return json.dumps({**batch, "batchOrder": fixed}, separators=(",", ":"), ensure_ascii=False)
        return value


def _checked(secret_b64: str, api_key: str, nonce: int) -> bytes:
    if isinstance(nonce, bool) or not isinstance(nonce, int) or not 0 <= nonce <= _U64_MAX:
        raise ValueError("invalid Kraken nonce")
    if not _HEADER_TOKEN.fullmatch(api_key):
        raise ValueError("invalid Kraken authentication header")
    secret = base64.b64decode(secret_b64, validate=True)
    if not secret:
        raise ValueError("missing Kraken HMAC material")
    return secret


def sign_spot(secret_b64: str, api_key: str, path: str, nonce: int, params: Params) -> tuple[str, dict[str, str]]:
    """Returns the form body (nonce first) and the headers. The body is sent exactly as signed."""
    secret = _checked(secret_b64, api_key, nonce)
    if not path.startswith("/0/private/") or path == "/0/private/" or path == "/0/private/AddOrderBatch":
        raise ValueError("invalid Kraken spot form path")  # AddOrderBatch takes a JSON body only
    if any(name == "nonce" for name, _ in params):
        raise ValueError("Kraken nonce is supplied separately")
    body = _encode_params([("nonce", str(nonce)), *params]).replace("%20", "+")
    digest = hashlib.sha256((str(nonce) + body).encode()).digest()
    mac = hmac.new(secret, path.encode() + digest, hashlib.sha512)
    headers = {
        "Content-Type": "application/x-www-form-urlencoded",
        "API-Key": api_key,
        "API-Sign": base64.b64encode(mac.digest()).decode(),
    }
    return body, headers


def sign_futures(secret_b64: str, api_key: str, path: str, nonce: int, params: Params) -> tuple[str, dict[str, str]]:
    """Returns the encoded query string and the headers; the digest excludes the /derivatives transport prefix."""
    secret = _checked(secret_b64, api_key, nonce)
    endpoint = path.removeprefix("/derivatives")
    if not endpoint.startswith("/api/") or endpoint == "/api/":
        raise ValueError("invalid Kraken futures path")
    encoded = _encode_params(params)
    digest = hashlib.sha256((encoded + str(nonce) + endpoint).encode()).digest()
    mac = hmac.new(secret, digest, hashlib.sha512)
    return encoded, {"APIKey": api_key, "Nonce": str(nonce), "Authent": base64.b64encode(mac.digest()).decode()}
