"""Direct order and fee handling around the official Hyperliquid SDK's Exchange."""

import importlib
import json
import re
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from threading import RLock
from types import MappingProxyType
from typing import Literal, NotRequired, Protocol, TypedDict

from .._http import ApiError, Refusal, unix_ms
from ..client import Client
from ..dec import parse_dec
from ..models import FeeSchedule, VenueAccountInfo

Class = Literal["perp", "hip3_perp", "spot", "outcome"]
Tif = Literal["Alo", "Ioc", "Gtc"]
Grouping = Literal["na", "normalTpsl", "positionTpsl"]
Liquidity = Literal["maker", "taker"]
Network = Literal["mainnet", "testnet"]
HardCap = Literal[0, 20, 50]
STEP = Decimal("0.00001")
HARD_CAP: dict[str, int] = {"perp": 20, "hip3_perp": 20, "spot": 20, "outcome": 50}
ADDRESS = re.compile(r"0x[0-9a-fA-F]{40}\Z")
CLOID = re.compile(r"0x[0-9a-f]{32}\Z")


class Builder(TypedDict):
    b: str
    f: int


class SdkOrder(TypedDict):
    coin: str
    is_buy: bool
    limit_px: float
    sz: float
    order_type: dict[str, object]
    reduce_only: bool
    cloid: NotRequired["Cloid"]


class Info(Protocol):
    def name_to_asset(self, name: str) -> int: ...


class Wallet(Protocol):
    @property
    def address(self) -> str: ...


class Exchange(Protocol):
    @property
    def info(self) -> Info: ...

    @property
    def wallet(self) -> Wallet: ...

    @property
    def account_address(self) -> str | None: ...

    @property
    def vault_address(self) -> str | None: ...

    @property
    def expires_after(self) -> int | None: ...

    def bulk_orders(
        self, order_requests: list[SdkOrder], builder: Builder | None = None, grouping: Grouping = "na"
    ) -> object: ...

    def approve_builder_fee(self, builder: str, max_fee_rate: str) -> object: ...


@dataclass(frozen=True)
class Cloid:
    value: str

    def to_raw(self) -> str:
        return self.value


@dataclass(frozen=True)
class Order:
    coin: str
    cls: Class
    is_buy: bool
    limit_px: str | Decimal
    size: str | Decimal
    reduce_only: bool = False
    tif: Tif = "Gtc"
    trigger_px: str | Decimal | None = None
    tpsl: Literal["tp", "sl"] | None = None
    trigger_market: bool = False
    cloid: str | None = None


@dataclass(frozen=True)
class FeeRate:
    cls: str
    liquidity: Liquidity
    rate: Decimal
    encoded: str


@dataclass(frozen=True)
class DirectStart:
    rates: tuple[FeeRate, ...]
    approved_cap: Decimal | None
    class_caps: Mapping[str, Decimal]
    builder_ref: str
    valid_until: int
    venue: Literal["hyperliquid"] = "hyperliquid"


@dataclass(frozen=True)
class BatchResult:
    indices: tuple[int, ...]
    status: Literal["sent", "refused", "unknown"]
    response: object = None
    code: str | None = None


def _address(value: str) -> str:
    if not ADDRESS.fullmatch(value):
        raise ValueError("Invalid Hyperliquid address")
    return value.lower()


def _asset_class(asset: int) -> str:
    if type(asset) is not int or asset < 0:
        raise ValueError("Invalid Hyperliquid asset id")
    if asset < 10_000:
        return "perp"
    if asset < 100_000:
        return "spot"
    if asset < 100_000_000:
        return "hip3_perp"
    return "outcome"


def _wire_float(amount: str | Decimal) -> float:
    exact = parse_dec(amount)
    wire = float(exact)
    # The official Exchange accepts floats and serializes at eight places.
    if exact <= 0 or Decimal(f"{wire:.8f}") != exact:
        raise ValueError("Hyperliquid SDK cannot represent this amount exactly")
    return wire


# The L1 actions the signing policy allows an agent key to sign (06 section 3.2); everything else is refused.
L1_ACTIONS = frozenset(
    {"order", "cancel", "cancelByCloid", "batchModify", "modify", "updateLeverage", "scheduleCancel", "noop"}
)


def sign_l1(
    wallet: object,
    action: Mapping[str, object],
    *,
    nonce: int,
    network: Network,
    vault_address: str | None = None,
    expires_after: int | None = None,
) -> dict[str, object]:
    """Sign an allowed L1 action with the official SDK's own signing (never re-implemented): `{r, s, v}`.

    The network is explicit and has no default: the SDK signs source "a" for mainnet and "b" for testnet, so a testnet
    action cannot be replayed on mainnet. Any action outside `L1_ACTIONS` (a withdrawal, a transfer, an approval, a key
    change) is refused here, before the SDK sees it.
    """
    kind = action.get("type")
    if not isinstance(kind, str) or kind not in L1_ACTIONS:
        raise ApiError(f"Hyperliquid action {kind!r} is not one this signer signs", code="policy_refused")
    if network not in ("mainnet", "testnet"):
        raise ApiError("Hyperliquid network must be mainnet or testnet")
    if vault_address is not None and not ADDRESS.fullmatch(vault_address):
        raise ApiError("Hyperliquid vault address is not an address")
    if isinstance(nonce, bool) or not isinstance(nonce, int) or nonce <= 0:
        raise ApiError("Hyperliquid nonce must be a positive integer")
    signing = importlib.import_module("hyperliquid.utils.signing")
    raw = signing.sign_l1_action(
        wallet,
        dict(action),
        vault_address.lower() if vault_address else None,
        nonce,
        expires_after,
        network == "mainnet",
    )
    return {"r": f"0x{int(raw['r'], 16):064x}", "s": f"0x{int(raw['s'], 16):064x}", "v": raw["v"]}


def exchange_body(
    action: Mapping[str, object],
    nonce: int,
    signature: Mapping[str, object],
    vault_address: str | None = None,
    expires_after: int | None = None,
) -> str:
    """The exact POST /exchange body (compact, in the venue's field order)."""
    return json.dumps(
        {
            "action": action,
            "nonce": nonce,
            "signature": signature,
            "vaultAddress": vault_address,
            "expiresAfter": expires_after,
        },
        separators=(",", ":"),
        ensure_ascii=False,
    )


def link_address(client: Client, account_ref: str, sign: Callable[[str], str]) -> object:
    """Sign the API's account challenge locally with the master wallet."""
    return client.accounts.link(
        "hyperliquid", _address(account_ref), lambda message: {"scheme": "eip191", "signature": sign(message)}
    )


def nautilus_hyperliquid_options() -> dict[str, bool]:
    """Disable Nautilus's own builder before using a separate direct order path."""
    return {"include_builder_attribution": False}


class HyperliquidDirect:
    venue = "hyperliquid"

    def __init__(self, exchange: Exchange, *, now: Callable[[], int] = unix_ms) -> None:
        self._exchange = exchange
        self._now = now
        self._client: Client | None = None
        self._account_ref: str | None = None
        self._schedule: DirectStart | None = None
        self._lock = RLock()
        self._last_sdk_ms = -1

    def start(self, client: Client, account_ref: str) -> DirectStart:
        with self._lock:
            address = _address(account_ref)
            effective = self._exchange.account_address or self._exchange.wallet.address
            if _address(effective) != address:
                raise ApiError("Exchange belongs to another Hyperliquid account", code="account_not_bound")
            self._client = client
            self._account_ref = address
            return self._read()

    def _read(self) -> DirectStart:
        self._schedule = None
        client = self._client
        if client is None:
            raise ApiError("Start the direct session first", code="fee_approval_missing")
        accounts = client.accounts.list()
        if isinstance(accounts, Refusal) or not isinstance(accounts.data, list):
            raise ApiError("Account is unavailable", code="account_not_bound")
        raw_vault = self._exchange.vault_address
        vault = _address(raw_vault) if raw_vault is not None else None
        bound = [
            account
            for account in accounts.data
            if isinstance(account, VenueAccountInfo)
            and account.venue == self.venue
            and account.accountRef.lower() == self._account_ref
            and (account.subAccountRef or "").lower() == (vault or "")
        ]
        if len(bound) != 1:
            raise ApiError("No unique Hyperliquid account", code="account_not_bound")
        answer = client.fees.schedule({"venue": self.venue, "accountId": str(bound[0].accountId.root)})
        if isinstance(answer, Refusal) or not isinstance(answer.data, list):
            raise ApiError("Fee schedule is unavailable", code="unavailable")
        rows = [row for row in answer.data if isinstance(row, FeeSchedule) and row.venue == self.venue]
        if len(rows) != 1 or rows[0].builderRef is None or rows[0].unit != "tenth_bp" or rows[0].step != STEP:
            raise ApiError("Invalid Hyperliquid fee schedule", code="fee_approval_missing")

        row = rows[0]
        builder = row.builderRef
        assert builder is not None
        now = self._now()
        valid_until = int(row.validUntil.root.timestamp() * 1000)
        if valid_until <= now:
            raise ApiError("Fee schedule expired", code="fee_approval_missing")
        caps = {cls: min(cap, HARD_CAP[cls] * STEP) for cls, cap in row.classCaps.items() if cls in HARD_CAP}
        rates: list[FeeRate] = []
        seen: set[tuple[str, str]] = set()
        for rate in row.rates:
            if rate.originClass != "algo" or rate.cls not in HARD_CAP:
                continue
            key = (rate.cls.value, rate.liquidity.value)
            if (
                not re.fullmatch(r"[1-9][0-9]{0,3}", rate.encodedRate)
                or rate.rate != int(rate.encodedRate) * STEP
                or int(rate.validFrom.root.timestamp() * 1000) > now
                or int(rate.validUntil.root.timestamp() * 1000) < valid_until
                or key in seen
            ):
                raise ApiError("Invalid Hyperliquid rate", code="fee_approval_missing")
            seen.add(key)
            liquidity: Liquidity = "maker" if rate.liquidity == "maker" else "taker"
            rates.append(FeeRate(rate.cls.value, liquidity, rate.rate, rate.encodedRate))
        approved = row.approval.cap if row.approval is not None and row.approval.state == "approved" else None
        current = DirectStart(tuple(rates), approved, MappingProxyType(caps), _address(builder), valid_until)
        self._schedule = current
        return current

    def fee_fields(self, cls: Class, tif: Tif, reduce_only: bool, *, trigger: bool = False) -> Builder | None:
        with self._lock:
            current = self._schedule
            code = "fee_approval_missing"
            if current is not None and current.valid_until > self._now() and (current.approved_cap or 0) > 0:
                liquidity = "maker" if tif == "Alo" and not trigger else "taker"
                row = next((row for row in current.rates if row.cls == cls and row.liquidity == liquidity), None)
                cap = current.class_caps.get(cls)
                if row is not None and cap is not None and current.approved_cap is not None:
                    if row.rate <= cap and row.rate <= current.approved_cap:
                        return {"b": current.builder_ref, "f": int(row.encoded)}
                    code = "fee_above_cap"
            if reduce_only:
                return None
            raise ApiError("Hyperliquid builder rate is unavailable for this order", code=code)

    def _prepare(self, order: Order) -> SdkOrder:
        if _asset_class(self._exchange.info.name_to_asset(order.coin)) != order.cls:
            raise ValueError("Hyperliquid asset class differs from the order")
        if order.tif not in ("Alo", "Ioc", "Gtc"):
            raise ValueError("Invalid Hyperliquid time in force")
        if order.trigger_px is None:
            if order.tpsl is not None or order.trigger_market:
                raise ValueError("Missing Hyperliquid trigger price")
            order_type: dict[str, object] = {"limit": {"tif": order.tif}}
        else:
            if order.tpsl not in ("tp", "sl"):
                raise ValueError("Invalid Hyperliquid trigger side")
            order_type = {
                "trigger": {
                    "triggerPx": _wire_float(order.trigger_px),
                    "isMarket": order.trigger_market,
                    "tpsl": order.tpsl,
                }
            }
        result: SdkOrder = {
            "coin": order.coin,
            "is_buy": order.is_buy,
            "limit_px": _wire_float(order.limit_px),
            "sz": _wire_float(order.size),
            "order_type": order_type,
            "reduce_only": order.reduce_only,
        }
        if order.cloid is not None:
            if not CLOID.fullmatch(order.cloid):
                raise ValueError("Invalid Hyperliquid client order id")
            result["cloid"] = Cloid(order.cloid)
        return result

    def _reserve_sdk_ms(self) -> None:
        # The official Exchange allocates the action nonce from wall time inside each public method.
        current = int(time.time() * 1000)
        if current <= self._last_sdk_ms:
            time.sleep(0.001)
            current = int(time.time() * 1000)
            if current <= self._last_sdk_ms:
                raise ApiError("Hyperliquid SDK clock did not advance", code="invalid_request")
        self._last_sdk_ms = current

    def place_batch(self, orders: Sequence[Order], grouping: Grouping = "na") -> list[BatchResult]:
        with self._lock:
            if grouping not in ("na", "normalTpsl", "positionTpsl"):
                raise ValueError("Invalid Hyperliquid order grouping")
            if self._schedule is None or self._schedule.valid_until <= self._now():
                try:
                    self._read()
                except (ApiError, TypeError, ValueError):
                    pass  # Fee attribution may be unavailable. Reduce-only orders can still be sent.
            groups: list[tuple[Builder | None, list[SdkOrder], list[int]]] = []
            results: list[BatchResult] = []
            for index, order in enumerate(orders):
                try:
                    request = self._prepare(order)
                    builder = self.fee_fields(
                        order.cls, order.tif, order.reduce_only, trigger=order.trigger_px is not None
                    )
                except ApiError as error:
                    results.append(BatchResult((index,), "refused", code=error.code or "invalid_request"))
                    continue
                except (ValueError, KeyError):
                    results.append(BatchResult((index,), "refused", code="invalid_request"))
                    continue
                if groups and groups[-1][0] == builder:
                    groups[-1][1].append(request)
                    groups[-1][2].append(index)
                else:
                    groups.append((builder, [request], [index]))
            if grouping != "na" and (len(groups) != 1 or results):
                if not results and orders and all(order.reduce_only for order in orders):
                    groups = [
                        (None, [request for _, group, _ in groups for request in group], list(range(len(orders))))
                    ]
                else:
                    return [BatchResult(tuple(range(len(orders))), "refused", code="invalid_request")]
            for builder, requests, indices in groups:
                try:
                    self._reserve_sdk_ms()
                except ApiError:
                    # Nothing was sent for this group. Earlier groups keep their results.
                    results.append(BatchResult(tuple(indices), "refused", code="invalid_request"))
                    continue
                try:
                    response = self._exchange.bulk_orders(requests, builder=builder, grouping=grouping)
                except Exception:
                    results.append(BatchResult(tuple(indices), "unknown", code="unknown_outcome"))
                else:
                    results.append(BatchResult(tuple(indices), "sent", response))
            return sorted(results, key=lambda result: result.indices[0])

    def approve_builder_fee(self, owner: Exchange, cap_tenth_bp: HardCap, read_cap: Callable[[str, str], int]) -> int:
        with self._lock:
            account = self._account_ref
            if (
                account is None
                or _address(owner.wallet.address) != account
                or owner.vault_address is not None
                or owner.expires_after is not None
                or self._exchange.vault_address is not None
            ):
                raise ApiError("Approval needs the main wallet and no vault or expiry", code="signer_mismatch")
            schedule = self._schedule
            if schedule is None or schedule.valid_until <= self._now():
                schedule = self._read()
            if type(cap_tenth_bp) is not int or cap_tenth_bp not in (0, 20, 50):
                raise ApiError("Invalid builder approval cap", code="fee_above_cap")
            self._reserve_sdk_ms()
            self._schedule = None
            builder = schedule.builder_ref
            try:
                owner.approve_builder_fee(builder, {0: "0%", 20: "0.02%", 50: "0.05%"}[cap_tenth_bp])
            except Exception:
                pass  # A transport failure can leave the approval applied.
            observed = read_cap(account, builder)
            if type(observed) is not int or (observed != 0 if cap_tenth_bp == 0 else observed < cap_tenth_bp):
                raise ApiError("Hyperliquid approval was not confirmed", code="unknown_outcome")
            return observed
