"""Exact strategy amounts at the SDK Decimal boundary."""

import math
import re
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal
from typing import Literal, TypedDict

from ..models import Dec

_SCALE = 10**18
_MAX_UNITS = 10**38 - 1
_PATTERN = re.compile(r"-?(?:0|[1-9][0-9]{0,19})(?:\.[0-9]{1,18})?")
Side = Literal["long", "short"]


class ExitPrices(TypedDict):
    tp: Dec | None
    sl: Dec | None


@dataclass(frozen=True)
class InstrumentSteps:
    tick: Dec
    lot: Dec
    min_size: Dec | None = None
    min_notional: Dec | None = None

    def __post_init__(self) -> None:
        _to_units(self.tick)
        _to_units(self.lot)
        if self.min_size is not None:
            _to_units(self.min_size)
        if self.min_notional is not None:
            _to_units(self.min_notional)


def parse_dec(text: str) -> int:
    if _PATTERN.fullmatch(text) is None:
        raise ValueError("expected a decimal string with at most 18 places")
    whole, _, fraction = text.removeprefix("-").partition(".")
    units = int(whole + fraction.ljust(18, "0"))
    return -units if text.startswith("-") else units


def format_dec(units: int) -> str:
    whole, fraction = divmod(abs(units), _SCALE)
    suffix = f"{fraction:018d}".rstrip("0")
    return ("-" if units < 0 else "") + str(whole) + (f".{suffix}" if suffix else "")


def _to_units(value: Dec) -> int:
    if not isinstance(value, Decimal):
        raise TypeError("strategy amounts require Decimal")
    exponent = value.as_tuple().exponent
    if not isinstance(exponent, int) or exponent < -18 or (value != 0 and value.adjusted() > 19):
        raise ValueError("strategy amount is outside the 20.18 decimal range")
    return parse_dec(format(value, "f"))


def _from_units(units: int) -> Dec:
    if abs(units) > _MAX_UNITS:
        raise ValueError("strategy amount is outside the 20.18 decimal range")
    return Decimal(format_dec(units))


def _leverage(value: int) -> int:
    if type(value) is not int or not 1 <= value <= 2**53 - 1:
        raise ValueError("leverage must be a positive safe integer")
    return value


def entry_size(
    sizing: Mapping[str, str], leverage: int, equity: Dec, ref_px: Dec, steps: InstrumentSteps
) -> Dec | None:
    equity_units = _to_units(equity)
    price = _to_units(ref_px)
    lot = _to_units(steps.lot)
    if price <= 0 or lot <= 0:
        raise ValueError("reference price and lot must be positive")
    match sizing["kind"]:
        case "fixed_notional":
            requested = parse_dec(sizing["notional"]) * _SCALE
        case "fixed_margin":
            requested = parse_dec(sizing["margin"]) * _leverage(leverage) * _SCALE
        case "equity_fraction":
            requested = equity_units * parse_dec(sizing["fraction"]) * _leverage(leverage)
        case _:
            raise ValueError("unknown sizing kind")
    size = requested // (price * lot) * lot
    if size <= 0 or (steps.min_size is not None and size < _to_units(steps.min_size)):
        return None
    if steps.min_notional is not None and size * price < _to_units(steps.min_notional) * _SCALE:
        return None
    return _from_units(size)


def _divide(numerator: int, denominator: int, ceil: bool) -> int:
    return -(-numerator // denominator) if ceil else numerator // denominator


def exit_prices(side: Side, entry_px: Dec, exit: Mapping[str, str | None], tick: Dec) -> ExitPrices:
    price = _to_units(entry_px)
    tick_units = _to_units(tick)
    if tick_units <= 0:
        raise ValueError("tick must be positive")

    def rounded(fraction: str | None, above: bool) -> Dec | None:
        if fraction is None:
            return None
        offset = parse_dec(fraction)
        multiplier = _SCALE + offset if above else _SCALE - offset
        return _from_units(_divide(price * multiplier, _SCALE * tick_units, not above) * tick_units)

    return {
        "tp": rounded(exit["takeProfit"], side == "long"),
        "sl": rounded(exit["stopLoss"], side == "short"),
    }


def liquidation_price(side: Side, entry_px: Dec, leverage: int, maintenance: Dec) -> Dec:
    price = _to_units(entry_px)
    maintenance_units = _to_units(maintenance)
    leverage = _leverage(leverage)
    denominator = leverage * _SCALE
    buffer = _SCALE - maintenance_units * leverage
    factor = denominator - buffer if side == "long" else denominator + buffer
    return _from_units(_divide(price * factor, denominator, factor < denominator))


def dec_sqrt(value: Dec) -> Dec:
    units = _to_units(value)
    if units < 0:
        raise ValueError("square root requires a nonnegative value")
    return _from_units(math.isqrt(units * _SCALE))
