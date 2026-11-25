"""Place the exact legs that a route reserves for the caller."""

from collections.abc import Awaitable, Callable, Mapping
from datetime import UTC, datetime
from decimal import Decimal

from ._endpoints import PlaceOrderInput
from ._http import ApiError, JsonValue, Refusal
from ._route_endpoints import RouteInput, RoutePlacement
from .aclient import AsyncClient
from .client import Client
from .models import Route, RouteLeg
from .relay import RelaySigner, relay_place, relay_place_async

__all__ = ["DirectLegOrder", "RouteInput", "RoutePlacement", "leg_placer", "leg_placer_async"]


class DirectLegOrder(PlaceOrderInput):
    venueClientId: str
    ourRate: Decimal
    placeBefore: datetime


def _bad(leg: RouteLeg, message: str) -> ApiError:
    return ApiError(message, code="route_leg_invalid", client_order_id=leg.clientOrderId.root)


def _order(leg: RouteLeg, parent: Route, now: datetime) -> PlaceOrderInput:
    ready = leg.state == "reserved" or (leg.place == "direct" and leg.state == "placed")
    if not ready or leg.placeBefore.root <= now:
        raise _bad(leg, "Route leg reservation expired")
    if leg.size <= 0 or leg.limitPrice <= 0 or leg.ourRate < 0:
        raise _bad(leg, "Invalid route leg terms")
    return {
        "accountId": str(leg.accountId.root),
        "instrumentId": leg.instrumentId,
        "side": parent.side.value,
        "type": "limit",
        "tif": leg.tif.value,
        "size": leg.size,
        "price": leg.limitPrice,
        "reduceOnly": parent.reduceOnly,
        "routeId": str(parent.routeId.root),
        "clientOrderId": leg.clientOrderId.root,
    }


def _direct(leg: RouteLeg, order: PlaceOrderInput) -> DirectLegOrder:
    if not leg.venueClientId:
        raise _bad(leg, "Missing venue client order ID")
    return {**order, "venueClientId": leg.venueClientId, "ourRate": leg.ourRate, "placeBefore": leg.placeBefore.root}


def leg_placer(
    *,
    client: Client | None = None,
    relay: Mapping[str, RelaySigner[Mapping[str, JsonValue]]] | None = None,
    direct: Mapping[str, Callable[[DirectLegOrder], None]] | None = None,
    wallet: Callable[[PlaceOrderInput, RouteLeg, Route], None] | None = None,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> Callable[[RouteLeg, Route], None]:
    def place(leg: RouteLeg, parent: Route) -> None:
        order = _order(leg, parent, now())
        venue = leg.venue.value
        if leg.place == "direct":
            sender = None if direct is None else direct.get(venue)
            if sender is None:
                raise _bad(leg, "No matching direct route leg sender")
            sender(_direct(leg, order))
            return
        if leg.place == "wallet":
            if wallet is None:
                raise _bad(leg, "No wallet route leg signer")
            wallet(order, leg, parent)
            return
        if client is None or leg.place == "gateway":
            raise _bad(leg, "No client for reserved route leg")
        if leg.place == "relay":
            signer = None if relay is None else relay.get(venue)
            if signer is None or signer.venue != venue:
                raise _bad(leg, "No matching relay route leg signer")
            reply = relay_place(client, order, signer).result
        else:
            reply = client.orders.place(order).result
        if isinstance(reply, Refusal):
            raise ApiError(
                reply.message, code=reply.code, request_id=reply.request_id, client_order_id=leg.clientOrderId.root
            )

    return place


def leg_placer_async(
    *,
    client: AsyncClient | None = None,
    relay: Mapping[str, RelaySigner[Awaitable[Mapping[str, JsonValue]]]] | None = None,
    direct: Mapping[str, Callable[[DirectLegOrder], Awaitable[None]]] | None = None,
    wallet: Callable[[PlaceOrderInput, RouteLeg, Route], Awaitable[None]] | None = None,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> Callable[[RouteLeg, Route], Awaitable[None]]:
    async def place(leg: RouteLeg, parent: Route) -> None:
        order = _order(leg, parent, now())
        venue = leg.venue.value
        if leg.place == "direct":
            sender = None if direct is None else direct.get(venue)
            if sender is None:
                raise _bad(leg, "No matching direct route leg sender")
            await sender(_direct(leg, order))
            return
        if leg.place == "wallet":
            if wallet is None:
                raise _bad(leg, "No wallet route leg signer")
            await wallet(order, leg, parent)
            return
        if client is None or leg.place == "gateway":
            raise _bad(leg, "No client for reserved route leg")
        if leg.place == "relay":
            signer = None if relay is None else relay.get(venue)
            if signer is None or signer.venue != venue:
                raise _bad(leg, "No matching relay route leg signer")
            reply = (await relay_place_async(client, order, signer)).result
        else:
            reply = await client.orders.place(order).result
        if isinstance(reply, Refusal):
            raise ApiError(
                reply.message, code=reply.code, request_id=reply.request_id, client_order_id=leg.clientOrderId.root
            )

    return place
