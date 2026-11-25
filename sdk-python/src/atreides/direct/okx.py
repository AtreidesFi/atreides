"""Direct OKX tag injection (06 CEX 1.5, 8; CEX-102). The caller keeps its own OKX client and signing."""

import logging
import re
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from .._http import ApiError, unix_ms
from .template import DirectContext, DirectFees, DirectStart, FeeField, OrderType, TimeInForce

_TAG = re.compile(r"[A-Za-z0-9]{1,16}")
_NOTICE = "OKX tag replaced by the platform tag; move your own label to clOrdId"
_LOG = logging.getLogger("atreides.direct.okx")

# Request bodies on these paths carry `tag`. close-position is tagged although the gateway never calls it (X10).
OKX_TAGGED_PATHS = frozenset(
    {
        "/api/v5/trade/order",
        "/api/v5/trade/batch-orders",
        "/api/v5/trade/close-position",
        "/api/v5/trade/order-algo",
        "/api/v5/tradingBot/grid/order-algo",
        "/api/v5/tradingBot/recurring/order-algo",
        "/api/v5/sprd/order",
        "/api/v5/rfq/create-rfq",
        "/api/v5/rfq/create-quote",
    }
)
_WS_TAGGED_OPS = frozenset({"order", "batch-orders", "sprd-order"})


def _no_encoder(*_: object) -> FeeField:  # OKX pays a program share; no rate of ours is encoded (06 1.6)
    raise ApiError("OKX carries no per-order rate", code="fee_code_missing")


class OkxDirect(DirectFees):
    def __init__(
        self,
        *,
        override_existing_tag: bool = True,
        on_notice: Callable[[str], None] | None = None,
        now: Callable[[], int] = unix_ms,
    ) -> None:
        super().__init__("okx", False, _no_encoder, now=now)
        self._override = override_existing_tag
        self._notice = on_notice or _LOG.warning
        self._code: str | None = None

    def _read(self, context: DirectContext) -> DirectStart:
        start = super()._read(context)
        code = start.builder_ref or None
        if code is not None and _TAG.fullmatch(code) is None:
            raise ApiError("OKX tag from the fee schedule is malformed", code="fee_code_missing")
        self._code = code
        return start

    def tag(self) -> str | None:
        """The tag, or None while the program code is not live (nothing is sent then)."""
        return self._code

    def fee_fields(self, cls: str, order_type: OrderType, tif: TimeInForce, reduce_only: bool) -> FeeField:
        code = self._code
        if code is None:
            return {"kind": "none", "reason": "venue_has_no_fee_path"}
        return {"kind": "broker_tag", "tag": code}

    def tag_order(self, order: Mapping[str, Any]) -> dict[str, Any]:
        """One order or batch item as a copy; every other field is untouched."""
        code = self._code
        copy = dict(order)
        if code is None:
            return copy
        existing = copy.get("tag")
        if existing not in (None, "", code):
            if not self._override:
                return copy
            self._notice(_NOTICE)
        copy["tag"] = code
        return copy

    def tag_rest_body(
        self, path: str, body: Mapping[str, Any] | Sequence[Mapping[str, Any]]
    ) -> dict[str, Any] | list[dict[str, Any]]:
        """Tags the JSON body for a REST path; other paths and query strings pass through."""
        tagged = path.split("?", 1)[0] in OKX_TAGGED_PATHS
        if isinstance(body, Mapping):
            return self.tag_order(body) if tagged else dict(body)
        return [self.tag_order(item) if tagged else dict(item) for item in body]

    def tag_ws_frame(self, frame: Mapping[str, Any]) -> dict[str, Any]:
        """Tags the args of a WebSocket order, batch-orders or sprd-order frame."""
        copy = dict(frame)
        args = copy.get("args")
        if copy.get("op") in _WS_TAGGED_OPS and isinstance(args, list):
            copy["args"] = [self.tag_order(arg) for arg in args]
        return copy
