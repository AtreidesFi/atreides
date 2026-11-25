"""Persistent strategy runner for the signed API and a caller-held direct venue adapter."""

import asyncio
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal, localcontext
from pathlib import Path
from typing import Annotated, Literal, Protocol, TypeVar, cast

from pydantic import BaseModel, Field, ValidationError

from .._endpoints import PlaceOrderInput, order_body
from .._http import ApiError, Refusal, unix_ms
from ..aclient import AsyncClient
from ..client import Reply
from ..models import (
    AccountPositions,
    Dec,
    InstrumentInfo,
    Instruments,
    Order,
    PositionsRead1,
    RunDetail,
    RunEventCount,
    RunHeartbeatAnswer,
    RunOrderReportCount,
    RunView,
    VersionView,
)
from ._amounts import InstrumentSteps, entry_size, exit_prices
from ._engine import ENGINE_VERSION, FRAME_MS, CompiledStrategy, Decision, compile, step
from ._runner_state import HoldingState, PendingOrder, load_journal, save_journal
from ._signals import JsonMap

StopPolicy = Literal["leave_positions", "close_positions"]
T = TypeVar("T", bound=BaseModel)
_PREFIX = re.compile(r"r[a-z2-7]{7}\Z")
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


class Candle(BaseModel):
    t: Annotated[int, Field(ge=0)]
    closed: bool
    open: Dec
    high: Dec
    low: Dec
    close: Dec
    volume: Dec


@dataclass(frozen=True, slots=True)
class Outcome:
    state: Literal["accepted", "refused", "unknown"]
    venue_order_id: str | None = None
    code: str | None = None


class DirectVenue(Protocol):
    """Bridge the caller's local venue adapter. Only its audited send and read paths belong here."""

    account_id: str

    def derive_venue_client_id(self, client_order_id: str) -> str | None: ...
    async def place(self, order: PlaceOrderInput) -> Outcome: ...
    async def lookup(self, client_order_id: str, native_client_id: str | None) -> Outcome: ...
    async def positions(self) -> Sequence[AccountPositions]: ...


def _instant(at: int) -> str:
    return (_EPOCH + timedelta(milliseconds=at)).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _data(reply: Reply, model: type[T]) -> T:
    if isinstance(reply, Refusal):
        raise ApiError(reply.message, code=reply.code, request_id=reply.request_id)
    return reply.data if isinstance(reply.data, model) else model.model_validate(reply.data)


def _id(sequence: int) -> str:
    alphabet = "0123456789abcdefghijklmnopqrstuvwxyz"
    result = ""
    while sequence:
        sequence, digit = divmod(sequence, len(alphabet))
        result = alphabet[digit] + result
    if not result or len(result) > 20:
        raise ValueError("Runner order sequence exhausted")
    return result


def _wire_bar(at: int, bar: Candle | None) -> JsonMap:
    fields = ("open", "high", "low", "close", "volume")
    return {
        "closeAt": at,
        **{name: None if bar is None else float(getattr(bar, name)) for name in fields},
        "series": {},
        "features": {},
        "inputAgeMs": {},
    }


def _gateway_reply(reply: Reply) -> Outcome:
    if isinstance(reply, Refusal):
        if reply.retryable or reply.code in {"unknown_outcome", "unknown", "unavailable"}:
            return Outcome("unknown")
        return Outcome("refused", code=reply.code)
    order = _data(reply, Order)
    if order.state in {"unknown", "pending"}:
        return Outcome("unknown")
    if order.state == "rejected":
        return Outcome("refused", code="venue_rejected")
    return Outcome("accepted", venue_order_id=order.venueOrderId)


class Runner:
    def __init__(
        self,
        client: AsyncClient,
        run_id: str,
        detail: RunDetail,
        compiled: CompiledStrategy,
        state_path: Path,
        direct: DirectVenue | None,
        now: Callable[[], int],
        on_event: Callable[[JsonMap], None],
    ) -> None:
        account = detail.run.venueAccountId
        mode = detail.run.mode.value
        if account is None or mode not in {"direct", "gateway"} or not _PREFIX.fullmatch(detail.cloidPrefix):
            raise ValueError("A supported live run and account are required")
        if (mode == "direct") != (direct is not None) or (
            direct is not None and direct.account_id != str(account.root)
        ):
            raise ValueError("The execution adapter must match the run's mode and account")
        self.mode: Literal["direct", "gateway"] = cast(Literal["direct", "gateway"], mode)
        self._client = client
        self._path = {"id": run_id}
        self._account = str(account.root)
        self._prefix = detail.cloidPrefix
        self._compiled = compiled
        self._spec = compiled.spec
        self._frame = FRAME_MS[compiled.timeframe]
        self._direct = direct
        self._state_path = state_path
        self._state = load_journal(state_path, run_id, compiled)
        self._now = now
        self._emit = on_event
        self._status = detail.run.status.value
        self._failures = 0
        self._heartbeat_ms = 15_000
        self._snapshot: RunHeartbeatAnswer | None = None
        self._stop_policy: StopPolicy | None = cast(StopPolicy | None, detail.run.stopPolicy.value)
        self._local_stopping = False
        self._stop = asyncio.Event()
        self._bars: dict[int, dict[str, Candle]] = {}
        self._bar_ready = asyncio.Event()
        self._instruments: dict[str, InstrumentInfo] = {}
        self._active = False
        self._execution = asyncio.Lock()

    def _save(self) -> None:
        save_journal(self._state_path, self._state)

    async def _detail(self) -> RunDetail:
        return _data(await self._client.request("getStrategyRun", path=self._path), RunDetail)

    async def _heartbeat(self, detail: RunDetail) -> None:
        close_at = self._state["last_close"]
        holdings = [row.model_dump(mode="json", include={"instrument", "side", "size"}) for row in detail.holdings]
        try:
            self._snapshot = _data(
                await self._client.request(
                    "heartbeatStrategyRun",
                    path=self._path,
                    body={
                        "runnerLang": "python",
                        "runnerVersion": ENGINE_VERSION,
                        "specSha256": self._compiled.sha256.hex(),
                        "state": "paused_by_runner" if self._state["opening_paused"] else "running",
                        "lastDecisionAt": None if close_at is None else _instant(close_at),
                        "holdings": holdings,
                        "note": None,
                    },
                ),
                RunHeartbeatAnswer,
            )
        except (ApiError, ValidationError) as error:
            if isinstance(error, ApiError) and error.code == "strategy_run_version_mismatch":
                raise
            self._failures += 1
            self._emit({"kind": "heartbeat_failed", "consecutive": self._failures})
            return
        self._failures = 0
        result = self._snapshot
        if result is None:
            return
        self._status = result.status.value
        self._heartbeat_ms = result.nextHeartbeatInMs
        if not self._local_stopping:
            self._stop_policy = result.stopPolicy.value if result.stopPolicy is not None else None
        if self._status in {"stopped", "failed"}:
            self._stop.set()

    async def _report_outcome(self, pending: PendingOrder, result: Outcome) -> bool:
        data: JsonMap = {"clientOrderId": pending["client_id"]}
        if result.state == "accepted":
            kind = "order_reported"
            data["venueOrderId"] = result.venue_order_id
        elif result.state == "refused":
            kind = "order_refused"
            data["code"] = result.code or "venue_rejected"
        else:
            kind = "unknown_outcome"
        if result.state != "accepted" and not pending["reduce_only"]:
            self._state["opening_paused"] = True
            self._save()
            self._emit({"kind": "paused", "reason": kind})
        try:
            accepted = _data(
                await self._client.request(
                    "reportStrategyRunEvents",
                    path=self._path,
                    body={
                        "events": [{"kind": kind, "at": _instant(pending["at"]), "data": data}],
                    },
                ),
                RunEventCount,
            ).accepted
        except (ApiError, ValidationError):
            return False
        self._emit({"kind": "order", "clientOrderId": pending["client_id"], "outcome": result.state})
        return accepted == 1 and result.state != "unknown"

    async def _reconcile(self) -> None:
        for pending in list(self._state["pending"]):
            if pending not in self._state["pending"]:
                continue
            try:
                if self._direct is None:
                    lookup = await self._client.orders.get(self._account, pending["client_id"])
                    outcome = Outcome("unknown") if isinstance(lookup, Refusal) else _gateway_reply(lookup)
                else:
                    outcome = await self._direct.lookup(pending["client_id"], pending["native_id"])
            except (ApiError, ValidationError, OSError):
                continue
            if outcome.state != "unknown" and await self._report_outcome(pending, outcome):
                self._state["pending"].remove(pending)
                self._save()

    async def _send(self, order: PlaceOrderInput, kind: str, at: int) -> None:
        async with self._execution:
            if not order.get("reduceOnly", False) and (self._failures >= 3 or self._stop_policy is not None):
                return
            await self._send_locked(order, kind, at)

    async def _send_locked(self, order: PlaceOrderInput, kind: str, at: int) -> None:
        sequence = self._state["sequence"] + 1
        client_id = self._prefix + "-" + _id(sequence)
        order["clientOrderId"] = client_id
        native = self._direct.derive_venue_client_id(client_id) if self._direct is not None else None
        self._state["sequence"] = sequence
        self._save()
        try:
            count = _data(
                await self._client.request(
                    "reportStrategyRunOrders",
                    path=self._path,
                    body={
                        "reports": [
                            {
                                "clientOrderId": client_id,
                                "venueClientId": native,
                                "intent": kind,
                                "instrument": order["instrumentId"],
                                "side": order["side"],
                                "reduceOnly": order.get("reduceOnly", False),
                                "decidedAt": _instant(at),
                            }
                        ],
                    },
                ),
                RunOrderReportCount,
            )
        except (ApiError, ValidationError):
            return
        if count.accepted + count.duplicates != 1:
            return
        pending: PendingOrder = {
            "client_id": client_id,
            "native_id": native,
            "instrument": order["instrumentId"],
            "reduce_only": order.get("reduceOnly", False),
            "at": at,
        }
        self._state["pending"].append(pending)
        self._save()
        try:
            if self._direct is None:
                order["strategyRunId"] = self._path["id"]
                result = _gateway_reply(await self._client.request("placeOrder", body=order_body(order)))
            else:
                result = await self._direct.place(order)
        except (ApiError, ValidationError, OSError):
            result = Outcome("unknown")
        if await self._report_outcome(pending, result):
            self._state["pending"].remove(pending)
            self._save()

    async def _foreign(self, detail: RunDetail) -> set[str] | None:
        if self._direct is not None:
            rows = await self._direct.positions()
        else:
            response = await self._client.positions({"accountId": self._account})
            if isinstance(response, Refusal) or not isinstance(response.data, list):
                return None
            rows = [v if isinstance(v, AccountPositions) else AccountPositions.model_validate(v) for v in response.data]
        attributed = {p.instrument.root: (p.side.value, p.size) for p in detail.holdings}
        account = next((row.read.root for row in rows if str(row.accountId.root) == self._account), None)
        if not isinstance(account, PositionsRead1):
            return None
        return {p.instrumentId for p in account.value if attributed.get(p.instrumentId) != (p.side.value, p.size)}

    def _tracked(self, detail: RunDetail, bars: dict[str, Candle]) -> list[JsonMap]:
        tracked: dict[str, HoldingState] = {}
        output = []
        for position in detail.holdings:
            instrument, side = position.instrument.root, position.side.value
            entry = position.entryPx
            prior = self._state["holdings"].get(instrument)
            if prior is None or prior["side"] != side:
                prior = {"side": side, "entry": format(entry, "f"), "bars": 0, "extreme": format(entry, "f")}
            candle = bars.get(instrument)
            extreme = Decimal(prior["extreme"])
            if candle is not None:
                extreme = max(extreme, candle.high) if side == "long" else min(extreme, candle.low)
            tracked[instrument] = {**prior, "bars": prior["bars"] + 1, "extreme": format(extreme, "f")}
            output.append(
                {
                    "instrument": instrument,
                    "side": side,
                    "size": format(position.size, "f"),
                    "entryPx": format(entry, "f"),
                    "extremeSinceEntry": format(extreme, "f"),
                    "barsHeld": tracked[instrument]["bars"],
                }
            )
        self._state["holdings"] = tracked
        return output

    def _opening(
        self,
        detail: RunDetail,
        decision: Decision,
        bar: Candle,
        instrument: InstrumentInfo,
        capacity: tuple[int, int, Decimal],
    ) -> tuple[PlaceOrderInput, Decimal] | None:
        if (
            self._status != "live"
            or detail.run.status != "live"
            or self._failures >= 3
            or self._state["opening_paused"]
        ):
            return None
        heartbeat, stats = self._snapshot, detail.run.stats
        if heartbeat is None or heartbeat.capsState.dailyLossHit or stats is None or not stats.marksFresh:
            return None
        caps = heartbeat.caps
        reference = decision["instrument"]
        allowed = (
            any(ref.root == reference for ref in caps.instruments.root)
            if isinstance(caps.instruments, Instruments)
            else caps.instruments.venue == instrument.venue and caps.instruments.cls == instrument.cls
        )
        if not allowed:
            return None
        orders, positions, notional = capacity
        if orders >= caps.maxOrdersPerDay or positions >= caps.maxOpenPositions:
            return None
        execution = self._spec["execution"]
        leverage = int(execution.get("leverage", 1))
        if leverage > caps.maxLeverage or leverage > (instrument.maxLeverage or 1):
            return None
        if decision["action"] not in {"open_long", "open_short", "buy_scheduled", "sell_scheduled"}:
            return None
        side: Literal["buy", "sell"] = "buy" if decision["action"] in {"open_long", "buy_scheduled"} else "sell"
        ref_px = bar.close
        if not ref_px.is_finite() or ref_px <= 0 or stats.equity <= 0:
            return None
        sizing = self._spec.get("sizing")
        if self._spec["kind"] == "schedule":
            sizing = {"kind": "fixed_notional", "notional": self._spec["schedule"]["notional"]}
        if not isinstance(sizing, dict):
            return None
        steps = InstrumentSteps(
            instrument.tick,
            instrument.lot,
            None if instrument.minSize is None else instrument.minSize,
            None if instrument.minNotional is None else instrument.minNotional,
        )
        size = entry_size(sizing, leverage, stats.equity, ref_px, steps)
        if size is None:
            return None
        entry = execution["entry"]
        order: PlaceOrderInput = {
            "accountId": self._account,
            "instrumentId": reference,
            "side": side,
            "type": "market",
            "size": size,
            "tif": "ioc",
            "reduceOnly": False,
        }
        with localcontext() as ctx:
            ctx.prec = 80
            order_notional = size * ref_px
            if order_notional > caps.maxOrderNotional or notional + order_notional > caps.maxOpenNotional:
                return None
            if entry["kind"] == "limit_offset":
                offset = Decimal(entry["offset"])
                price = ref_px * (1 - offset if side == "buy" else 1 + offset)
                price = (price / steps.tick).to_integral_value(
                    rounding="ROUND_FLOOR" if side == "buy" else "ROUND_CEILING"
                ) * steps.tick
                if price <= 0 or size * price > caps.maxOrderNotional or notional + size * price > caps.maxOpenNotional:
                    return None
                order.update({"type": "limit", "price": price, "tif": execution["tif"]})
                order_notional = max(order_notional, size * price)
        if execution.get("restingExits") and self._spec.get("exit") is not None:
            prefill = exit_prices("long" if side == "buy" else "short", ref_px, self._spec["exit"], steps.tick)
            if prefill["tp"] is not None:
                order["tp"] = {"triggerPrice": prefill["tp"]}
            if prefill["sl"] is not None:
                order["sl"] = {"triggerPrice": prefill["sl"]}
        return order, order_notional

    async def _bar(self, channel: str, raw: object) -> None:
        name = channel.removeprefix("candles:").removesuffix(":" + self._compiled.timeframe)
        if name not in self._compiled.instruments:
            return
        try:
            bar = Candle.model_validate(raw)
        except ValidationError:
            return
        at = bar.t + self._frame
        previous = self._state["last_close"]
        if (
            not bar.closed
            or bar.t % self._frame != 0
            or at > self._now() + 1000
            or (previous is not None and at <= previous)
        ):
            return
        if len(self._bars) >= 4 and at not in self._bars:
            del self._bars[min(self._bars)]
        self._bars.setdefault(at, {})[name] = bar
        self._bar_ready.set()

    async def _market_batch(self) -> tuple[int, dict[str, Candle]]:
        while not self._stop.is_set():
            self._bar_ready.clear()
            if self._bars:
                at = min(self._bars)
                bars = self._bars[at]
                if len(bars) == len(self._compiled.instruments) or self._now() >= at + 10_000:
                    return at, self._bars.pop(at)
                timeout = max(0.001, (at + 10_000 - self._now()) / 1000)
            else:
                timeout = 1.0
            try:
                await asyncio.wait_for(self._bar_ready.wait(), timeout=timeout)
            except TimeoutError:
                pass
        raise asyncio.CancelledError

    async def _close(self, instrument: str, side: str, size: Decimal, reason: str, at: int) -> None:
        async with self._execution:
            await self._close_locked(instrument, side, size, reason, at)

    async def _close_locked(self, instrument: str, side: str, size: Decimal, reason: str, at: int) -> None:
        if size <= 0 or any(row["instrument"] == instrument for row in self._state["pending"]):
            return
        order: PlaceOrderInput = {
            "accountId": self._account,
            "instrumentId": instrument,
            "side": "sell" if side == "long" else "buy",
            "type": "market",
            "tif": "ioc",
            "size": size,
            "reduceOnly": True,
        }
        await self._send_locked(order, reason, at)

    async def _batch(self, at: int, bars: dict[str, Candle], detail: RunDetail) -> None:
        last = self._state["last_close"]
        if last is not None and at <= last:
            return
        try:
            foreign = await self._foreign(detail)
        except (ApiError, ValidationError, OSError):
            foreign = None
        tracked = self._tracked(detail, bars)
        # An unavailable account read disables openings. Existing attributed positions still get exits.
        engine_foreign = (
            set(self._compiled.instruments) - {p["instrument"] for p in tracked} if foreign is None else foreign
        )
        closes = {instrument: _wire_bar(at, bars.get(instrument)) for instrument in self._compiled.instruments}
        output = step(
            self._compiled,
            self._state["engine"],
            {"closes": closes, "holdings": tracked, "foreign": sorted(engine_foreign), "candidates": []},
        )
        self._state["engine"] = output["state"]
        self._state["last_close"] = at
        self._save()
        mode = self._stop_policy
        if mode is not None:
            return
        snapshot = self._snapshot
        stats = detail.run.stats
        orders = 0 if snapshot is None else snapshot.capsState.ordersToday
        notional = Decimal(0) if snapshot is None else snapshot.capsState.openNotional
        positions = len(tracked) if stats is None else max(len(tracked), stats.openPositions)
        own = {p.instrument.root: p for p in detail.holdings}
        exits = {"tp": "exit_tp", "sl": "exit_sl", "time": "exit_time", "rule": "exit_rule"}
        for decision in output["decisions"]:
            instrument = decision["instrument"]
            self._emit({"kind": "decision", "at": at, "instrument": instrument, "action": decision["action"]})
            if decision["action"] == "close":
                holding = own.get(instrument)
                if holding is not None:
                    await self._close(
                        instrument, holding.side.value, holding.size, exits.get(decision["reason"], "close"), at
                    )
                continue
            if (
                foreign is None
                or instrument in foreign
                or any(p["instrument"] == instrument for p in self._state["pending"])
            ):
                continue
            bar = bars.get(instrument)
            if bar is None:
                continue
            prepared = self._opening(
                detail, decision, bar, self._instruments[instrument], (orders, positions, notional)
            )
            if prepared is None:
                continue
            order, added = prepared
            await self._send(order, "add" if instrument in own else "open", at)
            orders += 1
            positions += int(instrument not in own)
            notional += added

    async def _pulse(self) -> None:
        try:
            detail = await self._detail()
        except (ApiError, ValidationError):
            self._failures += 1
            self._emit({"kind": "heartbeat_failed", "consecutive": self._failures})
            return
        await self._heartbeat(detail)
        async with self._execution:
            await self._reconcile()
            if self._stop_policy == "close_positions" and not self._stop.is_set():
                for position in detail.holdings:
                    await self._close_locked(
                        position.instrument.root, position.side.value, position.size, "stop_close", self._now()
                    )

    async def _heartbeats(self) -> None:
        while not self._stop.is_set():
            try:
                await asyncio.wait_for(self._stop.wait(), self._heartbeat_ms / 1000)
            except TimeoutError:
                await self._pulse()

    async def start(self) -> None:
        """Run until the server closes the run or the caller cancels this coroutine."""
        if self._active:
            raise RuntimeError("This strategy runner is already active")
        self._active = True
        stream = None
        tasks: list[asyncio.Task[None]] = []
        try:
            for name in self._compiled.instruments:
                self._instruments[name] = _data(await self._client.instruments.get(name), InstrumentInfo)
            await self._pulse()
            if self._stop.is_set():
                return
            stream = self._client.stream(
                [f"candles:{name}:{self._compiled.timeframe}" for name in self._instruments],
                on_event=lambda _: None,
                on_reset=lambda _: None,
                on_market=self._bar,
            )

            async def decisions() -> None:
                while not self._stop.is_set():
                    at, bars = await self._market_batch()
                    try:
                        detail = await self._detail()
                    except (ApiError, ValidationError):
                        continue
                    await self._batch(at, bars, detail)

            async def stopped() -> None:
                await self._stop.wait()

            tasks = [asyncio.create_task(job()) for job in (self._heartbeats, decisions, stream.wait, stopped)]
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                if not task.cancelled():
                    task.result()
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            if stream is not None:
                await stream.close()
            self._active = False

    async def stop(self, policy: StopPolicy) -> None:
        result = _data(await self._client.request("stopStrategyRun", path=self._path, body={"policy": policy}), RunView)
        self._local_stopping = True
        self._stop_policy = policy
        self._status = result.status.value
        self._emit({"kind": "stopped", "policy": policy})
        if self._status == "stopped" or policy == "leave_positions":
            self._stop.set()


async def runner(
    *,
    client: AsyncClient,
    run_id: str,
    state_path: str | Path,
    direct: DirectVenue | None = None,
    now: Callable[[], int] = unix_ms,
    on_event: Callable[[JsonMap], None] = lambda _: None,
) -> Runner:
    """Fetch and verify the immutable strategy before any local state or venue request is touched."""
    detail = _data(await client.request("getStrategyRun", path={"id": run_id}), RunDetail)
    if str(detail.run.id.root) != run_id:
        raise ValueError("The API returned a different strategy run")
    version_id = str(detail.run.versionId.root)
    version = _data(await client.request("getStrategyVersion", path={"id": version_id}), VersionView)
    if str(version.id.root) != version_id or version.spec is None or version.engineVersion != ENGINE_VERSION:
        raise ApiError("Strategy version is unavailable", code="strategy_run_version_mismatch")
    spec = cast(JsonMap, version.spec.root)
    compiled = compile(spec)
    if compiled.sha256.hex() != version.specSha256:
        raise ApiError("Strategy version hash differs", code="strategy_run_version_mismatch")
    if spec["kind"] not in {"rules", "schedule"} or compiled.timeframe == "1s":
        raise ValueError("This runner requires a rules or schedule version with a live candle timeframe")
    return Runner(client, run_id, detail, compiled, Path(state_path), direct, now, on_event)
