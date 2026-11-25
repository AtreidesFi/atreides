"""Sign a relay order locally, then send it through the authenticated API client."""

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Generic, Literal, TypeVar

from ._endpoints import Money, Placement, PlaceOrderInput
from ._http import ApiError, JsonValue, _order_id, encode_json, unix_ms
from .aclient import AsyncClient
from .client import Client, Reply

T = TypeVar("T", covariant=True)


@dataclass(frozen=True)
class RelaySignRequest:
    kind: Literal["place", "cancel", "modify"]
    account_id: str
    client_order_id: str
    new_client_order_id: str | None = None
    order: PlaceOrderInput | None = None
    change: Mapping[str, Money] | None = None


@dataclass(frozen=True)
class RelaySigner(Generic[T]):
    venue: str
    sign: Callable[[RelaySignRequest], T]


def _signed(venue: str, wire: Mapping[str, JsonValue]) -> dict[str, object]:
    if not isinstance(wire, Mapping) or not wire:
        raise TypeError("Relay signer must return a non-empty wire object")
    return {"venue": venue, "signed": wire}


def _place(order: PlaceOrderInput) -> RelaySignRequest:
    encode_json(order)
    client_order_id = order.get("clientOrderId") or _order_id(unix_ms())
    return RelaySignRequest(
        "place", order["accountId"], client_order_id, order={**order, "clientOrderId": client_order_id}
    )


def _place_body(request: RelaySignRequest, venue: str, wire: Mapping[str, JsonValue]) -> dict[str, object]:
    body: dict[str, object] = {
        "accountId": request.account_id,
        "clientOrderId": request.client_order_id,
        "relay": _signed(venue, wire),
    }
    if request.order is not None and "strategyRunId" in request.order:
        body["strategyRunId"] = request.order["strategyRunId"]
    if request.order is not None and "routeId" in request.order:
        body["routeId"] = request.order["routeId"]
    return body


def _modify(
    account_id: str, client_order_id: str, change: Mapping[str, Money], new_client_order_id: str | None
) -> RelaySignRequest:
    if set(change) - {"price", "size"}:
        raise TypeError("Relay modify takes a price or size")
    encode_json(change)
    replacement = new_client_order_id or _order_id(unix_ms())
    return RelaySignRequest("modify", account_id, client_order_id, new_client_order_id=replacement, change=dict(change))


def _modify_body(request: RelaySignRequest, venue: str, wire: Mapping[str, JsonValue]) -> dict[str, object]:
    return {
        **(request.change or {}),
        "accountId": request.account_id,
        "newClientOrderId": request.new_client_order_id,
        "signed": _signed(venue, wire),
    }


def relay_place(
    client: Client, order: PlaceOrderInput, signer: RelaySigner[Mapping[str, JsonValue]]
) -> Placement[Reply]:
    request = _place(order)
    body = _place_body(request, signer.venue, signer.sign(request))
    return Placement(request.client_order_id, client.request("placeOrder", body=body))


def relay_cancel(
    client: Client, account_id: str, client_order_id: str, signer: RelaySigner[Mapping[str, JsonValue]]
) -> Reply:
    request = RelaySignRequest("cancel", account_id, client_order_id)
    return client.request(
        "cancelOrder",
        path={"clientOrderId": client_order_id},
        body={"accountId": account_id, "signed": _signed(signer.venue, signer.sign(request))},
    )


def relay_modify(
    client: Client,
    account_id: str,
    client_order_id: str,
    change: Mapping[str, Money],
    signer: RelaySigner[Mapping[str, JsonValue]],
    *,
    new_client_order_id: str | None = None,
) -> Placement[Reply]:
    request = _modify(account_id, client_order_id, change, new_client_order_id)
    assert request.new_client_order_id is not None
    body = _modify_body(request, signer.venue, signer.sign(request))
    try:
        result = client.request("modifyOrder", path={"clientOrderId": client_order_id}, body=body)
    except ApiError as error:
        error.client_order_id = request.new_client_order_id
        raise
    return Placement(request.new_client_order_id, result)


async def relay_place_async(
    client: AsyncClient, order: PlaceOrderInput, signer: RelaySigner[Awaitable[Mapping[str, JsonValue]]]
) -> Placement[Reply]:
    request = _place(order)
    body = _place_body(request, signer.venue, await signer.sign(request))
    return Placement(request.client_order_id, await client.request("placeOrder", body=body))


async def relay_cancel_async(
    client: AsyncClient,
    account_id: str,
    client_order_id: str,
    signer: RelaySigner[Awaitable[Mapping[str, JsonValue]]],
) -> Reply:
    request = RelaySignRequest("cancel", account_id, client_order_id)
    return await client.request(
        "cancelOrder",
        path={"clientOrderId": client_order_id},
        body={"accountId": account_id, "signed": _signed(signer.venue, await signer.sign(request))},
    )


async def relay_modify_async(
    client: AsyncClient,
    account_id: str,
    client_order_id: str,
    change: Mapping[str, Money],
    signer: RelaySigner[Awaitable[Mapping[str, JsonValue]]],
    *,
    new_client_order_id: str | None = None,
) -> Placement[Reply]:
    request = _modify(account_id, client_order_id, change, new_client_order_id)
    assert request.new_client_order_id is not None
    body = _modify_body(request, signer.venue, await signer.sign(request))
    try:
        result = await client.request("modifyOrder", path={"clientOrderId": client_order_id}, body=body)
    except ApiError as error:
        error.client_order_id = request.new_client_order_id
        raise
    return Placement(request.new_client_order_id, result)
