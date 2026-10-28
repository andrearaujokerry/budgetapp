"""Exact money helpers (REL-4). Amounts are ``Decimal`` with two places, never ``float``.

Sign convention everywhere (Plaid's, shared by Amex CSV files): positive = charge, fee or interest;
negative = credit (refund, statement credit, payment).
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

CENT = Decimal("0.01")
ZERO = Decimal("0.00")
MINUS = "−"  # typographic minus sign used for display

_INPUT_RE = re.compile(r"^(?P<sign>[-+]?)(?P<digits>\d+(?:\.\d*)?|\.\d+)$")


def to_money(value: Decimal | int | str) -> Decimal:
    """Convert to a 2-place ``Decimal`` (half-up rounding).

    Accepts ``Decimal`` (e.g. Plaid JSON parsed with ``parse_float=Decimal``), ``int`` (JSON
    integers such as ``"limit": 5000``) and numeric strings. ``float`` is refused with
    ``TypeError`` because binary floats cannot represent cents exactly.
    """
    if isinstance(value, bool):
        raise TypeError("to_money() does not accept bool")
    if isinstance(value, float):
        raise TypeError("to_money() refuses float; parse JSON with parse_float=Decimal")
    if isinstance(value, Decimal):
        number = value
    elif isinstance(value, int):
        number = Decimal(value)
    elif isinstance(value, str):
        try:
            number = Decimal(value.strip())
        except InvalidOperation as exc:
            raise ValueError(f"not a number: {value!r}") from exc
    else:
        raise TypeError(f"to_money() cannot convert {type(value).__name__}")
    if not number.is_finite():
        raise ValueError(f"not a finite amount: {value!r}")
    return number.quantize(CENT, rounding=ROUND_HALF_UP)


def to_money_or_none(value: Decimal | int | str | None) -> Decimal | None:
    """Like :func:`to_money` but passes ``None`` through (for nullable Plaid fields)."""
    return None if value is None else to_money(value)


def parse_money_input(text: str) -> Decimal:
    """Parse an amount typed by the user, e.g. ``"$1,234.50"``, ``"-12"``, ``"(4.20)"``.

    Accepts an optional sign (``-``, ``+`` or the typographic minus), a ``$``, thousands
    separators and accounting-style parentheses. Raises ``ValueError`` with a friendly message for
    anything else, including more than two decimal places.
    """
    if not isinstance(text, str):
        raise TypeError("parse_money_input() expects a string")
    cleaned = text.strip().replace(MINUS, "-").replace("$", "").replace(",", "").replace(" ", "")
    negative = False
    if cleaned.startswith("(") and cleaned.endswith(")"):
        negative = True
        cleaned = cleaned[1:-1]
    match = _INPUT_RE.match(cleaned)
    if not cleaned or match is None:
        raise ValueError(f"“{text.strip()}” is not an amount. Use a number like 25 or 1,234.56.")
    digits = match.group("digits")
    if "." in digits and len(digits.split(".", 1)[1]) > 2:
        raise ValueError("Amounts can have at most two decimal places.")
    number = Decimal(digits)
    if match.group("sign") == "-":
        negative = not negative
    return to_money(-number if negative else number)


def format_money(
    amount: Decimal | int | None,
    *,
    signed: bool = False,
    symbol: bool = True,
    none: str = "—",
) -> str:
    """Format for display: ``$1,234.56``; negatives use the typographic minus (``−$12.00``).

    ``signed=True`` adds ``+`` to positive amounts (zero is unsigned). ``None`` renders as
    ``none`` (an em dash by default).
    """
    if amount is None:
        return none
    value = to_money(amount)
    sign = ""
    if value < 0:
        sign = MINUS
    elif signed and value > 0:
        sign = "+"
    body = f"{abs(value):,.2f}"
    return f"{sign}{'$' if symbol else ''}{body}"


def sum_money(values: Iterable[Decimal | int]) -> Decimal:
    """Exact sum of amounts, quantized to cents (``ZERO`` for an empty iterable)."""
    total = ZERO
    for value in values:
        total += to_money(value)
    return to_money(total)


def is_cents(value: Decimal) -> bool:
    """True when ``value`` is a finite Decimal with at most two decimal places."""
    return (
        isinstance(value, Decimal)
        and value.is_finite()
        and value == value.quantize(CENT, rounding=ROUND_HALF_UP)
    )
