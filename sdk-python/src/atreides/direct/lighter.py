"""Direct Lighter signing through a caller-owned lighter-sdk 1.1.8 SignerClient."""

import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal, localcontext
from importlib import import_module
from threading import RLock
from typing import Any, Literal, Protocol, TypedDict, cast

from .._http import ApiError, unix_ms
from .template import DirectContext, DirectFees, DirectStart, FeeField, OrderType, PublishedRate, TimeInForce

MAX_UINT48 = (1 << 48) - 1
_MAX_UINT32 = (1 << 32) - 1
_MAX_ORDER_INDEX = (1 << 60) - 1
_MAX_ORDER_EXPIRY = (1 << 63) - 1
HOUR_MS = 3_600_000
LighterTier = Literal["standard", "plus", "premium"]
_CLASS_CAPS = {"perp": 1000, "rwa_perp": 1000, "spot": 10000, "stock_token": 10000}
_SPOT = {"spot", "stock_token"}
NativeReply = tuple[int | str | None, str | None, str | None, str | None]


class LighterSignerClient(Protocol):
    account_index: int

    def sign_create_order(self, *args: Any, **kwargs: Any) -> NativeReply: ...

    def sign_modify_order(self, *args: Any, **kwargs: Any) -> NativeReply: ...

    def sign_create_grouped_orders(self, *args: Any, **kwargs: Any) -> NativeReply: ...

    def sign_approve_integrator(self, *args: Any, **kwargs: Any) -> NativeReply: ...


@dataclass(frozen=True)
class LighterAttribution:
    integrator_account_index: int = 0
    integrator_taker_fee: int = 0
    integrator_maker_fee: int = 0


@dataclass(frozen=True)
class LighterApproval:
    integrator_index: int
    expires_at: int
    max_perps_taker_fee: int
    max_perps_maker_fee: int
    max_spot_taker_fee: int
    max_spot_maker_fee: int


@dataclass(frozen=True)
class LighterAccount:
    account_index: int
    tier: LighterTier
    approvals: tuple[LighterApproval, ...]


@dataclass(frozen=True)
class LighterOrder:
    market_index: int
    client_order_index: int
    base_amount: int
    price: int
    is_ask: Literal[0, 1]
    order_type: int
    time_in_force: int
    reduce_only: Literal[0, 1]
    trigger_price: int
    order_expiry: int


@dataclass(frozen=True)
class LighterModification:
    market_index: int
    index: int
    base_amount: int
    price: int
    trigger_price: int
    original_attribution: LighterAttribution
    order_version: int = 0


@dataclass(frozen=True)
class LighterSelfTrade:
    behavior: int = 0
    equality: int = 0


_DEFAULT_SELF_TRADE = LighterSelfTrade()


@dataclass(frozen=True)
class LighterSigned:
    tx_type: int
    tx_info: str
    tx_hash: str


@dataclass(frozen=True)
class LighterDirectStart(DirectStart):
    tier: LighterTier
    approval: LighterApproval | None


class _OrderAttributes(TypedDict):
    integrator_account_index: int
    integrator_taker_fee: int
    integrator_maker_fee: int
    self_trade_behavior_mode: int
    self_trade_equality_mode: int
    nonce: int
    api_key_index: int


def _int(value: object, maximum: int = MAX_UINT48) -> bool:
    return type(value) is int and 0 <= value <= maximum


def _builder_index(value: str | None) -> int:
    if value is None or not value.isascii() or not value.isdecimal() or not 1 <= len(value) <= 15:
        raise ApiError("The published Lighter integrator is unavailable", code="fee_code_missing")
    index = int(value)
    if not 0 < index <= MAX_UINT48 or str(index) != value:
        raise ApiError("The published Lighter integrator is malformed", code="fee_code_missing")
    return index


def _encode(row: PublishedRate, builder: str | None, _type: OrderType, _tif: TimeInForce) -> FeeField:
    with localcontext() as ctx:
        ctx.prec = 38
        units = row.rate.scaleb(6)
        if units != units.to_integral_value() or str(int(units)) != row.encoded:
            raise ApiError("The published Lighter rate is not an exact ppm integer", code="fee_code_missing")
    return {
        "kind": "order_field",
        "builder": str(_builder_index(builder)),
        "encodedRate": row.encoded,
        "rate": row.rate,
    }


def _check_order(order: LighterOrder) -> None:
    if not (
        _int(order.market_index)
        and _int(order.client_order_index)
        and _int(order.base_amount)
        and _int(order.price, _MAX_UINT32)
        and order.price > 0
        and _int(order.is_ask, 1)
        and _int(order.reduce_only, 1)
        and _int(order.order_type, 6)
        and _int(order.time_in_force, 2)
        and _int(order.trigger_price, _MAX_UINT32)
        and _int(order.order_expiry, _MAX_ORDER_EXPIRY)
    ):
        raise ApiError("Invalid Lighter order wire units", code="invalid_request")


def _check_approval_caps(cls: str, approval: LighterApproval, attrs: LighterAttribution) -> None:
    if cls in _SPOT:
        taker_cap, maker_cap = approval.max_spot_taker_fee, approval.max_spot_maker_fee
    else:
        taker_cap, maker_cap = approval.max_perps_taker_fee, approval.max_perps_maker_fee
    if (
        not _int(taker_cap, _CLASS_CAPS[cls])
        or not _int(maker_cap, _CLASS_CAPS[cls])
        or attrs.integrator_taker_fee > taker_cap
        or attrs.integrator_maker_fee > maker_cap
    ):
        raise ApiError("Lighter fee exceeds the live side cap", code="fee_above_cap")


def _order_args(order: LighterOrder) -> tuple[int, ...]:
    return (
        order.market_index,
        order.client_order_index,
        order.base_amount,
        order.price,
        order.is_ask,
        order.order_type,
        order.time_in_force,
        order.reduce_only,
        order.trigger_price,
        order.order_expiry,
    )


def _checked_signature(
    result: NativeReply,
    tx_type: int,
    account_index: int,
    api_key_index: int,
    nonce: int,
    attributes: LighterAttribution | LighterApproval,
    self_trade: LighterSelfTrade,
    order_version: int,
) -> LighterSigned:
    actual_type, raw, digest, error = result
    invalid = ApiError("Lighter native signature did not match the request", code="signature_invalid")
    if error or actual_type not in (tx_type, str(tx_type)) or not raw or not digest:
        raise invalid
    try:
        row = json.loads(raw)
    except ValueError:
        raise invalid from None
    if (
        not isinstance(row, dict)
        or row.get("AccountIndex") != account_index
        or row.get("ApiKeyIndex") != api_key_index
        or row.get("Nonce") != nonce
        or not isinstance(row.get("Sig"), str)
        or not row["Sig"]
    ):
        raise invalid
    if isinstance(attributes, LighterAttribution):
        wire = row.get("L2TxAttributes") or {}
        if not isinstance(wire, dict) or tuple(wire.get(key, 0) for key in ("1", "2", "3", "6", "7", "8")) != (
            attributes.integrator_account_index,
            attributes.integrator_taker_fee,
            attributes.integrator_maker_fee,
            self_trade.behavior,
            self_trade.equality,
            order_version,
        ):
            raise invalid
    elif row.get("L1Sig", "") != "" or any(
        row.get(field) != expected
        for field, expected in (
            ("IntegratorAccountIndex", attributes.integrator_index),
            ("ApprovalExpiry", attributes.expires_at),
            ("MaxPerpsTakerFee", attributes.max_perps_taker_fee),
            ("MaxPerpsMakerFee", attributes.max_perps_maker_fee),
            ("MaxSpotTakerFee", attributes.max_spot_taker_fee),
            ("MaxSpotMakerFee", attributes.max_spot_maker_fee),
        )
    ):
        raise invalid
    return LighterSigned(tx_type, raw, digest)


class LighterDirect:
    """Signs venue-native transactions. The caller allocates a nonce and decides when to submit."""

    venue = "lighter"

    def __init__(
        self,
        signer: LighterSignerClient,
        api_key_index: int,
        markets: Mapping[int, str],
        read_account: Callable[[], LighterAccount],
        *,
        standard_tracking: bool = False,
        now: Callable[[], int] = unix_ms,
    ) -> None:
        if not _int(signer.account_index) or not _int(api_key_index, 254):
            raise ApiError("Invalid Lighter signer account or API key index", code="invalid_request")
        self._signer = signer
        self._account_index = signer.account_index
        self._api_key_index = api_key_index
        self._markets = dict(markets)
        self._read_account = read_account
        self._tracking = standard_tracking
        self._now = now
        self._fees = DirectFees(
            self.venue,
            False,
            _encode,
            cap_for=lambda cls: Decimal("0.01") if cls in _SPOT else Decimal("0.001"),
            now=now,
        )
        self._quote: DirectStart | None = None
        self._account: LighterAccount | None = None
        self._account_read_at = 0
        self._lock = RLock()

    def refresh_account(self) -> LighterAccount:
        with self._lock:
            account = self._read_account()
            if account.account_index != self._account_index:
                raise ApiError("Lighter venue readback belongs to a different account", code="account_not_bound")
            if account.tier not in ("standard", "plus", "premium"):
                raise ApiError("Lighter account tier is unavailable", code="fee_code_missing")
            self._account = LighterAccount(account.account_index, account.tier, tuple(account.approvals))
            self._account_read_at = self._now()
            return self._account

    def start(self, context: DirectContext) -> LighterDirectStart:
        if context.account_ref != str(self._account_index):
            raise ApiError("Lighter SDK context belongs to a different account", code="account_not_bound")
        with self._lock:
            self._quote = None
            self._account = None
            quote = self._fees.start(context)
            account = self.refresh_account()
            self._quote = quote
            index = None if quote.builder_ref is None else _builder_index(quote.builder_ref)
            return LighterDirectStart(
                quote.venue,
                quote.rates,
                quote.approved_cap,
                quote.class_caps,
                quote.builder_ref,
                quote.valid_until,
                account.tier,
                None if index is None else self._approval(account, index),
            )

    def _approval(self, account: LighterAccount, index: int) -> LighterApproval | None:
        rows = [
            row
            for row in account.approvals
            if row.integrator_index == index and _int(row.expires_at) and row.expires_at > self._now()
        ]
        return rows[0] if len(rows) == 1 else None

    def _refresh(self) -> None:
        quote = self._fees.refresh()
        if self._account is None or self._now() - self._account_read_at >= HOUR_MS:
            self.refresh_account()
        self._quote = quote

    def _policy(self, cls: str, self_trade: LighterSelfTrade) -> LighterAttribution:
        account, quote = self._account, self._quote
        if account is None or quote is None or quote.valid_until <= self._now():
            raise ApiError("The Lighter fee schedule is unavailable", code="fee_code_missing")
        if account.tier == "standard" and not self._tracking:
            return LighterAttribution()
        index = _builder_index(quote.builder_ref)
        approval = self._approval(account, index)
        if approval is None:
            if account.tier == "standard":
                return LighterAttribution()
            raise ApiError("Lighter integrator approval is missing or expired", code="fee_approval_missing")
        if account.tier == "standard" or self_trade.behavior or self_trade.equality:
            return LighterAttribution(index)
        taker = self._fees.fee_fields(cls, "market", "ioc", False)
        maker = self._fees.fee_fields(cls, "limit", "post_only", False)
        attrs = LighterAttribution(index, int(taker["encodedRate"]), int(maker["encodedRate"]))
        _check_approval_caps(cls, approval, attrs)
        return attrs

    def fee_fields(self, cls: str, type_: OrderType, tif: TimeInForce, reduce_only: bool) -> FeeField:
        with self._lock:
            try:
                if cls not in _CLASS_CAPS:
                    raise ApiError("Unknown Lighter instrument class", code="invalid_request")
                attrs = self._policy(cls, LighterSelfTrade())
                if not attrs.integrator_account_index:
                    return {"kind": "none", "reason": "venue_has_no_fee_path"}
                ppm = (
                    attrs.integrator_maker_fee
                    if type_ == "limit" and tif == "post_only"
                    else attrs.integrator_taker_fee
                )
                return {
                    "kind": "order_field",
                    "builder": str(attrs.integrator_account_index),
                    "encodedRate": str(ppm),
                    "rate": Decimal(ppm).scaleb(-6),
                }
            except Exception:
                if reduce_only:
                    return {"kind": "none", "reason": "close_without_approval"}
                raise

    def _attribution(self, market_index: int, reduce_only: bool, self_trade: LighterSelfTrade) -> LighterAttribution:
        with self._lock:
            try:
                cls = self._markets.get(market_index)
                if cls is None or cls not in _CLASS_CAPS:
                    raise ApiError("Unknown Lighter market", code="invalid_request")
                self._refresh()
                return self._policy(cls, self_trade)
            except Exception:
                if reduce_only:
                    return LighterAttribution()
                raise

    def _check_signing(self, nonce: int, self_trade: LighterSelfTrade) -> None:
        if self._signer.account_index != self._account_index:
            raise ApiError("Lighter signer changed accounts", code="account_not_bound")
        if not _int(nonce) or not _int(self_trade.behavior, 3) or not _int(self_trade.equality, 1):
            raise ApiError("Invalid Lighter nonce or self-trade mode", code="invalid_request")

    def _kwargs(self, attrs: LighterAttribution, nonce: int, self_trade: LighterSelfTrade) -> _OrderAttributes:
        return {
            "integrator_account_index": attrs.integrator_account_index,
            "integrator_taker_fee": attrs.integrator_taker_fee,
            "integrator_maker_fee": attrs.integrator_maker_fee,
            "self_trade_behavior_mode": self_trade.behavior,
            "self_trade_equality_mode": self_trade.equality,
            "nonce": nonce,
            "api_key_index": self._api_key_index,
        }

    def _signed(
        self,
        result: NativeReply,
        tx_type: int,
        nonce: int,
        attrs: LighterAttribution | LighterApproval,
        self_trade: LighterSelfTrade = _DEFAULT_SELF_TRADE,
        order_version: int = 0,
    ) -> LighterSigned:
        return _checked_signature(
            result, tx_type, self._account_index, self._api_key_index, nonce, attrs, self_trade, order_version
        )

    def sign_create_order(
        self, order: LighterOrder, nonce: int, self_trade: LighterSelfTrade = _DEFAULT_SELF_TRADE
    ) -> LighterSigned:
        self._check_signing(nonce, self_trade)
        _check_order(order)
        attrs = self._attribution(order.market_index, order.reduce_only == 1, self_trade)
        return self._signed(
            self._signer.sign_create_order(*_order_args(order), **self._kwargs(attrs, nonce, self_trade)),
            14,
            nonce,
            attrs,
            self_trade,
        )

    def sign_modify_order(
        self, order: LighterModification, nonce: int, self_trade: LighterSelfTrade = _DEFAULT_SELF_TRADE
    ) -> LighterSigned:
        self._check_signing(nonce, self_trade)
        if not (
            _int(order.market_index)
            and _int(order.index, _MAX_ORDER_INDEX)
            and order.index > 0
            and _int(order.base_amount)
            and _int(order.price, _MAX_UINT32)
            and order.price > 0
            and _int(order.trigger_price, _MAX_UINT32)
            and _int(order.order_version)
        ):
            raise ApiError("Invalid Lighter modification wire units", code="invalid_request")
        attrs = order.original_attribution
        if not (
            _int(attrs.integrator_account_index)
            and _int(attrs.integrator_taker_fee, 10000)
            and _int(attrs.integrator_maker_fee, 10000)
        ):
            raise ApiError("The original Lighter attribution is malformed", code="fee_code_missing")
        if attrs != LighterAttribution():
            with self._lock:
                cls = self._markets.get(order.market_index)
                if cls is None or cls not in _CLASS_CAPS:
                    raise ApiError("Unknown Lighter market", code="invalid_request")
                builder = _builder_index(None if self._quote is None else self._quote.builder_ref)
                if attrs.integrator_account_index != builder:
                    raise ApiError("Lighter modify integrator differs from the bound integrator", code="fee_above_cap")
                # Modify keeps the signed fees of the original order, subject to current venue approval.
                account = self.refresh_account()
                approval = self._approval(account, builder)
                if approval is None:
                    raise ApiError("Lighter integrator approval is missing or expired", code="fee_approval_missing")
                if (attrs.integrator_taker_fee or attrs.integrator_maker_fee) and (
                    account.tier == "standard" or self_trade.behavior or self_trade.equality
                ):
                    raise ApiError("Lighter tier or self-trade mode does not permit fees", code="fee_above_cap")
                _check_approval_caps(cls, approval, attrs)
        result = self._signer.sign_modify_order(
            order.market_index,
            order.index,
            order.base_amount,
            order.price,
            order.trigger_price,
            order_version=order.order_version,
            **self._kwargs(attrs, nonce, self_trade),
        )
        return self._signed(result, 17, nonce, attrs, self_trade, order.order_version)

    def sign_create_grouped_orders(
        self,
        grouping_type: Literal[1, 2, 3],
        orders: Sequence[LighterOrder],
        nonce: int,
        self_trade: LighterSelfTrade = _DEFAULT_SELF_TRADE,
    ) -> LighterSigned:
        self._check_signing(nonce, self_trade)
        if grouping_type not in (1, 2, 3) or len(orders) != (3 if grouping_type == 3 else 2):
            raise ApiError("Invalid Lighter order group", code="invalid_request")
        market = orders[0].market_index
        for order in orders:
            _check_order(order)
            if order.market_index != market:
                raise ApiError("A Lighter order group must use one market", code="invalid_request")
        attrs = self._attribution(market, all(order.reduce_only == 1 for order in orders), self_trade)
        module = import_module("lighter.signer_client")
        request_type = cast(Callable[..., object], module.CreateOrderTxReq)
        native_orders = [request_type(*_order_args(order)) for order in orders]
        result = self._signer.sign_create_grouped_orders(
            grouping_type, native_orders, **self._kwargs(attrs, nonce, self_trade)
        )
        return self._signed(result, 28, nonce, attrs, self_trade)

    def sign_approve_tracking(self, nonce: int, expires_at: int) -> LighterSigned:
        """Sign a Standard account's explicit zero-cap L2 approval."""
        self._check_signing(nonce, LighterSelfTrade())
        if not self._tracking:
            raise ApiError("Lighter Standard tracking requires opt-in", code="fee_approval_missing")
        with self._lock:
            self._refresh()
            if self.refresh_account().tier != "standard" or not _int(expires_at) or expires_at <= self._now():
                raise ApiError("Invalid Lighter zero-fee approval tier or expiry", code="invalid_request")
            index = _builder_index(None if self._quote is None else self._quote.builder_ref)
            attrs = LighterApproval(index, expires_at, 0, 0, 0, 0)
        result = self._signer.sign_approve_integrator(
            None, index, 0, 0, 0, 0, expires_at, nonce=nonce, api_key_index=self._api_key_index
        )
        return self._signed(result, 45, nonce, attrs)
