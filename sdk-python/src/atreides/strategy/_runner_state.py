"""Local runner journal. An allocated ID survives a crash before the venue call."""

import json
import os
import tempfile
from pathlib import Path
from typing import TypedDict

from ._engine import CompiledStrategy, EngineState, init_state


class PendingOrder(TypedDict):
    client_id: str
    native_id: str | None
    instrument: str
    reduce_only: bool
    at: int


class HoldingState(TypedDict):
    side: str
    entry: str
    bars: int
    extreme: str


class Journal(TypedDict):
    run_id: str
    spec_hash: str
    sequence: int
    engine: EngineState
    last_close: int | None
    opening_paused: bool
    holdings: dict[str, HoldingState]
    pending: list[PendingOrder]


def load_journal(path: Path, run_id: str, compiled: CompiledStrategy) -> Journal:
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {
            "run_id": run_id,
            "spec_hash": compiled.sha256.hex(),
            "sequence": 0,
            "engine": init_state(compiled),
            "last_close": None,
            "opening_paused": False,
            "holdings": {},
            "pending": [],
        }
    journal: Journal = json.loads(raw)
    if journal["run_id"] != run_id or journal["spec_hash"] != compiled.sha256.hex():
        raise ValueError("The saved runner state belongs to another run or strategy version")
    if type(journal["sequence"]) is not int or journal["sequence"] < 0:
        raise ValueError("Invalid runner order sequence")
    return journal


def save_journal(path: Path, journal: Journal) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as out:
        written = Path(out.name)
        try:
            json.dump(journal, out, separators=(",", ":"), allow_nan=False)
            out.flush()
            os.fsync(out.fileno())
            os.replace(written, path)
        finally:
            written.unlink(missing_ok=True)
