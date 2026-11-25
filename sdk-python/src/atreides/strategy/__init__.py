"""Shared strategy validation, closed-bar decisions, and exact sizing."""

from ._amounts import InstrumentSteps, dec_sqrt, entry_size, exit_prices, liquidation_price
from ._engine import ENGINE_VERSION, CompiledStrategy, EngineState, StepOutput, compile, init_state, step
from ._validate import Instrument, InstrumentCatalog, InvalidSpec, SpecIssue, ValidSpec, validate_spec
from .runner import DirectVenue, Outcome, Runner, runner

__all__ = [
    "ENGINE_VERSION",
    "CompiledStrategy",
    "DirectVenue",
    "EngineState",
    "Instrument",
    "InstrumentCatalog",
    "InstrumentSteps",
    "InvalidSpec",
    "Outcome",
    "Runner",
    "SpecIssue",
    "StepOutput",
    "ValidSpec",
    "compile",
    "dec_sqrt",
    "entry_size",
    "exit_prices",
    "init_state",
    "liquidation_price",
    "runner",
    "step",
    "validate_spec",
]
