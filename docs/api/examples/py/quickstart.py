from typing import Literal
from urllib.parse import urlsplit

from atreides import Client, Refusal
from atreides._endpoints import PlaceOrderInput
from atreides.keys import read_key_file


def staging_client(origin: str, key_path: str) -> Client:
    url = urlsplit(origin)
    if url.scheme != 'https' or not url.hostname or origin != f'https://{url.netloc}' or url.username is not None:
        raise ValueError('Use the configured testnet HTTPS origin')
    key = read_key_file(key_path)
    if key.key_id is None or not key.key_id.startswith('test_') or key.api_origin != origin:
        raise ValueError('Use a test key bound to that origin')
    return Client(origin, key)


def discover_venues(client: Client) -> list[object]:
    reply = client.venues.list()
    if isinstance(reply, Refusal):
        raise RuntimeError(f'{reply.code}: {reply.message}')
    if not isinstance(reply.data, list):
        raise RuntimeError('Expected a list of venues')
    return reply.data


def place_and_cancel(client: Client, order: PlaceOrderInput):
    """Place a resting limit order and cancel it using the SDK-generated ID."""
    placement = client.orders.place(order)
    placed = placement.result
    if isinstance(placed, Refusal):
        raise RuntimeError(f"{placed.code}: {placed.message}")
    cancelled = client.orders.cancel(order["accountId"], placement.client_order_id)
    if isinstance(cancelled, Refusal):
        raise RuntimeError(f"{cancelled.code}: {cancelled.message}")
    return placed.data, cancelled.data, placement.client_order_id


def close_position(client: Client, account_id: str, instrument_id: str, close_side: Literal["buy", "sell"], size: str):
    """Close the specified size on the same instrument."""
    closed = client.orders.place({
        "accountId": account_id, "instrumentId": instrument_id, "side": close_side,
        "size": size, "type": "market", "reduceOnly": True,
    }).result
    if isinstance(closed, Refusal):
        raise RuntimeError(f"{closed.code}: {closed.message}")
    return closed.data
