"""The same request and retry policy for synchronous and asynchronous HTTP clients."""

import json
import re
import secrets
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from decimal import Decimal
from email.utils import parsedate_to_datetime
from importlib.metadata import PackageNotFoundError, version
from random import uniform
from typing import Generic, Literal, TypeAlias, TypeVar
from urllib.parse import quote, urlencode, urlsplit
from uuid import UUID, uuid4

import httpx
from pydantic import BaseModel, TypeAdapter, ValidationError
from typing_extensions import TypeAliasType

from . import models
from ._distribution import DISTRIBUTION, USER_AGENT_PREFIX
from ._operations import OPERATIONS, Operation
from .dec import format_dec
from .error_codes import ErrorCode
from .signing import RequestToSign, base64url, sign_request

T = TypeVar("T")


class Ok(BaseModel, Generic[T]):
    ok: Literal[True] = True
    data: T
    next_cursor: str | None = None
    request_id: str | None = None


class Refusal(BaseModel):
    ok: Literal[False] = False
    code: ErrorCode | Literal["unknown"]
    message: str
    retryable: bool
    details: dict[str, object] | None = None
    request_id: str | None = None


Answer = TypeAliasType("Answer", Ok[T] | Refusal, type_params=(T,))
JsonValue: TypeAlias = str | int | bool | None | list["JsonValue"] | dict[str, "JsonValue"]
QueryValue: TypeAlias = str | int | bool | Decimal | None
Query: TypeAlias = Mapping[str, QueryValue | Sequence[QueryValue]]
Body: TypeAlias = BaseModel | Mapping[str, object] | None
_KEY_TEXT = re.compile(r"(?:live|test)_[0-9A-HJKMNP-TV-Z]{26}")
_SLOT = re.compile(r"\{([a-zA-Z][a-zA-Z0-9]*)\}")
_ULID = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
_RETRYABLE_HTTP = frozenset({502, 503, 504})
try:
    _CLIENT_VERSION = version(DISTRIBUTION)
except PackageNotFoundError:
    _CLIENT_VERSION = "0.0.0"
_CLIENT_HEADER = f"{USER_AGENT_PREFIX}/{_CLIENT_VERSION}"
_RESPONSE_MODELS: dict[str, Callable[[object], object]] = {
    "listVenues": TypeAdapter(list[models.VenueInfo]).validate_python,
    "getVenue": TypeAdapter(models.VenueInfo).validate_python,
    "getVenueBudget": TypeAdapter(models.VenueBudget).validate_python,
    "listInstruments": TypeAdapter(list[models.InstrumentInfo]).validate_python,
    "getInstrument": TypeAdapter(models.InstrumentInfo).validate_python,
    "getFees": TypeAdapter(list[models.FeeSchedule]).validate_python,
    "quoteFee": TypeAdapter(models.FeeQuote).validate_python,
    "listAccounts": TypeAdapter(list[models.VenueAccountInfo]).validate_python,
    "createAccountLinkChallenge": TypeAdapter(models.AccountLinkChallenge).validate_python,
    "linkAccount": TypeAdapter(models.VenueAccountInfo).validate_python,
    "placeOrder": TypeAdapter(models.Order | models.Route | models.DryRunResult).validate_python,
    "quoteRoute": TypeAdapter(models.RouteQuote).validate_python,
    "getRoute": TypeAdapter(models.Route).validate_python,
    "listRoutes": TypeAdapter(list[models.Route]).validate_python,
    "cancelRoute": TypeAdapter(models.Route).validate_python,
    "listOrders": TypeAdapter(list[models.Order]).validate_python,
    "getOrder": TypeAdapter(models.Order).validate_python,
    "cancelOrder": TypeAdapter(models.Order).validate_python,
    "modifyOrder": TypeAdapter(models.Order).validate_python,
    "cancelAll": TypeAdapter(models.CancelAllResult).validate_python,
    "setDeadMan": TypeAdapter(models.DeadMan).validate_python,
    "listPositions": TypeAdapter(list[models.AccountPositions]).validate_python,
    "listBalances": TypeAdapter(list[models.AccountBalances]).validate_python,
    "listFills": TypeAdapter(list[models.Fill]).validate_python,
    "getEquity": TypeAdapter(models.EquityView).validate_python,
    "whoami": TypeAdapter(models.Whoami).validate_python,
}


class ApiError(Exception):
    """The request did not produce a usable API answer."""

    def __init__(
        self,
        message: str,
        *,
        status: int | None = None,
        code: str | None = None,
        request_id: str | None = None,
        client_order_id: str | None = None,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.request_id = request_id
        self.client_order_id = client_order_id


@dataclass(frozen=True, slots=True)
class RequestKey:
    key_text: str
    seed: bytes = field(repr=False)

    def __post_init__(self) -> None:
        if _KEY_TEXT.fullmatch(self.key_text) is None or not isinstance(self.seed, bytes) or len(self.seed) != 32:
            raise ValueError("An API key needs its key text and 32-byte Ed25519 seed")


def api_origin(value: str) -> str:
    parts = urlsplit(value)
    if (
        parts.scheme not in {"https", "http"}
        or parts.hostname is None
        or parts.username is not None
        or parts.password is not None
        or parts.path not in {"", "/"}
        or parts.query
        or parts.fragment
    ):
        raise ValueError("Expected an HTTP API origin")
    return f"{parts.scheme}://{parts.netloc}"


def unix_ms() -> int:
    return time.time_ns() // 1_000_000


def encode_json(value: object) -> JsonValue:
    """Convert a request to the wire shape without accepting any float."""
    if isinstance(value, BaseModel):
        return encode_json(value.model_dump(mode="python", by_alias=True, exclude_unset=True))
    if isinstance(value, float):
        raise TypeError("API request values must not be floats")
    if isinstance(value, Decimal):
        return format_dec(value)
    if value is None or isinstance(value, str | bool | int):
        return value
    if isinstance(value, datetime):
        if value.utcoffset() is None:
            raise TypeError("API datetimes need a timezone")
        return value.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    if isinstance(value, date | UUID):
        return str(value)
    if isinstance(value, Mapping):
        mapped: dict[str, JsonValue] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise TypeError("API object keys must be strings")
            mapped[key] = encode_json(item)
        return mapped
    if isinstance(value, Sequence) and not isinstance(value, bytes | bytearray | memoryview):
        return [encode_json(item) for item in value]
    raise TypeError(f"Unsupported API request type {type(value).__name__}")


def json_bytes(value: JsonValue) -> bytes:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")


def _query(query: Query | None) -> str:
    items: list[tuple[str, str]] = []
    for name, value in (query or {}).items():
        values = value if isinstance(value, Sequence) and not isinstance(value, str) else (value,)
        for item in values:
            if item is None:
                continue
            if isinstance(item, float):
                raise TypeError("API query values must not be floats")
            if isinstance(item, Decimal):
                text = format_dec(item)
            elif isinstance(item, bool):
                text = "true" if item else "false"
            elif isinstance(item, str | int):
                text = str(item)
            else:
                raise TypeError(f"Unsupported API query type {type(item).__name__}")
            items.append((name, text))
    return urlencode(items)


def _order_id(now_ms: int) -> str:
    value = ((now_ms & ((1 << 48) - 1)) << 80) | secrets.randbits(80)
    digits = "".join(_ULID[(value >> shift) & 31] for shift in range(120, -1, -5))
    return f"c{digits}"


@dataclass(frozen=True, slots=True)
class Prepared:
    operation_id: str
    operation: Operation
    target: str
    body: bytes
    idempotency_key: str | None
    client_order_id: str | None

    @property
    def can_retry(self) -> bool:
        return self.operation.method == "GET" or self.operation.idempotency in {"header", "target", "client_order_id"}


@dataclass(frozen=True, slots=True)
class Retry:
    after: float
    resync: bool = False


class HttpPolicy:
    def __init__(
        self,
        base_url: str,
        key: RequestKey | None,
        now: Callable[[], int],
        attempts: int,
        base_delay: float,
    ) -> None:
        if attempts < 1 or base_delay < 0:
            raise ValueError("Retry attempts must be positive and delay nonnegative")
        self.origin = api_origin(base_url)
        self.key = key
        self.now = now
        self.attempts = attempts
        self.base_delay = base_delay
        self.clock_skew_ms = 0

    def prepare(
        self,
        operation_id: str,
        path: Mapping[str, str] | None,
        query: Query | None,
        body: Body,
        idempotency_key: str | None,
    ) -> Prepared:
        operation = OPERATIONS[operation_id]
        values = path or {}
        if set(values) != set(_SLOT.findall(operation.path)):
            raise ValueError(f"Wrong path parameters for {operation_id}")
        target = _SLOT.sub(lambda match: quote(values[match[1]], safe=""), operation.path)
        encoded = _query(query)
        if encoded:
            target = f"{target}?{encoded}"
        payload = encode_json(body)
        client_id = values.get("clientOrderId")
        if operation_id == "placeOrder":
            if not isinstance(payload, dict):
                raise TypeError("Placing an order requires an object body")
            field_name = "clientRouteId" if payload.get("route") == "best" else "clientOrderId"
            if field_name not in payload:
                payload[field_name] = _order_id(self.now())
            candidate = payload[field_name]
            if not isinstance(candidate, str) or not candidate:
                raise TypeError(f"{field_name} must be a nonempty string")
            client_id = candidate
        if operation.idempotency == "header" and idempotency_key is None:
            idempotency_key = str(uuid4())
        return Prepared(
            operation_id, operation, target, b"" if body is None else json_bytes(payload), idempotency_key, client_id
        )

    def signed_headers(self, method: str, target: str, body: bytes) -> dict[str, str]:
        if self.key is None:
            return {"X-Api-Client": _CLIENT_HEADER}
        timestamp = str(self.now() + self.clock_skew_ms)
        nonce = base64url(secrets.token_bytes(16))
        signed = RequestToSign(method, target, body, timestamp, nonce, self.key.key_text)
        return {
            "X-Api-Client": _CLIENT_HEADER,
            "X-Api-Key": self.key.key_text,
            "X-Api-Timestamp": timestamp,
            "X-Api-Nonce": nonce,
            "X-Api-Signature": sign_request(signed, self.key.seed),
        }

    def request(self, prepared: Prepared) -> httpx.Request:
        request = httpx.Request(prepared.operation.method, f"{self.origin}{prepared.target}", content=prepared.body)
        request.headers.update(self.signed_headers(request.method, request.url.raw_path.decode("ascii"), prepared.body))
        if prepared.body:
            request.headers["Content-Type"] = "application/json"
        if prepared.idempotency_key is not None:
            request.headers["Idempotency-Key"] = prepared.idempotency_key
        return request

    def _server_time(self, value: str | None) -> int | None:
        if value is None:
            return None
        try:
            parsed = parsedate_to_datetime(value)
            return int(parsed.timestamp() * 1000) if parsed.utcoffset() is not None else None
        except (TypeError, ValueError, OverflowError):
            return None

    def network_retry(self, prepared: Prepared, attempt: int) -> Retry | None:
        return (
            Retry(self.base_delay * 2**attempt * uniform(0.5, 1.5))
            if prepared.can_retry and attempt + 1 < self.attempts
            else None
        )

    def answer(
        self,
        response: httpx.Response,
        prepared: Prepared,
        attempt: int,
        resynced: bool,
        sent_at: int,
    ) -> Answer[object] | Retry:
        try:
            parsed: object = response.json()
        except ValueError:
            parsed = None
        row = parsed if isinstance(parsed, dict) else {}
        code = row.get("code")
        request_id = response.headers.get("X-Request-Id")
        if request_id is None and isinstance(row.get("requestId"), str):
            request_id = row["requestId"]
        status = response.status_code
        if code == "timestamp_out_of_window" and not resynced and self.key is not None:
            server_time = self._server_time(response.headers.get("Date"))
            if server_time is not None:
                self.clock_skew_ms = server_time - (sent_at + self.now()) // 2
                return Retry(0, resync=True)
        if attempt + 1 < self.attempts:
            if status == 429:
                header = response.headers.get("Retry-After", "")
                if header.isascii() and header.isdecimal():
                    return Retry(float(header))
                date_ms = self._server_time(header)
                if date_ms is not None:
                    return Retry(max(0, date_ms - self.now()) / 1000)
                return Retry(self.base_delay * 2**attempt)
            if code == "idempotency_in_progress" and status != 200 and prepared.operation.idempotency == "header":
                details = row.get("details")
                delay = details.get("retryAfterMs") if isinstance(details, dict) else None
                return Retry(delay / 1000 if type(delay) is int and delay >= 0 else self.base_delay)
            if status in _RETRYABLE_HTTP:
                retry = self.network_retry(prepared, attempt)
                if retry is not None:
                    return retry
        if status in {401, 403, 429} or status >= 500 or 300 <= status < 400:
            raise ApiError(
                f"API request failed with HTTP {status}",
                status=status,
                code=code if isinstance(code, str) else None,
                request_id=request_id,
                client_order_id=prepared.client_order_id,
            )
        if 200 <= status < 300 and row.get("ok") is True and "data" in row:
            cursor = row.get("nextCursor")
            if cursor is None or isinstance(cursor, str):
                data: object = row["data"]
                decoder = _RESPONSE_MODELS.get(prepared.operation_id)
                try:
                    if decoder is not None:
                        data = decoder(data)
                except (ValidationError, TypeError, ValueError):
                    raise ApiError(
                        "Malformed API data",
                        status=status,
                        request_id=request_id,
                        client_order_id=prepared.client_order_id,
                    ) from None
                return Ok[object](data=data, next_cursor=cursor, request_id=request_id)
        if row.get("ok") is False and isinstance(code, str) and isinstance(row.get("message"), str):
            known: ErrorCode | Literal["unknown"]
            try:
                known = ErrorCode(code)
            except ValueError:
                known = "unknown"
            details = row.get("details")
            return Refusal(
                code=known,
                message=row["message"],
                retryable=row.get("retryable") is True,
                details=details if isinstance(details, dict) else None,
                request_id=request_id,
            )
        raise ApiError(
            "Malformed API response", status=status, request_id=request_id, client_order_id=prepared.client_order_id
        )
