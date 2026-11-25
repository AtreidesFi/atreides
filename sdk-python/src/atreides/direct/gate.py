"""Direct Gate tagging (06-venue-cex.md section 8; CEX-403, CEX-406).

The caller owns the HMAC secret and signs the prepared request after ``prepare``: the header is not signed, ``text`` is
part of the body hash. Only the channel header and the ``text`` prefix change.
"""

import json
import re
import warnings
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Literal

GATE_CHANNEL_HEADER = "X-Gate-Channel-Id"
_ORDER_PATHS = frozenset(
    {
        "/api/v4/spot/orders",
        "/api/v4/spot/batch_orders",
        "/api/v4/futures/usdt/orders",
        "/api/v4/futures/usdt/batch_orders",
    }
)
_TEXT_BYTES = 28
_TEXT_CHARS = re.compile(r"[0-9A-Za-z_.-]*")
_WS_WARNING = "Gate WebSocket orders carry no channel id until Gate confirms the header there; this order is untagged."


def gate_text(prefix: str, client_id: str) -> str:
    """``t-`` + prefix + the bot's id, cut to 28 bytes after ``t-``."""
    own = client_id.removeprefix("t-")
    if not _TEXT_CHARS.fullmatch(prefix) or not _TEXT_CHARS.fullmatch(own):
        raise ValueError("invalid Gate text")
    joined = own if own.startswith(prefix) else prefix + own
    return "t-" + joined[:_TEXT_BYTES]


@dataclass(frozen=True)
class GatePrepared:
    body: str | None
    headers: Mapping[str, str]


@dataclass
class GateDirect:
    channel_id: str
    """Our channel id from config; empty means the program is not live and nothing is sent."""
    prefix_client_ids: bool = True
    client_prefix: str = "a1"
    warn: Callable[[str], None] = field(default=lambda message: warnings.warn(message, stacklevel=3))
    venue: Literal["gate"] = field(default="gate", init=False)
    _warned: bool = field(default=False, init=False, repr=False)

    def __post_init__(self) -> None:
        if self.channel_id and re.search(r"[^!-~]", self.channel_id):
            raise ValueError("invalid Gate channel id")
        if not _TEXT_CHARS.fullmatch(self.client_prefix) or len(self.client_prefix) > _TEXT_BYTES:
            raise ValueError("invalid Gate client prefix")

    def tag(self) -> dict[str, Any]:
        return {
            "venue": "gate",
            "kind": "header",
            "name": GATE_CHANNEL_HEADER,
            "value": self.channel_id,
            "signed": False,
        }

    def prepare(self, method: str, path: str, body: str | None = None) -> GatePrepared:
        """Tag one REST request. Only order-create paths change; no other field of the bot's request is touched."""
        if method.upper() != "POST" or path not in _ORDER_PATHS:
            return GatePrepared(body, {})
        headers = {GATE_CHANNEL_HEADER: self.channel_id} if self.channel_id else {}
        if not self.prefix_client_ids or body is None:
            return GatePrepared(body, headers)
        prefix = self.client_prefix
        changed = False

        def rewrite(order: Any) -> Any:
            nonlocal changed
            if not isinstance(order, dict) or not isinstance(order.get("text"), str):
                return order
            tagged = gate_text(prefix, order["text"])
            if tagged == order["text"]:
                return order
            changed = True
            return {**order, "text": tagged}

        parsed = json.loads(body)
        out = [rewrite(item) for item in parsed] if isinstance(parsed, list) else rewrite(parsed)
        # An unchanged body keeps the bot's exact bytes (number formatting, spacing).
        if not changed:
            return GatePrepared(body, headers)
        return GatePrepared(json.dumps(out, separators=(",", ":"), ensure_ascii=False), headers)

    def ws_order(self) -> dict[str, bool]:
        """WebSocket order channels are never tagged. Warns once per instance."""
        if not self._warned:
            self._warned = True
            self.warn(_WS_WARNING)
        return {"tagged": False}
