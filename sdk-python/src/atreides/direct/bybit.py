"""Direct Bybit V5: Broker ID in `Referer` and the `orderLinkId` prefix (06 sections 4.6, 1.5, 8; CEX-201).

Both transforms run before signing: `Referer` is outside the signed string, `orderLinkId` is inside the body.
"""

import logging
import re
from collections.abc import Callable, Mapping
from typing import Any

from .cex import CexTag, prefix_client_id, with_header

_LINK_ID = re.compile(r"[A-Za-z0-9_-]+")
_LINK_ID_MAX = 36
_REST_CREATE = "/v5/order/create"
_REST_BATCH = "/v5/order/create-batch"
# Bybit reads the Broker ID from either spelling, so a bot's `X-Referer` is dropped too.
_ALIASES = ("X-Referer",)
_NOTICE = "Bybit: the bot's own Referer was replaced by the platform Broker ID"
_LOG = logging.getLogger("atreides.direct.bybit")


class DirectBybit:
    def __init__(
        self,
        tag: CexTag,
        *,
        prefix_client_ids: bool = True,
        client_prefix: str = "a1",
        on_notice: Callable[[str], None] | None = None,
    ) -> None:
        if tag.kind != "header" or tag.name != "Referer":
            raise ValueError("Bybit tag is the Referer header")
        self._tag = tag
        self._prefix = client_prefix if prefix_client_ids else None
        self._notice = on_notice or _LOG.warning

    def _tagged(self, headers: Mapping[str, str] | None) -> dict[str, str]:
        return with_header(headers, self._tag.name, self._tag.value, _ALIASES, lambda: self._notice(_NOTICE))

    def tag(self) -> CexTag:
        return self._tag

    def _link(self, order: Any) -> Any:
        if self._prefix is None or not isinstance(order, dict) or "orderLinkId" not in order:
            return order
        return {**order, "orderLinkId": prefix_client_id(order["orderLinkId"], self._prefix, _LINK_ID_MAX, _LINK_ID)}

    def _batch(self, body: Any) -> Any:
        if isinstance(body, dict) and isinstance(body.get("request"), list):
            return {**body, "request": [self._link(item) for item in body["request"]]}
        return body

    def rest(self, request: dict[str, Any]) -> dict[str, Any]:
        """Create and batch-create get `Referer` and the id prefix; every other request passes through."""
        create = request["method"] == "POST" and request["path"] == _REST_CREATE
        many = request["method"] == "POST" and request["path"] == _REST_BATCH
        if not create and not many:
            return request
        out = dict(request)
        if self._tag.value != "":
            out["headers"] = self._tagged(request.get("headers"))
        if "body" in request:
            out["body"] = self._link(request["body"]) if create else self._batch(request["body"])
        return out

    def ws(self, frame: dict[str, Any]) -> dict[str, Any]:
        """`order.create` and `order.create-batch` get `header.Referer` and the id prefix."""
        if frame["op"] not in ("order.create", "order.create-batch"):
            return frame
        single = frame["op"] == "order.create"
        out = dict(frame)
        if self._tag.value != "":
            out["header"] = self._tagged(frame.get("header"))
        out["args"] = [(self._link(arg) if single else self._batch(arg)) for arg in frame["args"]]
        return out
