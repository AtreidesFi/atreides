from collections.abc import Iterable

from atreides import ApiError, Client, PlaceOrderInput, Refusal
from atreides.models import Order


def quote_once(client: Client, quote: PlaceOrderInput) -> str:
    order: PlaceOrderInput = {**quote, 'type': 'limit', 'tif': 'post_only'}
    try:
        placement = client.orders.place(order)
    except ApiError as error:
        raise RuntimeError(f'Look up {error.client_order_id} before quoting again') from error
    identifier = placement.client_order_id
    if isinstance(placement.result, Refusal):
        raise RuntimeError(f'{identifier}: {placement.result.code}')
    read = client.orders.get(quote['accountId'], identifier)
    if isinstance(read, Refusal) or not isinstance(read.data, Order) or read.data.state == 'unknown':
        raise RuntimeError(f'Resolve {identifier} before quoting again')
    if read.data.state not in {'filled', 'cancelled', 'rejected'}:
        cancel = client.orders.cancel(quote['accountId'], identifier)
        if isinstance(cancel, Refusal) or not isinstance(cancel.data, Order) or cancel.data.state not in {'filled', 'cancelled'}:
            raise RuntimeError(f'Confirm cancellation of {identifier} before quoting again')
    return identifier


def market_making_loop(client: Client, quotes: Iterable[PlaceOrderInput]) -> None:
    for quote in quotes:
        quote_once(client, quote)
