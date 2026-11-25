"""Exact decimal values shared with the generated API models."""

from decimal import Decimal

from pydantic import TypeAdapter

from .models import Dec

__all__ = ["Dec", "format_dec", "parse_dec"]
_DECIMAL = TypeAdapter[Decimal](Dec)


def parse_dec(value: str | int | Decimal) -> Decimal:
    return _DECIMAL.validate_python(value)


def format_dec(value: str | int | Decimal) -> str:
    parsed = parse_dec(value)
    if parsed.is_zero():
        return "0"
    text = format(parsed, "f")
    return text.rstrip("0").rstrip(".") if "." in text else text
