"""HMAC client for a user-run gateway image."""

import hashlib
import hmac
import json
import re
from base64 import b64encode
from collections.abc import Callable, Iterator, Mapping, Sequence
from types import TracebackType
from urllib.parse import quote, urlencode, urlsplit
from uuid import uuid4

import httpx

from ._endpoints import DeadMan, Orders, PlaceOrderInput, order_body
from ._http import Answer, ApiError, Body, HttpPolicy, Ok, Prepared, Query, Retry, json_bytes, unix_ms
from ._keep_alive import KeepAlive, heartbeat_inputs
from .models import Order, VenueId

_Reply = Answer[object]
_MUTATIONS = {
    "placeOrder": "/internal/orders",
    "cancelOrder": "/internal/cancel",
    "modifyOrder": "/internal/modify",
    "cancelAll": "/internal/cancel-all",
    "setDeadMan": "/internal/deadman",
}
_UUID7 = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}")
_CLIENT_ORDER_ID = re.compile(r"[A-Za-z0-9._:-]{1,64}")


def _require_uuid(value: str) -> str:
    if not _UUID7.fullmatch(value):
        raise ValueError("Expected a desk user or venue account UUIDv7")
    return value


def _orders_unavailable(_operation: str, _query: Query | None) -> Iterator[object]:
    raise ApiError("Desk gateway order listing is unavailable", code="feature_unavailable")


class LocalDeadMan(DeadMan[_Reply]):
    def __init__(self, client: "LocalGatewayClient") -> None:
        super().__init__(client._call)
        self._active: set[KeepAlive] = set()

    def keep_alive(self, account_ids: Sequence[str], timeout_ms: int) -> KeepAlive:
        accounts = tuple(_require_uuid(account) for account in heartbeat_inputs(account_ids, timeout_ms))
        handle = KeepAlive(accounts, timeout_ms, self.arm, self._active.discard)
        self._active.add(handle)
        return handle

    def stop_all(self) -> None:
        for handle in tuple(self._active):
            handle.stop()


class LocalGatewayClient:
    def __init__(
        self,
        gateway_url: str,
        hmac_secret: str | bytes,
        *,
        user_id: str,
        venue: str | VenueId,
        timeout: float = 10.0,
        transport: httpx.BaseTransport | None = None,
        now: Callable[[], int] = unix_ms,
    ) -> None:
        self._policy = HttpPolicy(gateway_url, None, now, attempts=1, base_delay=0)
        url = urlsplit(self._policy.origin)
        if url.hostname not in {"127.0.0.1", "::1", "localhost"} and url.scheme != "https":
            raise ValueError("Use HTTPS to reach a remote desk gateway")
        self._user_id = _require_uuid(user_id)
        self._venue = VenueId(venue)
        self._secret = hmac_secret.encode() if isinstance(hmac_secret, str) else hmac_secret
        if not self._secret:
            raise ValueError("The desk HMAC secret is required")
        self._http = httpx.Client(timeout=timeout, transport=transport, follow_redirects=False, trust_env=False)
        self.orders: Orders[_Reply, Iterator[object]] = Orders(
            self._call, _orders_unavailable, self._prepare_order, self._send_placement
        )
        self.deadman = LocalDeadMan(self)

    def __enter__(self) -> "LocalGatewayClient":
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()

    def close(self) -> None:
        self.deadman.stop_all()
        self._http.close()

    @staticmethod
    def _order_id(value: str | None) -> str:
        if value is None or not _CLIENT_ORDER_ID.fullmatch(value):
            raise ValueError("Client order ID must have 1 to 64 letters, digits, dots, underscores, colons or hyphens")
        return value

    def _prepare_order(self, order: PlaceOrderInput) -> Prepared:
        _require_uuid(order["accountId"])
        return self._policy.prepare("placeOrder", None, None, order_body(order), None)

    def _send_placement(self, prepared: Prepared) -> _Reply:
        self._order_id(prepared.client_order_id)
        # The public order bytes already contain the validated account; extraction avoids serializing them again.
        fields = json.loads(prepared.body)
        account_id = _require_uuid(str(fields["accountId"]))
        return self._send(prepared, account_id)

    def _call(
        self,
        operation_id: str,
        *,
        path: Mapping[str, str] | None = None,
        query: Query | None = None,
        body: Body = None,
        idempotency_key: str | None = None,
    ) -> _Reply:
        account = (
            (query or {}).get("accountId")
            if operation_id == "getOrder"
            else (body.get("accountId") if isinstance(body, Mapping) else None)
        )
        if not isinstance(account, str):
            raise ValueError("A desk request needs the venue account ID")
        prepared = self._policy.prepare(operation_id, path, query, body, idempotency_key)
        if operation_id in {"cancelOrder", "modifyOrder", "getOrder"}:
            self._order_id(prepared.client_order_id)
        return self._send(prepared, _require_uuid(account))

    def _send(self, prepared: Prepared, account_id: str) -> _Reply:
        operation = prepared.operation_id
        method = "GET" if operation == "getOrder" else "POST"
        request_id = str(uuid4())
        payload = b""
        if operation == "getOrder":
            client_id = self._order_id(prepared.client_order_id)
            target = (
                "/internal/orders/"
                + quote(client_id, safe="")
                + "?"
                + urlencode({"accountId": account_id, "userId": self._user_id})
            )
        else:
            target = _MUTATIONS[operation]
            if operation in {"cancelOrder", "modifyOrder"}:
                target += "?" + urlencode({"originalTarget": prepared.target})
            payload = json_bytes(
                {
                    "requestId": request_id,
                    "operation": operation,
                    "userId": self._user_id,
                    "venueAccountId": account_id,
                    "venue": self._venue.value,
                    "mode": "gateway",
                    "keyPlace": "signer",
                    "origin": "sdk",
                    "caller": {"kind": "local"},
                    "rawBody": b64encode(prepared.body).decode("ascii"),
                    "clientIp": "127.0.0.1",
                    "fee": None,
                    "leg": None,
                }
            )
        request = self._http.build_request(method, self._policy.origin + target, content=payload)
        timestamp = str(self._policy.now())
        raw_target = request.url.raw_path.decode("ascii")
        digest = hashlib.sha256(payload).hexdigest()
        signed = f"{method}\n{raw_target}\n{digest}\n{timestamp}".encode()
        request.headers.update(
            {
                "X-Atr-Caller": "local",
                "X-Atr-Request-Id": request_id,
                "X-Atr-Timestamp": timestamp,
                "X-Atr-Signature": hmac.digest(self._secret, signed, "sha256").hex(),
            }
        )
        if payload:
            request.headers["Content-Type"] = "application/json"
        try:
            response = self._http.send(request)
        except httpx.TransportError as failure:
            raise ApiError(
                "Desk gateway transport failed", request_id=request_id, client_order_id=prepared.client_order_id
            ) from failure
        if "X-Atr-Request-Id" in response.headers:
            response.headers["X-Request-Id"] = response.headers["X-Atr-Request-Id"]
        answer = self._policy.answer(response, prepared, 0, False, self._policy.now())
        if isinstance(answer, Retry):
            raise ApiError("Desk gateway request requires explicit retry", request_id=request_id)
        if operation == "getOrder" and isinstance(answer, Ok):
            if not isinstance(answer.data, Order) or answer.data.accountId != account_id:
                raise ApiError("Desk order belongs to another account", request_id=request_id)
        return answer
