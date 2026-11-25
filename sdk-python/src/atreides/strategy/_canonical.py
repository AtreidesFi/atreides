"""ECMAScript JSON number formatting for spec fingerprints and engine state."""

import json
import math
from decimal import Decimal


def utf16(value: str) -> bytes:
    return value.encode("utf-16-be", "surrogatepass")


def _quote(value: str) -> str:
    # JSON.stringify preserves Unicode, but escapes isolated surrogate code points.
    raw = json.dumps(value, ensure_ascii=False)
    return "".join(f"\\u{ord(ch):04x}" if 0xD800 <= ord(ch) <= 0xDFFF else ch for ch in raw)


def _number(value: int | float) -> str:
    try:
        number = float(value)
    except OverflowError as exc:
        raise ValueError("canonical JSON has no form for a non-finite number") from exc
    if not math.isfinite(number):
        raise ValueError("canonical JSON has no form for a non-finite number")
    if number == 0:
        return "0"
    # Python's shortest roundtrip supplies digits. JS's choice of notation is different.
    digits = Decimal(repr(number))
    if 1e-6 <= abs(number) < 1e21:
        fixed = format(digits, "f")
        return fixed.rstrip("0").rstrip(".") if "." in fixed else fixed
    significand, exponent = format(digits, "e").split("e")
    if "." in significand:
        significand = significand.rstrip("0").rstrip(".")
    return f"{significand}e{int(exponent):+d}"


def canonical_json(value: object) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, str):
        return _quote(value)
    if isinstance(value, (int, float)):
        return _number(value)
    if isinstance(value, list):
        return "[" + ",".join(map(canonical_json, value)) + "]"
    if isinstance(value, dict):
        if not all(isinstance(key, str) for key in value):
            raise ValueError("canonical JSON object keys must be text")
        return "{" + ",".join(_quote(key) + ":" + canonical_json(value[key]) for key in sorted(value, key=utf16)) + "}"
    raise ValueError("canonical JSON requires plain JSON data")
