"""Deterministic, resumable strategy decision engine over closed bars."""

import base64
import binascii
import hashlib
import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TypedDict, cast

from ._amounts import parse_dec
from ._canonical import canonical_json, utf16
from ._signals import (
    BAR_FIELDS,
    Context,
    Frame,
    JsonMap,
    Memory,
    evaluate_condition,
    fill_signals,
    finite,
    read_operand,
)

ENGINE_VERSION = "1.0.0"
FRAME_MS = {
    "1s": 1000,
    "1m": 60_000,
    "5m": 300_000,
    "15m": 900_000,
    "1h": 3_600_000,
    "4h": 14_400_000,
    "1d": 86_400_000,
}
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC).date()


class EngineState(TypedDict):
    version: str
    data: str


class Snapshot(TypedDict):
    specHash: str
    ordinal: int
    lastClose: int | None
    frames: list[Frame | None]
    smoothing: dict[str, dict[str, Memory]]
    day: str | None
    entriesToday: int
    lastEntry: dict[str, int]
    scanMembers: list[str]
    scanRebalanced: bool
    spreadPairId: str | None


class Decision(TypedDict):
    at: int
    instrument: str
    action: str
    reason: str
    pairId: str | None
    staleInputs: list[str]


class Skip(TypedDict):
    at: int
    instrument: str
    why: str


class StepOutput(TypedDict):
    state: EngineState
    decisions: list[Decision]
    skips: list[Skip]


@dataclass(frozen=True)
class CompiledStrategy:
    _spec_json: str
    sha256: bytes
    warmup_bars: int
    timeframe: str
    instruments: tuple[str, ...]

    @property
    def spec(self) -> JsonMap:
        return cast(JsonMap, json.loads(self._spec_json))


def _signal_order(spec: JsonMap) -> list[tuple[str, JsonMap, int]]:
    ordered: dict[str, tuple[str, JsonMap, int]] = {}
    visiting: set[str] = set()
    signals: dict[str, JsonMap] = spec["signals"]

    def visit(name: str) -> int:
        if name in ordered:
            return ordered[name][2]
        if name not in signals or name in visiting:
            raise ValueError("invalid strategy signal dependency")
        visiting.add(name)
        signal = signals[name]
        operand = signal["of"]
        depth = visit(operand["signal"]) if "signal" in operand else 1
        needed = depth + int(operand.get("lag", 0)) + int(signal["period"]) - 1
        if signal["fn"] in ("roc", "rsi", "atr"):
            needed += 1
        ordered[name] = (name, signal, needed)
        visiting.remove(name)
        return needed

    for name in sorted(signals, key=utf16):
        visit(name)
    return list(ordered.values())


def compile(spec: JsonMap) -> CompiledStrategy:
    """Snapshot a previously validated spec, deriving warm-up from its signal graph."""
    encoded = canonical_json(spec)
    spec = cast(JsonMap, json.loads(encoded))
    depths = {name: required for name, _, required in _signal_order(spec)}

    def operand_bars(operand: JsonMap) -> int:
        return depths.get(operand.get("signal", ""), 1) + int(operand.get("lag", 0))

    def bars(rule: JsonMap | None) -> int:
        if rule is None:
            return 1
        for compound in ("all", "any"):
            if compound in rule:
                return max([1, *[bars(child) for child in rule[compound]]])
        if "not" in rule:
            return bars(rule["not"])
        if "held" in rule:
            return bars(rule["held"]) + int(rule["bars"]) - 1
        return max(operand_bars(rule["left"]), operand_bars(rule["right"])) + (
            rule["cmp"] in ("crosses_above", "crosses_below")
        )

    warmup = [1, *depths.values()]
    kind = spec["kind"]
    instruments: list[str]
    if kind in ("rules", "scan"):
        warmup.extend(bars(spec["entry"][name]) for name in ("long", "short"))
        if kind == "scan":
            warmup.append(operand_bars(spec["universe"]["rankBy"]))
    elif kind == "spread":
        warmup.extend(bars(spec["entry"][name]) for name in ("open", "reverse"))
    elif kind == "schedule":
        warmup.append(bars(spec["schedule"]["gate"]))
    if kind in ("rules", "scan", "spread", "schedule"):
        warmup.append(bars(spec["exit"]["rule"]))
    if kind == "spread":
        instruments = [leg["instrument"] for leg in spec["legs"]]
    elif kind == "mandate":
        instruments = spec["mandate"]["instruments"]
    else:
        instruments = [] if kind == "scan" else spec["universe"]["instruments"]
    return CompiledStrategy(
        encoded,
        hashlib.sha256(encoded.encode()).digest(),
        max(warmup),
        spec["timeframe"],
        tuple(sorted(set(instruments), key=utf16)),
    )


def _encode(snapshot: Snapshot) -> EngineState:
    encoded = base64.b64encode(canonical_json(snapshot).encode()).decode("ascii")
    return {"version": ENGINE_VERSION, "data": encoded}


def init_state(compiled: CompiledStrategy) -> EngineState:
    return _encode(
        {
            "specHash": compiled.sha256.hex(),
            "ordinal": 0,
            "lastClose": None,
            "frames": [],
            "smoothing": {},
            "day": None,
            "entriesToday": 0,
            "lastEntry": {},
            "scanMembers": [],
            "scanRebalanced": False,
            "spreadPairId": None,
        }
    )


def _safe_int(value: object) -> bool:
    return type(value) is int and abs(value) <= 2**53 - 1


def _decode(compiled: CompiledStrategy, state: EngineState) -> Snapshot:
    try:
        if state["version"] != ENGINE_VERSION:
            raise ValueError("version")
        snapshot = cast(Snapshot, json.loads(base64.b64decode(state["data"], validate=True).decode("utf-8")))
        if (
            snapshot["specHash"] != compiled.sha256.hex()
            or not _safe_int(snapshot["ordinal"])
            or snapshot["ordinal"] < 0
            or (snapshot["lastClose"] is not None and not _safe_int(snapshot["lastClose"]))
            or not _safe_int(snapshot["entriesToday"])
            or snapshot["entriesToday"] < 0
            or not isinstance(snapshot["frames"], list)
            or len(snapshot["frames"]) > compiled.warmup_bars + 1
            or not isinstance(snapshot["scanMembers"], list)
            or not isinstance(snapshot["scanRebalanced"], bool)
            or not isinstance(snapshot["smoothing"], dict)
            or not isinstance(snapshot["lastEntry"], dict)
        ):
            raise ValueError("shape or spec")
        return snapshot
    except (ValueError, KeyError, TypeError, binascii.Error) as exc:
        raise ValueError("engine state belongs to a different version or strategy") from exc


def _bars(closes: dict[str, JsonMap]) -> tuple[int, dict[str, JsonMap]] | None:
    if not closes:
        return None
    at = cast(int, next(iter(closes.values()))["closeAt"])
    if not _safe_int(at) or any(bar["closeAt"] != at for bar in closes.values()):
        raise ValueError("a strategy step needs one closed-bar timestamp")
    bars: dict[str, JsonMap] = {}
    for instrument, bar in closes.items():
        fields = {name: finite(bar.get(name)) for name in BAR_FIELDS}
        if any(value is None for value in fields.values()):
            continue
        bars[instrument] = {
            **fields,
            "closeAt": at,
            "series": {name: finite(value) for name, value in bar["series"].items()},
            "features": {name: finite(value) for name, value in bar["features"].items()},
            "inputAgeMs": {
                name: age for name, value in bar["inputAgeMs"].items() if (age := finite(value)) is not None
            },
        }
    return at, bars


@dataclass
class _Run:
    compiled: CompiledStrategy
    spec: JsonMap
    snapshot: Snapshot
    at: int
    frame: Frame
    holdings: dict[str, JsonMap]
    foreign: set[str]
    open_positions: int
    decisions: list[Decision] = field(default_factory=list)
    skips: list[Skip] = field(default_factory=list)

    def context(self, instrument: str) -> Context:
        return Context(instrument, self.snapshot["frames"])

    @property
    def index(self) -> int:
        return len(self.snapshot["frames"]) - 1

    def condition(self, instrument: str, rule: JsonMap | None) -> tuple[bool, list[str]]:
        return evaluate_condition(rule, self.index, self.context(instrument))

    def skip(self, instrument: str, why: str) -> None:
        if not any(row["instrument"] == instrument and row["why"] == why for row in self.skips):
            self.skips.append({"at": self.at, "instrument": instrument, "why": why})

    def decide(self, instrument: str, action: str, reason: str, stale: list[str], pair: str | None = None) -> None:
        self.decisions.append(
            {
                "at": self.at,
                "instrument": instrument,
                "action": action,
                "reason": reason,
                "pairId": pair,
                "staleInputs": sorted(set(stale), key=utf16),
            }
        )

    def enter(
        self, entries: list[tuple[str, str]], reason: str, added: int, stale: list[str], pair: str | None = None
    ) -> bool:
        limits = self.spec["limits"]
        last = self.snapshot["lastEntry"]
        ordinal = self.snapshot["ordinal"]
        refusal: str | None = None
        if any(instrument in self.foreign for instrument, _ in entries):
            refusal = "foreign_position"
        elif any(
            instrument in last and ordinal - last[instrument] <= limits["cooldownBars"] for instrument, _ in entries
        ):
            refusal = "cooldown"
        elif added > 0 and self.open_positions + added > limits["maxOpenPositions"]:
            refusal = "max_open"
        elif self.snapshot["entriesToday"] >= limits["maxTradesPerDay"]:
            refusal = "max_trades_day"
        if refusal:
            for instrument, _ in entries:
                self.skip(instrument, refusal)
            return False
        self.open_positions += added
        self.snapshot["entriesToday"] += 1
        for instrument, action in entries:
            last[instrument] = ordinal
            self.decide(instrument, action, reason, stale, pair)
        return True

    def exit_for(self, holding: JsonMap, exit: JsonMap, ref: str | None = None) -> tuple[str | None, list[str]]:
        instrument = holding["instrument"]
        bar = self.frame["bars"].get(instrument)
        if bar is None:
            return None, [f"{instrument}:bar"]
        if exit["trailingStop"] is not None:
            fraction = float(exit["trailingStop"])
            extreme = float(holding["extremeSinceEntry"])
            price = float(bar["close"])
            hit = price <= extreme * (1 - fraction) if holding["side"] == "long" else price >= extreme * (1 + fraction)
            if hit:
                return "trailing", []
        if exit["maxHoldBars"] is not None and holding["barsHeld"] >= exit["maxHoldBars"]:
            return "time", []
        matched, stale = self.condition(ref if ref is not None else instrument, exit["rule"])
        return "rule" if matched else None, stale

    def exits(self, instruments: list[str] | tuple[str, ...], selected: set[str] | None = None) -> set[str]:
        closing: set[str] = set()
        for instrument in instruments:
            holding = self.holdings.get(instrument)
            if holding is None or instrument in self.foreign:
                continue
            if instrument not in self.frame["bars"]:
                self.skip(instrument, "input_stale")
                continue
            reason, stale = (
                ("rebalance_out", [])
                if selected is not None and instrument not in selected
                else self.exit_for(holding, self.spec["exit"])
            )
            if reason:
                closing.add(instrument)
                self.decide(instrument, "close", reason, stale)
            elif stale:
                self.skip(instrument, "input_stale")
        return closing

    def rules(self, instruments: list[str] | tuple[str, ...], selected: set[str] | None = None) -> None:
        exits = sorted(set(instruments) | self.holdings.keys(), key=utf16) if selected is not None else instruments
        self.exits(exits, selected)
        for instrument in instruments:
            if (selected is not None and instrument not in selected) or instrument in self.holdings:
                continue
            if instrument not in self.frame["bars"]:
                self.skip(instrument, "input_stale")
                continue
            long, long_stale = self.condition(instrument, self.spec["entry"]["long"])
            short, short_stale = self.condition(instrument, self.spec["entry"]["short"])
            stale = long_stale + short_stale
            if long or short:
                self.enter([(instrument, "open_long" if long else "open_short")], "entry", 1, stale)
            elif stale:
                self.skip(instrument, "input_stale")

    def due(self, every: int) -> bool:
        return (self.snapshot["ordinal"] - self.compiled.warmup_bars) % every == 0

    def scan(self, candidates: list[JsonMap] | None) -> None:
        universe = self.spec["universe"]
        if self.due(universe["rebalanceEveryBars"]) and candidates is not None:
            minimum = parse_dec(universe["minVolume24h"])
            scores: dict[str, float] = {}
            for candidate in sorted(candidates, key=lambda row: utf16(row["instrument"])):
                instrument = candidate["instrument"]
                if parse_dec(candidate["volume24h"]) < minimum:
                    continue
                score = read_operand(universe["rankBy"], self.index, self.context(instrument))["value"]
                if score is None or instrument not in self.frame["bars"]:
                    self.skip(instrument, "input_stale")
                else:
                    scores[instrument] = score
            sign = -1 if universe["pick"] == "top" else 1
            self.snapshot["scanMembers"] = sorted(scores, key=lambda ref: (sign * scores[ref], utf16(ref)))[
                : universe["count"]
            ]
            self.snapshot["scanRebalanced"] = True
        members = self.snapshot["scanMembers"]
        if self.snapshot["scanRebalanced"]:
            self.rules(members, set(members))
        else:
            self.exits(sorted(self.holdings, key=utf16))

    def spread(self) -> None:
        first, second = [leg["instrument"] for leg in self.spec["legs"]]
        instruments = (first, second)
        if any(ref not in self.frame["bars"] for ref in instruments):
            for instrument in instruments:
                self.skip(instrument, "input_stale")
            return
        holdings = [self.holdings[ref] for ref in instruments if ref in self.holdings and ref not in self.foreign]
        opened, open_stale = self.condition(first, self.spec["entry"]["open"])
        reversed_, reverse_stale = self.condition(first, self.spec["entry"]["reverse"])
        stale = open_stale + reverse_stale
        new_pair = f"pair:{self.at}:{first}:{second}"
        if holdings:
            pair = self.snapshot["spreadPairId"] or new_pair
            exits = [self.exit_for(holding, self.spec["exit"], first) for holding in holdings]
            stated = next((position for position in holdings if position["instrument"] == first), None)
            flipped = stated is not None and (reversed_ if stated["side"] == "long" else opened)
            reason = next((why for why, _ in exits if why), None) or ("rule" if flipped else None)
            stale += [label for _, labels in exits for label in labels]
            for holding in holdings:
                if reason:
                    self.decide(holding["instrument"], "close", reason, stale, pair)
                elif stale:
                    self.skip(holding["instrument"], "input_stale")
            self.snapshot["spreadPairId"] = pair
        elif opened or reversed_:
            entries = (
                [(first, "open_long"), (second, "open_short")]
                if opened
                else [(first, "open_short"), (second, "open_long")]
            )
            if self.enter(entries, "entry", 1, stale, new_pair):
                self.snapshot["spreadPairId"] = new_pair
        elif stale:
            for instrument in instruments:
                self.skip(instrument, "input_stale")

    def schedule(self) -> None:
        closing = self.exits(self.compiled.instruments)
        schedule = self.spec["schedule"]
        if not self.due(schedule["everyBars"]):
            return
        action = "buy_scheduled" if schedule["side"] == "buy" else "sell_scheduled"
        for instrument in self.compiled.instruments:
            if instrument in closing:
                continue
            if instrument not in self.frame["bars"]:
                self.skip(instrument, "input_stale")
                continue
            matched, stale = self.condition(instrument, schedule["gate"])
            if schedule["gate"] is None or matched:
                self.enter([(instrument, action)], "schedule", int(instrument not in self.holdings), stale)
            elif stale:
                self.skip(instrument, "input_stale")


def step(compiled: CompiledStrategy, state: EngineState, input: JsonMap) -> StepOutput:
    """Advance a closed-bar batch; order submission and amount sizing are separate."""
    from datetime import timedelta

    snapshot = _decode(compiled, state)
    closed = _bars(input["closes"])
    last = snapshot["lastClose"]
    if closed is None or closed[0] == last:
        return {"state": state, "decisions": [], "skips": []}
    at, bars = closed
    if last is not None and at < last:
        raise ValueError("strategy bars must advance chronologically")
    interval = FRAME_MS[compiled.timeframe]
    elapsed = interval if last is None else at - last
    gaps = elapsed // interval - 1 if elapsed % interval == 0 else 0
    width = compiled.warmup_bars + 1
    frame: Frame = {"bars": bars, "signals": {}}
    snapshot["frames"] = [*snapshot["frames"], *([None] * min(gaps, width - 1)), frame][-width:]
    snapshot["ordinal"] += gaps + 1
    snapshot["lastClose"] = at
    day = (_EPOCH + timedelta(days=at // 86_400_000)).isoformat()
    if snapshot["day"] != day:
        snapshot["day"] = day
        snapshot["entriesToday"] = 0
    spec = compiled.spec
    instruments = sorted(set(compiled.instruments) | bars.keys(), key=utf16)
    snapshot["smoothing"] = fill_signals(
        instruments, _signal_order(spec), snapshot["frames"], frame, {} if gaps else snapshot["smoothing"]
    )
    holdings = {position["instrument"]: position for position in input["holdings"]}
    kind = spec["kind"]
    opened = int(bool(holdings)) if kind == "spread" else len(holdings)
    run = _Run(compiled, spec, snapshot, at, frame, holdings, set(input["foreign"]), opened)
    if kind not in ("bot", "mandate"):
        if snapshot["ordinal"] < compiled.warmup_bars:
            for instrument in sorted(bars, key=utf16) if kind == "scan" else compiled.instruments:
                run.skip(instrument, "warmup")
        elif kind == "rules":
            run.rules(compiled.instruments)
        elif kind == "scan":
            run.scan(input["candidates"])
        elif kind == "spread":
            run.spread()
        else:
            run.schedule()
    return {"state": _encode(snapshot), "decisions": run.decisions, "skips": run.skips}
