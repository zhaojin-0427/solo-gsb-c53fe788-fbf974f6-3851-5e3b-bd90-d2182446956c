"""Decimal helpers.

All quantities and emissions are handled with :class:`decimal.Decimal` end to
end; the wire format is a string so that no binary float ever corrupts a
number.  Values are quantised to 12 fractional digits (ROUND_HALF_UP) when
presented, while the canonical hash keeps the *normalised* decimal value
(``0.10`` and ``0.1`` hash identically).
"""

from __future__ import annotations

from contextlib import contextmanager
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP, localcontext

# Plenty of guard digits; only results are quantised.
PRECISION = 50
QUANT = Decimal("1e-12")
PERCENT_QUANT = Decimal("1e-6")


def to_decimal(value) -> Decimal:
    """Parse an incoming value into a finite Decimal."""
    if isinstance(value, Decimal):
        dec = value
    else:
        try:
            dec = Decimal(str(value))
        except (InvalidOperation, ValueError):
            raise InvalidOperation(f"not a decimal number: {value!r}")
    if not dec.is_finite():
        raise InvalidOperation(f"non-finite decimal number: {value!r}")
    return dec


def canonical(value: Decimal) -> str:
    """Stable textual form used inside the idempotency hash."""
    dec = value if isinstance(value, Decimal) else to_decimal(value)
    # Decimal.normalize() may produce exponents (e.g. 1E+2); format with "f"
    # so the representation is pure digits with a fixed decimal point.
    return format(dec.normalize(), "f")


def quantize(value: Decimal, quant: Decimal = QUANT) -> Decimal:
    if value == 0:
        return Decimal("0")
    return value.quantize(quant, rounding=ROUND_HALF_UP)


def quantize_str(value: Decimal, quant: Decimal = QUANT) -> str:
    return str(quantize(value, quant))


def percent_str(value: Decimal) -> str:
    return str(quantize(value, PERCENT_QUANT))


def to_jsonable(obj):
    """Recursively turn Decimals into quantised strings in response payloads."""
    if isinstance(obj, Decimal):
        return quantize_str(obj)
    if isinstance(obj, dict):
        return {key: to_jsonable(val) for key, val in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [to_jsonable(item) for item in obj]
    return obj


@contextmanager
def high_precision():
    """Context manager giving a wide decimal precision."""
    with localcontext() as ctx:
        ctx.prec = PRECISION
        yield
