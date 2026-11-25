"""Closed-bar operands, conditions and indicator continuation."""

import math
from dataclasses import dataclass
from typing import Any, TypedDict, cast

from ._canonical import utf16

JsonMap = dict[str, Any]  # The engine uses the checked JSON wire shape.
BAR_FIELDS = frozenset(("open", "high", "low", "close", "volume"))
INPUT_MAX_AGE_MS = 60_000


class Read(TypedDict):
    value: float | None
    staleInputs: list[str]


class Average(TypedDict):
    count: int
    seed: float
    value: float | None


class Memory(TypedDict):
    first: Average
    second: Average | None


class Frame(TypedDict):
    bars: dict[str, JsonMap]
    signals: dict[str, dict[str, Read]]


@dataclass(frozen=True)
class Context:
    instrument: str
    frames: list[Frame | None]

    def bar(self, ref: str, index: int) -> JsonMap | None:
        frame = self.frames[index] if 0 <= index < len(self.frames) else None
        return frame["bars"].get(ref) if frame is not None else None

    def signal(self, name: str, index: int) -> Read | None:
        frame = self.frames[index] if 0 <= index < len(self.frames) else None
        return frame["signals"].get(self.instrument, {}).get(name) if frame is not None else None


def finite(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (float, int)):
        return None
    try:
        number = float(value)
    except OverflowError:
        return None
    return number if math.isfinite(number) else None


def _result(value: object, label: str) -> Read:
    number = finite(value)
    return {"value": number, "staleInputs": [label] if number is None else []}


def _at(kind: str, name: str, instrument: str, index: int, ctx: Context) -> Read:
    label = f"{instrument}:{kind}:{name}"
    bar = ctx.bar(instrument, index)
    if bar is None:
        return _result(None, label)
    if kind == "series" and name in BAR_FIELDS:
        return _result(bar[name], label)
    age = finite(bar["inputAgeMs"].get(name))
    if age is None or not 0 <= age <= INPUT_MAX_AGE_MS:
        return _result(None, label)
    return _result(bar[kind if kind == "series" else "features"].get(name), label)


def _instrument(operand: JsonMap, default: str) -> str:
    instrument = cast(str, operand.get("instrument", "self"))
    return default if instrument == "self" else instrument


def read_operand(operand: JsonMap, index: int, ctx: Context) -> Read:
    if "const" in operand:
        return _result(float(operand["const"]), "const")
    cursor = index - operand.get("lag", 0)
    if "signal" in operand:
        name = operand["signal"]
        signal = ctx.signal(name, cursor)
        if signal is None or (signal["value"] is None and not signal["staleInputs"]):
            return _result(None, f"signal:{name}")
        return signal
    if "spread" in operand:
        fields = operand["spread"]
        a, b, of = fields["a"], fields["b"], fields["of"]
        left, right = _at("series", of, a, index, ctx), _at("series", of, b, index, ctx)
        x, y = left["value"], right["value"]
        if x is None or y is None:
            return {"value": None, "staleInputs": left["staleInputs"] + right["staleInputs"]}
        delta = x - y
        value = delta if fields["as"] == "diff" else (delta / y) * 10_000 if y else None
        return _result(value, f"spread:{a}:{b}:{of}")
    kind = "series" if "series" in operand else "feature"
    return _at(kind, operand[kind], _instrument(operand, ctx.instrument), cursor, ctx)


def evaluate_condition(rule: JsonMap | None, index: int, ctx: Context) -> tuple[bool, list[str]]:
    if rule is None:
        return False, []
    stale: dict[str, None] = {}

    def read(operand: JsonMap, bar: int) -> float | None:
        found = read_operand(operand, bar, ctx)
        stale.update(dict.fromkeys(found["staleInputs"]))
        return found["value"]

    def evaluate(node: JsonMap, bar: int) -> bool:
        if ctx.bar(ctx.instrument, bar) is None:
            stale[f"{ctx.instrument}:bar"] = None
            return False
        if "all" in node:
            return all([evaluate(child, bar) for child in node["all"]])
        if "any" in node:
            return any([evaluate(child, bar) for child in node["any"]])
        if "not" in node:
            return not evaluate(node["not"], bar)
        if "held" in node:
            count = sum(evaluate(node["held"], bar - offset) for offset in range(node["bars"]))
            return count >= cast(int, node["min"])
        left, right = read(node["left"], bar), read(node["right"], bar)
        if left is None or right is None:
            return False
        match node["cmp"]:
            case "gt":
                return left > right
            case "gte":
                return left >= right
            case "lt":
                return left < right
            case "lte":
                return left <= right
        previous_left, previous_right = read(node["left"], bar - 1), read(node["right"], bar - 1)
        if previous_left is None or previous_right is None:
            return False
        if node["cmp"] == "crosses_above":
            return previous_left <= previous_right and left > right
        return previous_left >= previous_right and left < right

    matched = evaluate(rule, index)
    return matched and not stale, list(stale)


def _average(previous: Average | None, value: float | None, period: int, weight: float) -> Average:
    empty: Average = {"count": 0, "seed": 0, "value": None}
    if value is None:
        return empty
    before = previous or empty
    if before["count"] < period:
        count, seed = before["count"] + 1, before["seed"] + value
        return (
            {"count": count, "seed": seed, "value": seed / period if count == period else None}
            if math.isfinite(seed)
            else empty
        )
    prior = before["value"]
    new = finite(prior + (value - prior) * weight) if prior is not None else None
    return {**before, "value": new} if new is not None else empty


def _sample(operand: JsonMap, index: int, ctx: Context) -> tuple[Read, JsonMap | None]:
    ref = _instrument(operand, ctx.instrument)
    return read_operand(operand, index, ctx), ctx.bar(ref, index - operand.get("lag", 0))


def _sum(values: list[float]) -> float:
    # Deliberately fold left: Python 3.12+ sum() compensates floats, unlike the TS engine.
    acc = 0.0
    for value in values:
        acc += value
    return acc


def _window(fn: str, values: list[float], bars: list[JsonMap | None], band: float) -> float | None:
    first, last = values[0], values[-1]
    if fn == "highest":
        return max(values)
    if fn == "lowest":
        return min(values)
    if fn == "roc":
        return (last / first - 1) * 100 if first else None
    if fn == "vwap":
        product = volume = 0.0
        for value, bar in zip(values, bars, strict=True):
            count = finite(bar["volume"]) if bar is not None else None
            if count is None or count < 0:
                return None
            product += value * count
            volume += count
        return product / volume if volume else None
    mean = _sum(values) / len(values)
    if fn == "sma":
        return mean
    deviation = math.sqrt(_sum([(value - mean) * (value - mean) for value in values]) / len(values))
    if fn == "stddev":
        return deviation
    if fn == "zscore":
        return (last - mean) / deviation if deviation else None
    return mean + band * deviation if fn == "bb_upper" else mean - band * deviation


def _compute(definition: JsonMap, index: int, ctx: Context, previous: Memory | None) -> tuple[Read, Memory | None]:
    fn, operand, period = definition["fn"], definition["of"], definition["period"]
    current, bar = _sample(operand, index, ctx)
    value = current["value"]
    first = previous["first"] if previous is not None else None
    if fn == "ema":
        average = _average(first, value, period, 2 / (period + 1))
        return {"value": average["value"], "staleInputs": current["staleInputs"]}, {"first": average, "second": None}
    if fn in ("atr", "rsi"):
        earlier = read_operand(operand, index - 1, ctx)
        prior = earlier["value"]
        stale = sorted(set(current["staleInputs"] + earlier["staleInputs"]), key=utf16)
        if fn == "atr":
            high = finite(bar["high"]) if bar else None
            low = finite(bar["low"]) if bar else None
            span = (
                finite(max(high - low, abs(high - prior), abs(low - prior)))
                if high is not None and low is not None and high >= low and prior is not None and value is not None
                else None
            )
            average = _average(first, span, period, 1 / period)
            return {"value": average["value"], "staleInputs": stale}, {"first": average, "second": None}
        delta = finite(value - prior) if value is not None and prior is not None else None
        gain = _average(first, max(delta, 0) if delta is not None else None, period, 1 / period)
        loss = _average(
            previous["second"] if previous else None,
            max(-delta, 0) if delta is not None else None,
            period,
            1 / period,
        )
        g, loss_value = gain["value"], loss["value"]
        total = g + loss_value if g is not None and loss_value is not None else None
        rsi = (50 if total == 0 else finite(g / total * 100)) if total is not None and g is not None else None
        return {"value": rsi, "staleInputs": stale}, {"first": gain, "second": loss}

    width = period + 1 if fn == "roc" else period
    samples = [_sample(operand, bar_index, ctx) for bar_index in range(index - width + 1, index + 1)]
    stale = sorted({label for result, _ in samples for label in result["staleInputs"]}, key=utf16)
    values = [result["value"] for result, _ in samples]
    result = None
    if all(value is not None for value in values):
        result = finite(_window(fn, cast(list[float], values), [b for _, b in samples], float(definition.get("k", 2))))
    return {"value": result, "staleInputs": stale}, None


def fill_signals(
    instruments: list[str],
    ordered: list[tuple[str, JsonMap, int]],
    frames: list[Frame | None],
    frame: Frame,
    prior: dict[str, dict[str, Memory]],
) -> dict[str, dict[str, Memory]]:
    smoothing: dict[str, dict[str, Memory]] = {}
    for instrument in instruments:
        signals: dict[str, Read] = {}
        memories: dict[str, Memory] = {}
        frame["signals"][instrument] = signals
        ctx = Context(instrument, frames)
        for name, definition, _ in ordered:
            result, memory = _compute(definition, len(frames) - 1, ctx, prior.get(instrument, {}).get(name))
            signals[name] = result
            if memory is not None:
                memories[name] = memory
        if memories:
            smoothing[instrument] = memories
    return smoothing
