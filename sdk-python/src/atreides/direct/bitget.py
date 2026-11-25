"""Direct Bitget UTA v3: `X-CHANNEL-API-CODE` on REST, top-level `apiCode` on WS, `clientOid` prefix.

06 sections 5.2, 1.5, 8; CEX-306. Both transforms run before signing: the header is outside the signed string,
`clientOid` is inside the body.
"""

import logging
import re
from collections.abc import Callable
from typing import Any

from .cex import CexTag, prefix_client_id, with_header

_CLIENT_OID = re.compile(r"[A-Za-z0-9_-]+")
_CLIENT_OID_MAX = 32
_REST_PLACE = "/api/v3/trade/place-order"
_REST_BATCH = "/api/v3/trade/place-batch"
_NOTICE = "Bitget: the bot's own channel API code was replaced by the platform code"
_LOG = logging.getLogger("atreides.direct.bitget")


class DirectBitget:
    def __init__(
        self,
        tag: CexTag,
        *,
        prefix_client_ids: bool = True,
        client_prefix: str = "a1",
        on_notice: Callable[[str], None] | None = None,
    ) -> None:
        if tag.kind != "header" or tag.name != "X-CHANNEL-API-CODE":
            raise ValueError("Bitget tag is the X-CHANNEL-API-CODE header")
        self._tag = tag
        self._prefix = client_prefix if prefix_client_ids else None
        self._notice = on_notice or _LOG.warning

    def tag(self) -> CexTag:
        return self._tag

    def _order(self, order: Any) -> Any:
        if self._prefix is None or not isinstance(order, dict) or "clientOid" not in order:
            return order
        return {**order, "clientOid": prefix_client_id(order["clientOid"], self._prefix, _CLIENT_OID_MAX, _CLIENT_OID)}

    def rest(self, request: dict[str, Any]) -> dict[str, Any]:
        """`place-order` and `place-batch` get the header and the id prefix; everything else passes through."""
        if request["method"] != "POST" or request["path"] not in (_REST_PLACE, _REST_BATCH):
            return request
        out = dict(request)
        if self._tag.value != "":
            out["headers"] = with_header(
                request.get("headers"), self._tag.name, self._tag.value, (), lambda: self._notice(_NOTICE)
            )
        body = request.get("body")
        if isinstance(body, list):
            out["body"] = [self._order(item) for item in body]
        elif "body" in request:
            out["body"] = self._order(body)
        return out

    def ws(self, frame: dict[str, Any]) -> dict[str, Any]:
        """`trade` frames with topic `place-order` or `batch-place` get top-level `apiCode` and the id prefix."""
        if frame["op"] != "trade" or frame.get("topic") not in ("place-order", "batch-place"):
            return frame
        out = dict(frame)
        if self._tag.value != "":
            held = frame.get("apiCode")
            if isinstance(held, str) and held not in ("", self._tag.value):
                self._notice(_NOTICE)
            out["apiCode"] = self._tag.value
        out["args"] = [self._order(arg) for arg in frame["args"]]
        return out
