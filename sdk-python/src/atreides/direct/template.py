"""Shared start, refresh and fee-cap checks for venue-native SDK modules."""

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from decimal import Decimal
from threading import RLock
from types import MappingProxyType
from typing import Literal, Required, TypedDict

from .._http import ApiError, Refusal, unix_ms
from ..client import Client
from ..models import FeeSchedule, VenueAccountInfo

OrderType = Literal["limit", "market"]
TimeInForce = Literal["gtc", "ioc", "fok", "post_only", "gtd"]


class FeeField(TypedDict, total=False):
    kind: Required[str]
    rate: Decimal
    builder: str
    encodedRate: str
    reason: str
    code: str
    tag: str
    prefix: str
    portion: str
    recipient: str


@dataclass(frozen=True)
class PublishedRate:
    cls: str
    liquidity: str
    rate: Decimal
    encoded: str


@dataclass(frozen=True)
class DirectStart:
    venue: str
    rates: tuple[PublishedRate, ...]
    approved_cap: Decimal | None
    class_caps: Mapping[str, Decimal]
    builder_ref: str | None
    valid_until: int


@dataclass(frozen=True)
class DirectContext:
    client: Client
    account_ref: str


def _identity(reference: str) -> str:
    return reference


def _refuse(reduce_only: bool, code: str) -> FeeField:
    if reduce_only:
        return {"kind": "none", "reason": "close_without_approval"}
    raise ApiError("Direct SDK fee is unavailable for this order", code=code)


class DirectFees:
    def __init__(
        self,
        venue: str,
        requires_approval: bool,
        encode: Callable[[PublishedRate, str | None, OrderType, TimeInForce], FeeField],
        *,
        cap_for: Callable[[str], Decimal] | None = None,
        normalize_account: Callable[[str], str] = _identity,
        now: Callable[[], int] = unix_ms,
    ) -> None:
        self.venue = venue
        self._requires_approval = requires_approval
        self._encode = encode
        self._cap_for = cap_for
        self._normalize = normalize_account
        self._now = now
        self._context: DirectContext | None = None
        self._published: DirectStart | None = None
        self._lock = RLock()

    def start(self, context: DirectContext) -> DirectStart:
        with self._lock:
            self._context = DirectContext(context.client, self._normalize(context.account_ref))
            self._published = None
            return self.refresh()

    def refresh(self) -> DirectStart:
        with self._lock:
            if self._published is not None and self._published.valid_until > self._now():
                return self._published
            context = self._context
            if context is None:
                raise ApiError("Start the direct module before signing", code="fee_code_missing")
            self._published = self._read(context)
            return self._published

    def _read(self, context: DirectContext) -> DirectStart:
        accounts = context.client.accounts.list()
        if isinstance(accounts, Refusal) or not isinstance(accounts.data, list):
            raise ApiError("Direct SDK account is unavailable", code="account_not_bound")
        matches = [
            account
            for account in accounts.data
            if isinstance(account, VenueAccountInfo)
            and account.venue == self.venue
            and self._normalize(account.subAccountRef or account.accountRef) == context.account_ref
        ]
        if len(matches) != 1:
            raise ApiError("Direct SDK account does not resolve uniquely", code="account_not_bound")
        answer = context.client.fees.schedule({"venue": self.venue, "accountId": str(matches[0].accountId.root)})
        if isinstance(answer, Refusal) or not isinstance(answer.data, list):
            raise ApiError("Direct SDK fee schedule is unavailable", code="unavailable")
        schedules = [row for row in answer.data if isinstance(row, FeeSchedule) and row.venue == self.venue]
        if len(schedules) != 1:
            raise ApiError("Direct SDK fee schedule does not resolve uniquely", code="fee_code_missing")
        row = schedules[0]
        at = self._now()
        valid_until = int(row.validUntil.root.timestamp() * 1000)
        if valid_until <= at or row.step < 0:
            raise ApiError("Direct SDK fee schedule is expired or malformed", code="fee_code_missing")
        caps: dict[str, Decimal] = {}
        rates: list[PublishedRate] = []
        seen: set[tuple[str, str]] = set()
        for rate in row.rates:
            if rate.originClass != "algo":
                continue
            cls = rate.cls.value
            liquidity = rate.liquidity.value
            key = (cls, liquidity)
            if (
                key in seen
                or rate.rate < 0
                or (row.step > 0 and rate.rate % row.step != 0)
                or int(rate.validFrom.root.timestamp() * 1000) > at
                or int(rate.validUntil.root.timestamp() * 1000) < valid_until
            ):
                raise ApiError("Direct SDK rate is malformed", code="fee_code_missing")
            seen.add(key)
            rates.append(PublishedRate(cls, liquidity, rate.rate, rate.encodedRate))
            cap = row.classCaps.get(cls)
            if cap is not None:
                caps[cls] = min(cap, self._cap_for(cls)) if self._cap_for is not None else cap
        approval = row.approval
        approved_cap = approval.cap if approval is not None and approval.state == "approved" else None
        return DirectStart(self.venue, tuple(rates), approved_cap, MappingProxyType(caps), row.builderRef, valid_until)

    def fee_fields(self, cls: str, order_type: OrderType, tif: TimeInForce, reduce_only: bool) -> FeeField:
        with self._lock:
            current = self._published
            if current is None or current.valid_until <= self._now():
                return _refuse(reduce_only, "fee_code_missing")
            approved_cap = current.approved_cap
            if self._requires_approval and (approved_cap is None or approved_cap <= 0):
                return _refuse(reduce_only, "fee_approval_missing")
            liquidity = "maker" if order_type == "limit" and tif == "post_only" else "taker"
            rate = next((rate for rate in current.rates if rate.cls == cls and rate.liquidity == liquidity), None)
            cap = current.class_caps.get(cls)
            if rate is None or cap is None:
                return _refuse(reduce_only, "fee_code_missing")
            if rate.rate > cap or (approved_cap is not None and rate.rate > approved_cap):
                return _refuse(reduce_only, "fee_above_cap")
            try:
                field = self._encode(rate, current.builder_ref, order_type, tif)
            except Exception:
                if reduce_only:
                    return _refuse(True, "fee_code_missing")
                raise
            if "rate" in field and (field["rate"] < 0 or field["rate"] > rate.rate):
                return _refuse(reduce_only, "fee_above_cap")
            return field
