"""The strategy/v1 validator. Issues use the TS validator's ordering and JSON pointers."""

import hashlib
import json
import math
import re
from collections.abc import Callable, Collection, Mapping
from copy import deepcopy
from dataclasses import dataclass
from typing import Any, Literal, TypedDict, TypeVar, cast

from ._amounts import format_dec, parse_dec
from ._canonical import canonical_json

_Object = dict[str, Any]
_T = TypeVar("_T")
_Check = Callable[[object, str], object]
_DecRule = tuple[Callable[[int], bool], str]
_ONE = 10**18
_POS = (lambda value: value > 0, "must be greater than 0")
_NONNEG = (lambda value: value >= 0, "must not be negative")
_FRAC = (lambda value: 0 < value <= _ONE, "must be a fraction in (0, 1]")
_BAND = (lambda value: _ONE // 10 <= value <= 10 * _ONE, "must be in [0.1, 10]")
_OFFSET = (lambda value: 0 < value <= _ONE // 5, "must be in (0, 0.2]")
_QUOTES = ("usdc", "usdt", "usdg", "pusd")
_TIMEFRAMES = ("1s", "1m", "5m", "15m", "1h", "4h", "1d")
_CLASSES = ("perp", "hip3_perp", "rwa_perp", "spot", "outcome", "option", "prediction", "stock_token")
_LAB_CLASSES = _CLASSES[:4]
_VENUES = (
    "hyperliquid",
    "aster",
    "lighter",
    "pacifica",
    "grvt",
    "extended",
    "nado",
    "derive",
    "dydx",
    "sodex",
    "txflow",
    "arcus",
    "polymarket-perps",
    "risex",
    "qfex",
    "polymarket",
    "kalshi",
    "robinhood-chain",
    "okx",
    "bybit",
    "bitget",
    "gate",
    "kraken",
    "binance",
)
_SPOT_LACKS = (
    "funding_rate",
    "funding_apr",
    "open_interest",
    "oi_change",
    "basis",
    "liquidations_long_usd",
    "liquidations_short_usd",
)
_SERIES = ("open", "high", "low", "close", "volume", "mark", *_SPOT_LACKS)
_INDICATORS = (
    "sma",
    "ema",
    "rsi",
    "atr",
    "stddev",
    "zscore",
    "highest",
    "lowest",
    "roc",
    "bb_upper",
    "bb_lower",
    "vwap",
)
_COMPARISONS = ("gt", "gte", "lt", "lte", "crosses_above", "crosses_below")
_MODES = ("isolated", "cross")
_FEEDS = ("bars", "trades", "book", "funding", "marks", "open_interest")
_OPERANDS = ("series", "signal", "feature", "const", "spread")
_CONDITIONS = ("all", "any", "not", "cmp", "held")
_BASE = ("schema", "kind", "timeframe", "quoteAsset", "signals", "limits")
_KINDS = {
    "rules": ("universe", "entry", "exit", "sizing", "execution"),
    "spread": ("legs", "entry", "exit", "sizing", "execution", "legTimeoutMs"),
    "scan": ("universe", "entry", "exit", "sizing", "execution"),
    "schedule": ("universe", "schedule", "exit", "execution"),
    "bot": ("universe", "feeds", "marginMode"),
    "mandate": ("mandate", "subject"),
}
_AMOUNT_KEYS = {"fixed_notional": "notional", "fixed_margin": "margin", "equity_fraction": "fraction"}
_SUBJECT_KEYS = {"strategy_version": "strategyVersionId", "account_history": "venueAccountId"}
_SIGNAL_NAME = re.compile("[a-z][a-z0-9_]{0,31}")
_ASSET = re.compile("[a-z0-9][a-z0-9._-]{0,31}")


class Instrument(TypedDict):
    cls: str
    quote: str
    maxLeverage: int | None


@dataclass(frozen=True)
class InstrumentCatalog:
    instruments: Mapping[str, Instrument]
    features: Collection[str]


class SpecIssue(TypedDict):
    pointer: str
    code: str
    message: str


class ValidSpec(TypedDict):
    ok: Literal[True]
    spec: _Object
    canonical: str
    sha256: bytes


class InvalidSpec(TypedDict):
    ok: Literal[False]
    issues: list[SpecIssue]


def _at(base: str, key: str | int) -> str:
    return base + "/" + str(key).replace("~", "~0").replace("/", "~1")


def _plain(value: object) -> _Object | None:
    return cast(_Object, value) if type(value) is dict and all(isinstance(key, str) for key in value) else None


def _member(value: object, options: Collection[str]) -> bool:
    return isinstance(value, str) and value in options


def _keys(obj: _Object) -> list[str]:
    # Object.keys visits canonical array indices before other members, even for JSON text that orders them later.
    indices = [
        key
        for key in obj
        if key.isascii() and key.isdigit() and len(key) <= 10 and str(int(key)) == key and int(key) < 2**32 - 1
    ]
    index_keys = set(indices)
    return sorted(indices, key=int) + [key for key in obj if key not in index_keys]


class _Validation:
    def __init__(self, catalog: InstrumentCatalog) -> None:
        self.catalog = catalog
        self.issues: list[SpecIssue] = []
        self.decimals: list[tuple[str, int]] = []
        self.quote: str | None = None
        self.kind = ""
        self.any_class = False
        self.signal_names: set[str] = set()
        self.universe: set[str] = set()
        self.comparisons = 0

    def add(self, pointer: str, code: str, message: str) -> None:
        self.issues.append({"pointer": pointer, "code": code, "message": message})

    def no_extra(self, obj: _Object, pointer: str, allowed: Collection[str]) -> None:
        for key in _keys(obj):
            if key not in allowed:
                self.add(_at(pointer, key), "unknown_field", f"{key} is not a field here")

    def checked_object(self, value: object, pointer: str, allowed: Collection[str]) -> _Object | None:
        obj = _plain(value)
        if obj is None:
            self.add(pointer, "type", "must be an object")
        else:
            self.no_extra(obj, pointer, allowed)
        return obj

    def field(self, obj: _Object, key: str, pointer: str, check: Callable[[object, str], _T]) -> _T | None:
        here = _at(pointer, key)
        if key in obj:
            return check(obj[key], here)
        self.add(here, "required", f"{key} is required")
        return None

    @staticmethod
    def nullable(check: _Check) -> _Check:
        return lambda value, here: check(value, here) if value is not None else None

    def integer(self, value: object, here: str, minimum: int, maximum: int | None = None) -> bool:
        if isinstance(value, bool) or not (
            isinstance(value, int) or (isinstance(value, float) and math.isfinite(value) and value.is_integer())
        ):
            self.add(here, "type", "must be an integer")
            return False
        if value < minimum or (maximum is not None and value > maximum):
            msg = f"must be from {minimum} to {maximum}" if maximum is not None else f"must be at least {minimum}"
            self.add(here, "range", msg)
            return False
        return True

    def ints(self, minimum: int, maximum: int | None = None) -> _Check:
        return lambda value, here: self.integer(value, here, minimum, maximum)

    def one_of(self, values: Collection[str]) -> _Check:
        def check(value: object, here: str) -> None:
            if not isinstance(value, str):
                self.add(here, "type", "must be text")
            elif value not in values:
                self.add(here, "range", f"must be one of {', '.join(values)}")

        return check

    def flag(self, value: object, here: str) -> None:
        if not isinstance(value, bool):
            self.add(here, "type", "must be true or false")

    def text(self, value: object, here: str) -> None:
        if not isinstance(value, str):
            self.add(here, "type", "must be text")
        elif not value:
            self.add(here, "pattern", "must not be empty")

    def decimal(self, rule: _DecRule | None = None) -> _Check:
        def check(value: object, here: str) -> None:
            if not isinstance(value, str):
                self.add(here, "type", "must be a decimal string")
                return
            try:
                parsed = parse_dec(value)
            except ValueError:
                self.add(here, "pattern", "must be a decimal string with at most 18 places")
                return
            self.decimals.append((here, parsed))
            if rule is not None and not rule[0](parsed):
                self.add(here, "range", rule[1])

        return check

    def instrument(self, value: object, here: str) -> Instrument | None:
        if not isinstance(value, str):
            self.add(here, "type", 'an instrument is text, "<venue>:<symbol>"')
            return None
        entry = self.catalog.instruments.get(value)
        if entry is None:
            self.add(here, "unknown_instrument", f"{value} is not in the catalog")
            return None
        cls, quote = entry["cls"], entry["quote"]
        if not self.any_class and cls not in _LAB_CLASSES:
            self.add(here, "class_not_supported", f"class {cls} cannot be tested in the lab")
        parity = self.kind == "spread" and quote in _QUOTES and self.quote in _QUOTES
        if self.quote is not None and quote != self.quote and not parity:
            self.add(here, "mixed_quote_asset", f"{value} is quoted in {quote}, not {self.quote}")
        return entry

    def instruments(self, value: object, here: str) -> list[Instrument]:
        found: list[Instrument] = []
        if not isinstance(value, list):
            self.add(here, "type", "must be a list")
            return found
        if not value:
            self.add(here, "range", "must hold at least one instrument")
        if len(value) > 20:
            self.add(here, "too_many", "at most 20 instruments")
        for index, ref in enumerate(value):
            entry = self.instrument(ref, _at(here, index))
            if entry is not None:
                found.append(entry)
                self.universe.add(entry["cls"])
        return found

    def list_universe(self, value: object, here: str) -> list[Instrument]:
        obj = self.checked_object(value, here, ("kind", "instruments"))
        if obj is None:
            return []
        self.field(obj, "kind", here, self.one_of(("list",)))
        return self.field(obj, "instruments", here, self.instruments) or []

    def lag(self, obj: _Object, here: str) -> None:
        if "lag" in obj:
            self.integer(obj["lag"], _at(here, "lag"), 0, 100)

    def operand_instrument(self, obj: _Object, here: str) -> Instrument | Literal["self"] | None:
        if "instrument" not in obj or obj["instrument"] == "self":
            return "self"
        return self.instrument(obj["instrument"], _at(here, "instrument"))

    def operand(self, value: object, here: str) -> None:
        obj = _plain(value)
        if obj is None:
            self.add(here, "type", "must be an object")
            return
        variant = next((key for key in _OPERANDS if key in obj), None)
        if variant is None:
            self.add(here, "required", f"an operand has one of {', '.join(_OPERANDS)}")
            return
        where = _at(here, variant)
        if variant == "series":
            self.no_extra(obj, here, ("series", "instrument", "lag"))
            name = obj["series"]
            self.one_of(_SERIES)(name, where)
            target = self.operand_instrument(obj, here)
            self.lag(obj, here)
            spot = "spot" in self.universe if target == "self" else target is not None and target["cls"] == "spot"
            if _member(name, _SPOT_LACKS) and spot:
                self.add(where, "class_not_supported", f"{name} does not exist for spot")
        elif variant == "feature":
            self.no_extra(obj, here, ("feature", "instrument", "lag"))
            name = obj["feature"]
            if not isinstance(name, str):
                self.add(where, "type", "must be text")
            elif name not in self.catalog.features:
                self.add(where, "unknown_feature", f"{name} is not a feature")
            self.operand_instrument(obj, here)
            self.lag(obj, here)
        elif variant == "signal":
            self.no_extra(obj, here, ("signal", "lag"))
            name = obj["signal"]
            if not isinstance(name, str):
                self.add(where, "type", "must be text")
            elif name not in self.signal_names:
                self.add(where, "unknown_signal", f"{name} is not in signals")
            self.lag(obj, here)
        elif variant == "const":
            self.no_extra(obj, here, ("const",))
            self.decimal()(obj["const"], where)
        else:
            self.no_extra(obj, here, ("spread",))
            fields = self.checked_object(obj["spread"], where, ("a", "b", "of", "as"))
            if fields is None:
                return
            legs = [self.field(fields, key, where, self.instrument) for key in ("a", "b")]
            self.field(fields, "of", where, self.one_of(("close", "mark", "funding_apr")))
            self.field(fields, "as", where, self.one_of(("diff", "gap_bp")))
            if fields.get("of") == "funding_apr" and any(leg is not None and leg["cls"] == "spot" for leg in legs):
                self.add(_at(where, "of"), "class_not_supported", "funding_apr does not exist for spot")

    def _signals(self, value: object, here: str) -> None:
        obj = _plain(value)
        if obj is None:
            self.add(here, "type", "must be an object")
            return
        keys = _keys(obj)
        if len(keys) > 32:
            self.add(here, "too_many", "at most 32 signals")
        next_signal: dict[str, str] = {}
        for key in keys:
            where = _at(here, key)
            if _SIGNAL_NAME.fullmatch(key) is None:
                self.add(where, "pattern", "a signal name matches ^[a-z][a-z0-9_]{0,31}$")
            signal = _plain(obj[key])
            if signal is None:
                self.add(where, "type", "must be an object")
                continue
            fn = signal.get("fn")
            band = fn in ("bb_upper", "bb_lower")
            allowed = ("fn", "of", "period") if _member(fn, _INDICATORS) and not band else ("fn", "of", "period", "k")
            self.no_extra(signal, where, allowed)
            self.field(signal, "fn", where, self.one_of(_INDICATORS))
            self.field(signal, "of", where, self.operand)
            self.field(signal, "period", where, self.ints(2, 1000))
            if band:
                self.field(signal, "k", where, self.decimal(_BAND))
            operand = _plain(signal.get("of"))
            target = operand.get("signal") if operand is not None else None
            if isinstance(target, str) and target in self.signal_names:
                next_signal[key] = target

        # A signal has at most one outgoing edge. Walk iteratively so invalid inputs cannot exhaust recursion.
        done: set[str] = set()
        for key in next_signal:
            chain: set[str] = set()
            current: str | None = key
            while current is not None and current not in done:
                chain.add(current)
                target = next_signal.get(current)
                if target is not None and target in chain:
                    self.add(
                        _at(_at(_at(here, current), "of"), "signal"),
                        "signal_cycle",
                        f"{current} depends on itself through {target}",
                    )
                    break
                current = target
            done.update(chain)

    def signals(self, obj: _Object) -> None:
        self.field(obj, "signals", "", self._signals)

    def condition(self, value: object, here: str, depth: int = 1) -> None:
        obj = _plain(value)
        if obj is None:
            self.add(here, "type", "must be an object")
            return
        if depth > 8:
            self.add(here, "too_many", "conditions nest at most 8 deep")
            return
        variant = next((key for key in _CONDITIONS if key in obj), None)
        if variant is None:
            self.add(here, "required", f"a condition has one of {', '.join(_CONDITIONS)}")
            return
        where = _at(here, variant)
        if variant in ("all", "any"):
            self.no_extra(obj, here, (variant,))
            children = obj[variant]
            if not isinstance(children, list):
                self.add(where, "type", "must be a list")
                return
            if not children:
                self.add(where, "range", "must hold at least one condition")
            for index, child in enumerate(children):
                self.condition(child, _at(where, index), depth + 1)
        elif variant == "not":
            self.no_extra(obj, here, ("not",))
            self.condition(obj[variant], where, depth + 1)
        elif variant == "cmp":
            self.no_extra(obj, here, ("cmp", "left", "right"))
            self.one_of(_COMPARISONS)(obj[variant], where)
            self.field(obj, "left", here, self.operand)
            self.field(obj, "right", here, self.operand)
            self.comparisons += 1
            if self.comparisons == 65:
                self.add(here, "too_many", "at most 64 comparisons in a spec")
        else:
            self.no_extra(obj, here, ("held", "bars", "min"))
            self.condition(obj[variant], where, depth + 1)
            bars_ok = self.field(obj, "bars", here, self.ints(1, 1000))
            minimum_ok = self.field(obj, "min", here, self.ints(1, 1000))
            if bars_ok and minimum_ok and obj["min"] > obj["bars"]:
                self.add(_at(here, "min"), "range", "must not exceed bars")

    def exit(self, value: object, here: str) -> None:
        keys = ("takeProfit", "stopLoss", "trailingStop", "maxHoldBars", "rule")
        obj = self.checked_object(value, here, keys)
        if obj is None:
            return
        for key in keys[:3]:
            self.field(obj, key, here, self.nullable(self.decimal(_FRAC)))
        self.field(obj, "maxHoldBars", here, self.nullable(self.ints(1, 100_000)))
        self.field(obj, "rule", here, self.nullable(self.condition))
        if self.kind in ("rules", "spread", "scan") and all(key in obj and obj[key] is None for key in keys[1:]):
            self.add(here, "no_exit", "set one of stopLoss, trailingStop, maxHoldBars or rule")

    def sizing(self, value: object, here: str) -> None:
        raw = _plain(value)
        kind = raw.get("kind") if raw is not None else None
        amount = _AMOUNT_KEYS.get(kind) if isinstance(kind, str) else None
        obj = self.checked_object(value, here, ("kind", amount) if amount is not None else ("kind",))
        if obj is None:
            return
        kinds = ("fixed_notional", "equity_fraction") if self.kind == "spread" else tuple(_AMOUNT_KEYS)
        self.field(obj, "kind", here, self.one_of(kinds))
        if amount is not None and _member(kind, kinds):
            self.field(obj, amount, here, self.decimal(_FRAC if kind == "equity_fraction" else _POS))

    def execution(self, value: object, here: str, traded: list[Instrument]) -> None:
        obj = self.checked_object(value, here, ("entry", "tif", "restingExits", "leverage", "marginMode"))
        if obj is None:
            return

        def entry(value: object, where: str) -> None:
            raw = _plain(value)
            offset = raw is not None and raw.get("kind") == "limit_offset"
            fields = self.checked_object(value, where, ("kind", "offset", "ttlBars") if offset else ("kind",))
            if fields is not None:
                self.field(fields, "kind", where, self.one_of(("market", "limit_offset")))
                if offset:
                    self.field(fields, "offset", where, self.decimal(_OFFSET))
                    self.field(fields, "ttlBars", where, self.ints(1, 1000))

        def leverage(value: object, where: str) -> None:
            if not self.integer(value, where, 1):
                return
            caps = [cap for row in traded if (cap := row["maxLeverage"]) is not None]
            leverage = cast(int, value)
            if caps and leverage > min(caps):
                self.add(where, "leverage_above_max", f"the instrument allows at most {min(caps)}")
            elif leverage > 1 and "spot" in self.universe:
                self.add(where, "range", "spot takes leverage 1")

        self.field(obj, "entry", here, entry)
        self.field(obj, "tif", here, self.one_of(("gtc", "ioc", "post_only")))
        self.field(obj, "restingExits", here, self.flag)
        self.field(obj, "leverage", here, leverage)
        self.field(obj, "marginMode", here, self.one_of(_MODES))

    def long_short(self, value: object, here: str) -> None:
        obj = self.checked_object(value, here, ("long", "short"))
        if obj is None:
            return
        self.field(obj, "long", here, self.nullable(self.condition))
        self.field(obj, "short", here, self.nullable(self.condition))
        if obj.get("short") is not None and "spot" in self.universe:
            self.add(_at(here, "short"), "range", "spot has no shorts")

    def limits(self, value: object, here: str) -> None:
        obj = self.checked_object(value, here, ("budget", "maxOpenPositions", "cooldownBars", "maxTradesPerDay"))
        if obj is None:
            return
        self.field(obj, "budget", here, self.decimal(_POS))
        self.field(obj, "maxOpenPositions", here, self.ints(1, 50))
        self.field(obj, "cooldownBars", here, self.ints(0, 10_000))
        self.field(obj, "maxTradesPerDay", here, self.ints(1, 10_000))

    def _entry_exit(self, obj: _Object, traded: list[Instrument]) -> None:
        self.signals(obj)
        self.field(obj, "entry", "", self.long_short)
        self.field(obj, "exit", "", self.exit)
        self.field(obj, "sizing", "", self.sizing)
        self.field(obj, "execution", "", lambda value, here: self.execution(value, here, traded))

    def rules(self, obj: _Object) -> None:
        traded = self.field(obj, "universe", "", self.list_universe) or []
        self._entry_exit(obj, traded)

    def spread(self, obj: _Object) -> None:
        legs: list[Instrument | None] = []
        traded: list[Instrument] = []

        def check_legs(value: object, here: str) -> None:
            if not isinstance(value, list):
                self.add(here, "type", "must be a list")
                return
            if len(value) != 2:
                self.add(here, "range", "a spread has exactly two legs")
            refs: list[object] = []
            for index, leg in enumerate(value):
                where = _at(here, index)
                fields = self.checked_object(leg, where, ("instrument",))
                found = self.field(fields, "instrument", where, self.instrument) if fields is not None else None
                legs.append(found)
                refs.append(fields.get("instrument") if fields is not None else None)
                if found is not None:
                    traded.append(found)
                    self.universe.add(found["cls"])
            if len(refs) == 2 and isinstance(refs[0], str) and refs[0] == refs[1]:
                self.add(
                    _at(_at(here, 1), "instrument"),
                    "spread_same_instrument",
                    "the two legs must be different instruments",
                )

        def entry(value: object, here: str) -> None:
            fields = self.checked_object(value, here, ("open", "reverse"))
            if fields is not None:
                self.field(fields, "open", here, self.condition)
                self.field(fields, "reverse", here, self.nullable(self.condition))

        self.field(obj, "legs", "", check_legs)
        self.signals(obj)
        self.field(obj, "entry", "", entry)
        self.field(obj, "exit", "", self.exit)
        self.field(obj, "sizing", "", self.sizing)
        self.field(obj, "execution", "", lambda value, here: self.execution(value, here, traded))
        self.field(obj, "legTimeoutMs", "", self.ints(100, 60_000))
        if len(legs) > 1 and (second := legs[1]) is not None and second["cls"] == "spot":
            self.add("/legs/1/instrument", "range", "spot has no shorts")
        entry_fields = _plain(obj.get("entry"))
        if (
            legs
            and (first := legs[0]) is not None
            and first["cls"] == "spot"
            and entry_fields is not None
            and entry_fields.get("reverse") is not None
        ):
            self.add("/entry/reverse", "range", "spot has no shorts")

    def scan(self, obj: _Object) -> None:
        def cls(value: object, here: str) -> None:
            if not isinstance(value, str) or value not in _CLASSES:
                self.one_of(_CLASSES)(value, here)
            elif value not in _LAB_CLASSES:
                self.add(here, "class_not_supported", f"class {value} cannot be tested in the lab")
            else:
                self.universe.add(value)

        def universe(value: object, here: str) -> None:
            fields = self.checked_object(
                value, here, ("kind", "venue", "cls", "rankBy", "pick", "count", "rebalanceEveryBars", "minVolume24h")
            )
            if fields is None:
                return
            self.field(fields, "kind", here, self.one_of(("scan",)))
            self.field(fields, "venue", here, self.one_of(_VENUES))
            self.field(fields, "cls", here, cls)
            self.field(fields, "rankBy", here, self.operand)
            self.field(fields, "pick", here, self.one_of(("top", "bottom")))
            self.field(fields, "count", here, self.ints(1, 20))
            self.field(fields, "rebalanceEveryBars", here, self.ints(1, 10_000))
            self.field(fields, "minVolume24h", here, self.decimal(_NONNEG))

        self.field(obj, "universe", "", universe)
        self._entry_exit(obj, [])

    def schedule(self, obj: _Object) -> None:
        def schedule(value: object, here: str) -> None:
            fields = self.checked_object(value, here, ("everyBars", "side", "notional", "gate"))
            if fields is None:
                return
            self.field(fields, "everyBars", here, self.ints(1, 100_000))
            self.field(fields, "side", here, self.one_of(("buy", "sell")))
            self.field(fields, "notional", here, self.decimal(_POS))
            self.field(fields, "gate", here, self.nullable(self.condition))
            if fields.get("side") == "sell" and "spot" in self.universe:
                self.add(_at(here, "side"), "range", "spot has no shorts")

        traded = self.field(obj, "universe", "", self.list_universe) or []
        self.signals(obj)
        self.field(obj, "schedule", "", schedule)
        self.field(obj, "exit", "", self.exit)
        self.field(obj, "execution", "", lambda value, here: self.execution(value, here, traded))

    def bot(self, obj: _Object) -> None:
        def feeds(value: object, here: str) -> None:
            if not isinstance(value, list):
                self.add(here, "type", "must be a list")
                return
            for index, feed in enumerate(value):
                self.one_of(_FEEDS)(feed, _at(here, index))

        self.field(obj, "universe", "", self.list_universe)
        self.signals(obj)
        self.field(obj, "feeds", "", feeds)
        self.field(obj, "marginMode", "", self.one_of(_MODES))

    def mandate(self, obj: _Object) -> None:
        def mandate(value: object, here: str) -> None:
            fields = self.checked_object(
                value, here, ("instruments", "maxLeverage", "maxPositionNotional", "drawdownStop", "horizonDays")
            )
            if fields is None:
                return
            self.field(fields, "instruments", here, self.instruments)
            self.field(fields, "maxLeverage", here, self.ints(1))
            self.field(fields, "maxPositionNotional", here, self.decimal(_POS))
            self.field(fields, "drawdownStop", here, self.decimal(_FRAC))
            self.field(fields, "horizonDays", here, self.ints(1, 365))

        def subject(value: object, here: str) -> None:
            raw = _plain(value)
            kind = raw.get("kind") if raw is not None else None
            key = _SUBJECT_KEYS.get(kind) if isinstance(kind, str) else None
            allowed = ("kind", key) if key is not None else ("kind", *_SUBJECT_KEYS.values())
            fields = self.checked_object(value, here, allowed)
            if fields is not None:
                self.field(fields, "kind", here, self.one_of(_SUBJECT_KEYS))
                if key is not None:
                    self.field(fields, key, here, self.text)

        self.field(obj, "mandate", "", mandate)
        self.signals(obj)
        self.field(obj, "subject", "", subject)

    def quote_asset(self, value: object, here: str) -> None:
        if not isinstance(value, str):
            self.add(here, "type", "must be text")
        elif _ASSET.fullmatch(value) is None:
            self.add(here, "pattern", 'an asset is 1 to 32 characters of a-z, 0-9, ".", "_" or "-"')
        else:
            self.quote = value

    def run(self, value: object, for_live: bool) -> _Object | None:
        obj = _plain(value)
        if obj is None:
            self.add("", "type", "a spec is an object")
            return None
        if "kind" not in obj:
            self.add("/kind", "required", "kind is required")
            return None
        kind = obj["kind"]
        if not isinstance(kind, str) or kind not in _KINDS:
            self.add("/kind", "range", f"must be one of {', '.join(_KINDS)}")
            return None
        self.kind = kind
        self.no_extra(obj, "", (*_BASE, *_KINDS[kind]))
        subject = _plain(obj.get("subject"))
        self.any_class = kind == "mandate" and subject is not None and subject.get("kind") == "account_history"
        self.signal_names = set(_plain(obj.get("signals")) or ())
        self.field(obj, "schema", "", self.one_of(("strategy/v1",)))
        self.field(obj, "timeframe", "", self.one_of(_TIMEFRAMES))
        self.field(obj, "quoteAsset", "", self.quote_asset)
        if for_live:
            if obj.get("timeframe") == "1s":
                self.add("/timeframe", "timeframe_not_live", "a live or paper run needs a timeframe of 1m or more")
            if kind in ("bot", "mandate"):
                self.add("/kind", "kind_not_live", f"a {kind} spec has no live run of ours")
        self.field(obj, "limits", "", self.limits)
        checks = {
            "rules": self.rules,
            "spread": self.spread,
            "scan": self.scan,
            "schedule": self.schedule,
            "bot": self.bot,
            "mandate": self.mandate,
        }
        checks[kind](obj)
        return obj


def validate_spec(value: object, catalog: InstrumentCatalog, *, for_live: bool = False) -> ValidSpec | InvalidSpec:
    """Return a canonical, fingerprinted spec or the ordered validation issues."""
    validator = _Validation(catalog)
    obj = validator.run(value, for_live)
    if obj is None or validator.issues:
        return {"ok": False, "issues": validator.issues}
    spec = deepcopy(obj)
    for pointer, units in validator.decimals:
        keys = [part.replace("~1", "/").replace("~0", "~") for part in pointer.split("/")[1:]]
        node: Any = spec
        for key in keys[:-1]:
            node = node[int(key)] if isinstance(node, list) else node[key]
        key = keys[-1]
        if isinstance(node, list):
            node[int(key)] = format_dec(units)
        else:
            node[key] = format_dec(units)
    try:
        canonical = canonical_json(spec)
    except ValueError:
        # JSON transport cannot express non-finite or non-JSON values, but callers may pass Python data directly.
        return {"ok": False, "issues": [{"pointer": "", "code": "type", "message": "a spec must contain JSON data"}]}
    return {
        "ok": True,
        "spec": json.loads(canonical),
        "canonical": canonical,
        "sha256": hashlib.sha256(canonical.encode()).digest(),
    }
