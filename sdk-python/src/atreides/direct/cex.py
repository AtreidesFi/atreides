"""Shared pieces of the CEX direct modules (06 section 8): the tag and the client-id prefix."""

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from re import Pattern
from typing import Any


@dataclass(frozen=True)
class CexTag:
    """Our program code for one venue, from config. An empty value means the program is not live."""

    kind: str
    name: str
    value: str


def prefix_client_id(value: Any, prefix: str, max_len: int, pattern: Pattern[str]) -> Any:
    """Prefix the bot's own id, truncating the id to fit. Ids the venue would refuse anyway are left to the venue."""
    if not isinstance(value, str) or value == "" or pattern.fullmatch(value) is None:
        return value
    if value.startswith(prefix):
        return value
    return prefix + value[: max_len - len(prefix)]


def with_header(
    headers: Mapping[str, str] | None,
    name: str,
    value: str,
    aliases: Sequence[str] = (),
    on_replace: Callable[[], None] | None = None,
) -> dict[str, str]:
    """Set one header, dropping any spelling of `name` and `aliases`; `on_replace` runs when a bot value differs."""
    drop = {key.lower() for key in (name, *aliases)}
    out: dict[str, str] = {}
    foreign = False
    for key, held in (headers or {}).items():
        if key.lower() not in drop:
            out[key] = held
        elif held not in ("", value):
            foreign = True
    if foreign and on_replace is not None:
        on_replace()
    out[name] = value
    return out
