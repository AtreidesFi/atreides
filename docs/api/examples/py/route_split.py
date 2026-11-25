from collections.abc import Callable

from atreides import Client, Refusal
from atreides.models import Route, RouteLeg, RouteQuote
from atreides.route import RouteInput


def route_split(
    client: Client,
    request: RouteInput,
    client_route_id: str,
    place_leg: Callable[[RouteLeg, Route], None] | None = None,
) -> Route:
    fields: RouteInput = {**request, "split": True, "maxLegs": min(request.get("maxLegs", 4), 4)}
    quote = client.route.quote(fields)
    if isinstance(quote, Refusal):
        raise RuntimeError(quote.message)
    if not isinstance(quote.data, RouteQuote):
        raise RuntimeError("Expected a route quote")
    placement = client.route.execute(fields, quote_id=quote.data.quoteId, client_route_id=client_route_id, place_leg=place_leg)
    result = placement.result
    if isinstance(result, Refusal):
        raise RuntimeError(result.message)
    if not isinstance(result.data, Route):
        raise RuntimeError("Expected a route")
    return result.data
