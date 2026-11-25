"""Typed route methods shared by the synchronous and asynchronous clients."""

from collections.abc import AsyncIterator, Awaitable, Callable, Generator, Iterator, Mapping
from dataclasses import dataclass
from typing import Generic, Literal, NotRequired, TypedDict, TypeVar, cast

from ._endpoints import Call, Money, OrderTrigger
from ._http import Answer, ApiError, Ok, Query, Refusal, _order_id
from .models import ClientOrderId, Route, RouteLeg, RouteQuote


class RouteInput(TypedDict):
    group: str
    side: Literal["buy", "sell"]
    type: Literal["limit", "market"]
    price: NotRequired[Money]
    size: NotRequired[Money]
    notional: NotRequired[Money]
    tif: NotRequired[Literal["ioc", "gtc", "post_only"]]
    reduceOnly: NotRequired[bool]
    tp: NotRequired[OrderTrigger]
    sl: NotRequired[OrderTrigger]
    maxSlippageBps: NotRequired[int]
    fundingHorizonHours: NotRequired[int]
    split: NotRequired[bool]
    maxLegs: NotRequired[int]
    crossClass: NotRequired[bool]
    allowPartial: NotRequired[bool]
    accountIds: NotRequired[list[str]]
    mode: NotRequired[Literal["gateway", "direct"]]


class RouteRef(TypedDict):
    routeId: str


Reply = Answer[object]
R = TypeVar("R")
P = TypeVar("P")


@dataclass(frozen=True, slots=True)
class RoutePlacement(Generic[R]):
    client_route_id: str
    result: R


def _wire(value: Mapping[str, object]) -> dict[str, object]:
    result = dict(value)
    for kind in ("tp", "sl"):
        trigger = result.get(kind)
        if isinstance(trigger, Mapping):
            result[kind] = {**trigger, "kind": kind, "limitPrice": trigger.get("limitPrice")}
    return result


class RouteEndpoints(Generic[R, P]):
    def __init__(self, call: Call[R], pages: Callable[[str, Query | None], P], now: Callable[[], int]) -> None:
        self._call = call
        self._pages = pages
        self._now = now

    def quote(self, route: RouteInput | RouteRef) -> R:
        return self._call("quoteRoute", body=_wire(route))

    def _new_id(self, supplied: str | None) -> str:
        return ClientOrderId(supplied if supplied is not None else _order_id(self._now())).root

    def register(self, route: RouteInput, client_route_id: str, quote_id: str | None, replan: int | None = None) -> R:
        body = {**_wire(route), "route": "best", "clientRouteId": client_route_id}
        if quote_id is not None:
            body["quoteId"] = quote_id
        if replan is not None:
            body["replan"] = replan
        return self._call("placeOrder", body=body)

    def place(
        self, route: RouteInput, *, quote_id: str | None = None, client_route_id: str | None = None
    ) -> RoutePlacement[R]:
        identity = self._new_id(client_route_id)
        return RoutePlacement(identity, self.register(route, identity, quote_id))

    def get(self, route_id: str) -> R:
        return self._call("getRoute", path={"routeId": route_id})

    def list(self, query: Query | None = None) -> P:
        return self._pages("listRoutes", query)

    def cancel(self, route_id: str) -> R:
        return self._call("cancelRoute", path={"routeId": route_id})


Request = (
    tuple[Literal["get", "quote"], str]
    | tuple[Literal["place"], tuple[RouteInput, str, str | None, int | None]]
    | tuple[Literal["leg"], tuple[RouteLeg, Route]]
    | tuple[Literal["wait"], float]
)


def execute_steps(
    original: RouteInput, identity: str, quote_id: str | None, follow_up: bool, new_id: Callable[[], str]
) -> Generator[Request, Answer[object] | None, Answer[object]]:
    # The sync and async clients drive the same protocol. Every request receives its answer before the next action.
    input_route = cast(RouteInput, dict(original))
    sent: set[str] = set()
    follow_ups = 0
    reply = yield ("place", (input_route, identity, quote_id, None))
    max_reads_per_parent = 1440 if original.get("mode") == "direct" else 240
    remaining_reads = max_reads_per_parent
    while remaining_reads > 0:
        remaining_reads -= 1
        if isinstance(reply, Refusal):
            return reply
        if reply is None:
            raise ApiError("The route operation returned no answer", client_order_id=identity)
        route = reply.data
        if not isinstance(route, Route) or route.clientRouteId.root != identity:
            raise ApiError("Malformed route response", client_order_id=identity, request_id=reply.request_id)
        if route.state in {"done", "partial", "failed", "cancelled"}:
            remaining = route.requestedSize - route.filledSize
            if not (
                follow_up
                and route.mode == "direct"
                and route.state == "partial"
                and route.endReason in {"depth", "not_replanned"}
                and route.report is not None
                and not route.report.provisional
                and remaining > 0
                and follow_ups < 2
            ):
                return reply
            identity = new_id()
            input_route = cast(RouteInput, dict(original))
            input_route.pop("notional", None)
            input_route["size"] = remaining
            follow_ups += 1
            remaining_reads = max_reads_per_parent
            reply = yield ("place", (input_route, identity, None, None))
            continue
        if route.execution == "client":
            for leg in route.legs:
                ready = leg.state == "reserved" or (leg.place == "direct" and leg.state == "placed")
                if ready and leg.clientOrderId.root not in sent:
                    sent.add(leg.clientOrderId.root)
                    yield ("leg", (leg, route))
            if (
                route.mode != "direct"
                and route.state == "working"
                and route.replans < 2
                and route.legs
                and all(leg.state in {"released", "lapsed"} and leg.place != "wallet" for leg in route.legs)
                and route.filledSize < route.requestedSize
            ):
                next_quote = yield ("quote", str(route.routeId.root))
                if isinstance(next_quote, Ok) and isinstance(next_quote.data, RouteQuote):
                    next_reply = yield ("place", (input_route, identity, next_quote.data.quoteId, route.replans + 1))
                    if not isinstance(next_reply, Refusal):
                        reply = next_reply
                        continue
                    if next_reply.code != "route_not_live":
                        return next_reply
                elif isinstance(next_quote, Refusal):
                    if next_quote.code not in {"route_not_live", "route_no_candidate", "insufficient_depth"}:
                        return next_quote
                else:
                    raise ApiError("Malformed route quote", client_order_id=identity)
        yield ("wait", 0.25)
        reply = yield ("get", str(route.routeId.root))
    raise ApiError("Route outcome is still pending", code="unknown_outcome", client_order_id=identity)


def _missing_placer(leg: RouteLeg) -> ApiError:
    return ApiError("This route requires a client leg placer", client_order_id=leg.clientOrderId.root)


class Routes(RouteEndpoints[Reply, Iterator[object]]):
    def __init__(
        self,
        call: Call[Reply],
        pages: Callable[[str, Query | None], Iterator[object]],
        now: Callable[[], int],
        sleep: Callable[[float], None],
    ) -> None:
        super().__init__(call, pages, now)
        self._sleep = sleep

    def execute(
        self,
        route: RouteInput,
        *,
        quote_id: str | None = None,
        client_route_id: str | None = None,
        place_leg: Callable[[RouteLeg, Route], None] | None = None,
        follow_up: bool = False,
    ) -> RoutePlacement[Reply]:
        identity = self._new_id(client_route_id)
        steps = execute_steps(route, identity, quote_id, follow_up, lambda: self._new_id(None))
        reply: Reply | None = None
        try:
            while True:
                request = steps.send(reply)
                reply = None
                if request[0] == "place":
                    reply = self.register(*request[1])
                elif request[0] == "get":
                    reply = self.get(request[1])
                elif request[0] == "quote":
                    reply = self.quote({"routeId": request[1]})
                elif request[0] == "leg":
                    leg, parent = request[1]
                    if place_leg is None:
                        raise _missing_placer(leg)
                    place_leg(leg, parent)
                else:
                    self._sleep(request[1])
        except StopIteration as done:
            return RoutePlacement(identity, cast(Reply, done.value))
        finally:
            steps.close()


class AsyncRoutes(RouteEndpoints[Awaitable[Reply], AsyncIterator[object]]):
    def __init__(
        self,
        call: Call[Awaitable[Reply]],
        pages: Callable[[str, Query | None], AsyncIterator[object]],
        now: Callable[[], int],
        sleep: Callable[[float], Awaitable[None]],
    ) -> None:
        super().__init__(call, pages, now)
        self._sleep = sleep

    def execute(
        self,
        route: RouteInput,
        *,
        quote_id: str | None = None,
        client_route_id: str | None = None,
        place_leg: Callable[[RouteLeg, Route], Awaitable[None]] | None = None,
        follow_up: bool = False,
    ) -> RoutePlacement[Awaitable[Reply]]:
        identity = self._new_id(client_route_id)

        async def run() -> Reply:
            steps = execute_steps(route, identity, quote_id, follow_up, lambda: self._new_id(None))
            reply: Reply | None = None
            try:
                while True:
                    request = steps.send(reply)
                    reply = None
                    if request[0] == "place":
                        reply = await self.register(*request[1])
                    elif request[0] == "get":
                        reply = await self.get(request[1])
                    elif request[0] == "quote":
                        reply = await self.quote({"routeId": request[1]})
                    elif request[0] == "leg":
                        leg, parent = request[1]
                        if place_leg is None:
                            raise _missing_placer(leg)
                        await place_leg(leg, parent)
                    else:
                        await self._sleep(request[1])
            except StopIteration as done:
                return cast(Reply, done.value)
            finally:
                steps.close()

        return RoutePlacement(identity, run())
