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
#: The largest amount NUMERIC(14,2) holds; anything bigger is refused before it reaches the DB.
MAX_AMOUNT = Decimal("999999999999.99")

_INPUT_RE = re.compile(r"^(?P<sign>[-+]?)(?P<digits>\d+(?:\.\d*)?|\.\d+)$")


def to_money(value: Decimal | int | str) -> Decimal:
    """Convert to a 2-place ``Decimal`` (half-up rounding).

    Accepts ``Decimal`` (e.g. Plaid JSON parsed with ``parse_float=Decimal``), ``int`` (JSON
    integers such as ``"limit": 5000``) and numeric strings. ``float`` is refused with
    ``TypeError`` because binary floats cannot represent cents exactly. ``ValueError`` for text,
    NaN/infinity and amounts beyond ±:data:`MAX_AMOUNT` (what ``NUMERIC(14,2)`` holds).
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
    if abs(number) > MAX_AMOUNT + Decimal("0.005"):
        raise ValueError(f"amount out of range: {value!r}")
    try:
        result = number.quantize(CENT, rounding=ROUND_HALF_UP)
    except InvalidOperation as exc:  # pragma: no cover - excluded by the range check
        raise ValueError(f"amount out of range: {value!r}") from exc
    if abs(result) > MAX_AMOUNT:
        raise ValueError(f"amount out of range: {value!r}")
    return result


def to_money_or_none(value: Decimal | int | str | None) -> Decimal | None:
    """Like :func:`to_money` but passes ``None`` through (for nullable Plaid fields)."""
    return None if value is None else to_money(value)


def parse_money_input(text: str) -> Decimal:
    """Parse an amount typed by the user, e.g. ``"$1,234.50"``, ``"-12"``, ``"(4.20)"``.

    Accepts an optional sign (``-``, ``+`` or the typographic minus), a ``$``, thousands
    separators and accounting-style parentheses. Raises ``ValueError`` with a friendly message for
    anything else, including more than two decimal places and amounts of a trillion dollars or
    more (beyond what the database stores).
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
    if number > MAX_AMOUNT:
        raise ValueError("Amounts must be under $1,000,000,000,000.")
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
    """True when ``value`` is a finite Decimal with at most two decimal places that fits in
    ``NUMERIC(14,2)``."""
    if not isinstance(value, Decimal) or not value.is_finite() or abs(value) > MAX_AMOUNT:
        return False
    return value == value.quantize(CENT, rounding=ROUND_HALF_UP)
