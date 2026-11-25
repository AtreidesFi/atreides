"""Named operations shared by the sync and async clients."""

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from decimal import Decimal
from typing import Generic, Literal, NotRequired, Protocol, TypedDict, TypeVar

from ._http import Body, Prepared, Query

R = TypeVar("R", covariant=True)
P = TypeVar("P", covariant=True)
Money = Decimal | str


class Call(Protocol[R]):
    def __call__(
        self,
        operation_id: str,
        *,
        path: Mapping[str, str] | None = None,
        query: Query | None = None,
        body: Body = None,
        idempotency_key: str | None = None,
    ) -> R: ...


class OrderTrigger(TypedDict):
    triggerPrice: Money
    limitPrice: NotRequired[Money]


def order_body(order: "PlaceOrderInput") -> dict[str, object]:
    wire: dict[str, object] = dict(order)
    for kind in ("tp", "sl"):
        trigger = wire.get(kind)
        if isinstance(trigger, Mapping):
            wire[kind] = {**trigger, "kind": kind, "limitPrice": trigger.get("limitPrice")}
    return wire


class PlaceOrderInput(TypedDict):
    accountId: str
    instrumentId: str
    side: Literal["buy", "sell"]
    type: Literal["limit", "market"]
    price: NotRequired[Money]
    size: NotRequired[Money]
    notional: NotRequired[Money]
    reduceOnly: NotRequired[bool]
    tif: NotRequired[Literal["gtc", "ioc", "fok", "post_only", "gtd"]]
    goodTil: NotRequired[str]
    tp: NotRequired[OrderTrigger]
    sl: NotRequired[OrderTrigger]
    slippageBps: NotRequired[int]
    clientOrderId: NotRequired[str]
    strategyRunId: NotRequired[str]
    routeId: NotRequired[str]


@dataclass(frozen=True, slots=True)
class Placement(Generic[R]):
    client_order_id: str
    result: R


class Venues(Generic[R]):
    def __init__(self, call: Call[R]) -> None:
        self._call = call

    def list(self) -> R:
        return self._call("listVenues")

    def get(self, venue: str) -> R:
        return self._call("getVenue", path={"venue": venue})

    def budget(self, venue: str, account_id: str) -> R:
        return self._call("getVenueBudget", path={"venue": venue}, query={"accountId": account_id})


class Instruments(Generic[R, P]):
    def __init__(self, call: Call[R], pages: Callable[[str, Query | None], P]) -> None:
        self._call = call
        self._pages = pages

    def list(self, query: Query | None = None) -> P:
        return self._pages("listInstruments", query)

    def get(self, instrument_id: str) -> R:
        return self._call("getInstrument", path={"instrumentId": instrument_id})


class Fees(Generic[R]):
    def __init__(self, call: Call[R]) -> None:
        self._call = call

    def schedule(self, query: Query | None = None) -> R:
        return self._call("getFees", query=query)

    def quote(self, body: Body) -> R:
        return self._call("quoteFee", body=body)


class Orders(Generic[R, P]):
    def __init__(
        self,
        call: Call[R],
        pages: Callable[[str, Query | None], P],
        prepare_order: Callable[[PlaceOrderInput], Prepared],
        send: Callable[[Prepared], R],
    ) -> None:
        self._call = call
        self._pages = pages
        self._prepare = prepare_order
        self._send = send

    def place(self, order: PlaceOrderInput) -> Placement[R]:
        request = self._prepare(order)
        if request.client_order_id is None:
            raise RuntimeError("A placed order needs its client order id")
        return Placement(request.client_order_id, self._send(request))

    def get(self, account_id: str, client_order_id: str) -> R:
        return self._call("getOrder", path={"clientOrderId": client_order_id}, query={"accountId": account_id})

    def list(self, query: Query | None = None) -> P:
        return self._pages("listOrders", query)

    def cancel(self, account_id: str, client_order_id: str) -> R:
        return self._call("cancelOrder", path={"clientOrderId": client_order_id}, body={"accountId": account_id})

    def modify(
        self,
        account_id: str,
        client_order_id: str,
        *,
        price: Money | None = None,
        size: Money | None = None,
    ) -> R:
        changes: dict[str, object] = {"accountId": account_id}
        if price is not None:
            changes["price"] = price
        if size is not None:
            changes["size"] = size
        if len(changes) == 1:
            raise ValueError("A modify needs a price or size")
        return self._call("modifyOrder", path={"clientOrderId": client_order_id}, body=changes)

    def cancel_all(self, account_id: str, instrument_id: str | None = None) -> R:
        body: dict[str, object] = {"accountId": account_id}
        if instrument_id is not None:
            body["instrumentId"] = instrument_id
        return self._call("cancelAll", body=body)


class DeadMan(Generic[R]):
    def __init__(self, call: Call[R]) -> None:
        self._call = call

    def arm(self, account_id: str, timeout_ms: int) -> R:
        if type(timeout_ms) is not int or not 5000 <= timeout_ms <= 600000:
            raise ValueError("Dead-man timeout must be from 5000 to 600000 milliseconds")
        return self._call("setDeadMan", body={"accountId": account_id, "timeoutMs": timeout_ms})

    def clear(self, account_id: str) -> R:
        return self._call("setDeadMan", body={"accountId": account_id, "timeoutMs": 0})
