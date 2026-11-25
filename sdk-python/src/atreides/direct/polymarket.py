"""Locally held Polymarket Session Keys and exact-byte order relays.

OfficialSigner loads the pinned upstream SDK on demand. Implement
PolymarketSigner with local IPC if the upstream signer runs in its
own virtual environment.
"""

import importlib
import json
import math
import re
import secrets
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal, Protocol, cast
from urllib.parse import urlencode, urlsplit

from .._endpoints import Placement
from .._http import ApiError, JsonValue, _order_id, unix_ms
from ..client import Client, Reply

_ADDRESS = re.compile(r"0x[0-9a-fA-F]{40}")
_BUILDER = re.compile(r"0x[0-9a-fA-F]{64}")
_UUID = re.compile(r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}")
_ORDER_ID = re.compile(r"[A-Za-z0-9._:-]{1,64}")
_SCALAR_MAX = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEBAAEDCE6AF48A03BBFD25E8CD0364141
_DAY = 86_400_000
_HOUR = 3_600_000
_ZERO = "0x" + "0" * 64
Family = Literal["ctf", "ctf_neg_risk", "v2"]
Tif = Literal["GTC", "GTD", "FAK", "FOK"]
Tick = Literal["0.1", "0.01", "0.005", "0.0025", "0.001", "0.0001"]
_EXCHANGES: dict[Family, tuple[str, str]] = {
    "ctf": ("0xE111180000d2663C0091e4f400237545B87B996B", "2"),
    "ctf_neg_risk": ("0xe2222d279d744050d28e00520010520000310F59", "2"),
    "v2": ("0xe3333700cA9d93003F00f0F71f8515005F6c00Aa", "3"),
}


@dataclass(frozen=True)
class Binding:
    account_id: str
    connection_id: str
    wallet: str
    session_signer: str
    valid_until: str


@dataclass(frozen=True)
class Credentials:
    key: str = field(repr=False)
    secret: str = field(repr=False)
    passphrase: str = field(repr=False)

    def __post_init__(self) -> None:
        if not (
            re.fullmatch(r"[A-Za-z0-9_-]{1,128}", self.key)
            and re.fullmatch(r"[A-Za-z0-9+/_-]{1,254}={0,2}", self.secret)
            and re.fullmatch(r"[!-~]{1,256}", self.passphrase)
        ):
            raise ValueError("Invalid CLOB credentials")


@dataclass(frozen=True)
class Order:
    token_id: str
    family: Family
    price: str
    shares: str
    tick: Tick
    side: Literal["buy", "sell"]
    builder: str
    time_in_force: Tif = "GTC"
    expiration: int = 0
    post_only: bool = False
    reduce_only: bool = False
    client_order_id: str | None = None


@dataclass(frozen=True)
class Renewal:
    days_left: int
    url: str


class PolymarketSigner(Protocol):
    @property
    def address(self) -> str: ...
    def clob_auth(self, timestamp_sec: int) -> str: ...
    def relay(
        self, maker: str, credentials: Credentials, order: Order, timestamp_ms: int
    ) -> Mapping[str, JsonValue]: ...


def _address(value: str) -> str:
    if not _ADDRESS.fullmatch(value) or int(value, 16) == 0:
        raise ValueError("Invalid Polymarket wallet")
    return value.lower()


def _origin(value: str) -> str:
    url = urlsplit(value)
    if (
        not url.hostname
        or url.username is not None
        or url.password is not None
        or url.query
        or url.fragment
        or url.scheme not in {"http", "https"}
        or (url.scheme == "http" and url.hostname not in {"localhost", "127.0.0.1", "::1"})
    ):
        raise ValueError("A trusted HTTPS origin is required")
    return f"{url.scheme}://{url.netloc}"


def _decimal_units(value: str, places: int) -> int:
    if not re.fullmatch(r"(?:0|[1-9]\d*)(?:\.\d+)?", value) or len(value) > 100:
        raise ValueError("Invalid bounded unsigned decimal")
    before, _, after = value.partition(".")
    return int(before + after[:places].ljust(places, "0"))


class OfficialSigner:
    """Sign with the pinned official polymarket-client 0.16.0 routines."""

    def __init__(self, private_key: bytes) -> None:
        from importlib.metadata import PackageNotFoundError, version

        try:
            installed = version("polymarket-client")
        except PackageNotFoundError as error:
            raise ApiError("The pinned Polymarket signer is unavailable", code="feature_unavailable") from error
        if installed != "0.16.0":
            raise ApiError("The Polymarket signer pin is 0.16.0", code="feature_unavailable")
        if len(private_key) != 32 or not 0 < int.from_bytes(private_key, "big") < _SCALAR_MAX:
            raise ValueError("Invalid Polymarket Session Key")
        self._account = importlib.import_module("eth_account").Account.from_key(private_key)
        self.address = _address(self._account.address)

    def _signature(self, typed: object) -> str:
        signed = self._account.sign_typed_data(full_message=typed)
        return "0x" + str(signed.signature.hex()).removeprefix("0x")

    def clob_auth(self, timestamp_sec: int) -> str:
        upstream = importlib.import_module("polymarket._internal.l1_auth")
        typed = upstream.build_api_key_auth_typed_data(
            address=self.address, chain_id=137, timestamp=timestamp_sec, nonce=0
        )
        return self._signature(typed)

    def relay(self, maker: str, credentials: Credentials, order: Order, timestamp_ms: int) -> Mapping[str, JsonValue]:
        types = importlib.import_module("polymarket._internal.actions.orders.types")
        typed = importlib.import_module("polymarket._internal.actions.orders.typed_data")
        wallet = importlib.import_module("polymarket._internal.wallet")
        orders = importlib.import_module("polymarket._internal.actions.orders.orders")
        post = importlib.import_module("polymarket._internal.actions.orders.post")
        hmac = importlib.import_module("polymarket._internal.hmac")
        exchange, version = _EXCHANGES[order.family]
        price = _decimal_units(order.price, 6)
        tick = _decimal_units(order.tick, 6)
        if tick not in (100_000, 10_000, 5_000, 2_500, 1_000, 100):
            raise ValueError("Unsupported Polymarket tick")
        if price < tick or price > 1_000_000 - tick or price % tick:
            raise ValueError("Polymarket price is off tick or out of range")
        if any(digit != "0" for digit in order.price.partition(".")[2][6:]):
            raise ValueError("Polymarket price exceeds base unit precision")
        shares = _decimal_units(order.shares, 2) * 10_000
        if shares == 0:
            raise ValueError("Polymarket share size rounds to zero")
        buy = order.side == "buy"
        cash = (price * shares + (999_999 if buy else 0)) // 1_000_000
        unsigned = types.UnsignedOrder(
            chain_id=137,
            exchange_address=exchange,
            protocol_version=version,
            builder=order.builder,
            expiration=order.expiration,
            maker=maker,
            metadata=_ZERO,
            order_type=order.time_in_force,
            salt=1 + secrets.randbelow((1 << 53) - 1),
            maker_amount=cash if buy else shares,
            taker_amount=shares if buy else cash,
            side="BUY" if buy else "SELL",
            signature_type=3,
            signer=maker,
            timestamp=timestamp_ms,
            token_id=order.token_id,
        )
        inner = self._signature(typed.build_order_typed_data(unsigned))
        wrapped = typed.build_order_signature(unsigned, inner)
        signature = wallet.wrap_deposit_wallet_session_signer_signature(self.address, wrapped)
        signed = orders.create_signed_order(unsigned, signature, post_only=order.post_only)
        path, payload = post.build_post_order_request(signed, owner_api_key=credentials.key)
        if path != "/order":
            raise ValueError("Unexpected Polymarket order path")
        body = json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
        timestamp = timestamp_ms // 1000
        return {
            "body": body,
            "headers": {
                "POLY_ADDRESS": self.address,
                "POLY_API_KEY": credentials.key,
                "POLY_PASSPHRASE": credentials.passphrase,
                "POLY_SIGNATURE": hmac.build_hmac_signature(
                    secret=credentials.secret, timestamp=timestamp, method="POST", path=path, body=body
                ),
                "POLY_TIMESTAMP": str(timestamp),
            },
        }


def new_session_key() -> bytes:
    """Generate a bot-owned key. Keep these bytes in the bot's own secret store."""
    return (1 + secrets.randbelow(_SCALAR_MAX - 1)).to_bytes(32, "big")


class PolymarketDirect:
    def __init__(
        self, client: Client, app_origin: str, signer: PolymarketSigner, *, now: Callable[[], int] = unix_ms
    ) -> None:
        self._client = client
        self._app = _origin(app_origin)
        self._signer = signer
        self._now = now
        self.address = _address(signer.address)

    def _binding(self, binding: Binding) -> tuple[str, int]:
        if (
            not _UUID.fullmatch(binding.account_id)
            or not _UUID.fullmatch(binding.connection_id)
            or _address(binding.session_signer) != self.address
        ):
            raise ApiError("Session Key does not match the connected wallet", code="account_not_bound")
        valid = datetime.fromisoformat(binding.valid_until.replace("Z", "+00:00"))
        if valid.tzinfo is None:
            raise ValueError("Session Key expiry requires a timezone")
        return _address(binding.wallet), int(valid.timestamp() * 1000)

    def connect_link(self, account_id: str | None = None) -> str:
        query = {"mode": "direct", "keyPlace": "bot", "kind": "register_key", "sessionSigner": self.address}
        if account_id is not None:
            if not _UUID.fullmatch(account_id):
                raise ValueError("Invalid Polymarket account ID")
            query["accountId"] = account_id
        return f"{self._app}/connections/polymarket/connect?{urlencode(query)}"

    def renewal(self, binding: Binding) -> Renewal | None:
        _, valid = self._binding(binding)
        remaining = valid - self._now()
        if remaining > 30 * _DAY:
            return None
        return Renewal(
            max(0, math.ceil(remaining / _DAY)), f"{self._app}/connections/{binding.connection_id}?action=renew"
        )

    def create_credentials(
        self,
        binding: Binding,
        send: Callable[[str, str, Mapping[str, str]], tuple[int, Mapping[str, object]]],
    ) -> Credentials:
        """The bot supplies a TLS transport with redirects disabled for these two fixed CLOB endpoints."""
        _, valid = self._binding(binding)
        now = self._now()
        if valid <= now:
            raise ApiError("Renew the expired Session Key", code="agent_expired")
        seconds = now // 1000
        headers = {
            "POLY_ADDRESS": self.address,
            "POLY_SIGNATURE": self._signer.clob_auth(seconds),
            "POLY_TIMESTAMP": str(seconds),
            "POLY_NONCE": "0",
        }
        status, result = send("POST", "https://clob.polymarket.com/auth/api-key", headers)
        if status in (400, 409):
            status, result = send("GET", "https://clob.polymarket.com/auth/derive-api-key", headers)
        if not 200 <= status < 300:
            raise ApiError("Polymarket refused CLOB credentials", status=status)
        fields = tuple(result.get(name) for name in ("apiKey", "secret", "passphrase"))
        if not all(isinstance(value, str) for value in fields):
            raise ApiError("Invalid CLOB credential response")
        return Credentials(*cast(tuple[str, str, str], fields))

    def place(self, binding: Binding, credentials: Credentials, order: Order) -> Placement[Reply]:
        maker, valid = self._binding(binding)
        now = self._now()
        if valid <= now:
            raise ApiError("Renew the expired Session Key", code="agent_expired")
        if valid - now <= _HOUR and not order.reduce_only:
            raise ApiError("Opening orders wait for Session Key renewal", code="key_expiring")
        if (
            order.side not in ("buy", "sell")
            or not re.fullmatch(r"[1-9]\d{0,77}", order.token_id)
            or int(order.token_id) >= 1 << 256
            or not _BUILDER.fullmatch(order.builder)
            or order.family not in _EXCHANGES
            or order.time_in_force not in ("GTC", "GTD", "FAK", "FOK")
            or (order.time_in_force == "GTD") != (order.expiration > 0)
            or (order.expiration != 0 and not now < order.expiration * 1000 <= valid)
            or (order.post_only and order.time_in_force not in ("GTC", "GTD"))
        ):
            raise ValueError("Invalid Polymarket order parameters")
        order_id = order.client_order_id if order.client_order_id is not None else _order_id(now)
        if not _ORDER_ID.fullmatch(order_id):
            raise ValueError("Invalid client order ID")
        signed = self._signer.relay(maker, credentials, order, now)
        if set(signed) != {"body", "headers"} or not isinstance(signed["body"], str):
            raise ValueError("Invalid signed Polymarket relay")
        result = self._client.request(
            "placeOrder",
            body={
                "accountId": binding.account_id,
                "clientOrderId": order_id,
                "relay": {"venue": "polymarket", "signed": dict(signed)},
            },
        )
        return Placement(order_id, result)
