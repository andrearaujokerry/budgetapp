"""Text from outside the app (forms, URLs, CSV files) made safe to store.

C0 control characters and DEL are invisible on a page, and PostgreSQL refuses NUL in text (and in
JSON), so every service that stores user text runs it through :func:`strip_control_chars` first:
transactions (notes, tags, filters), categories (names), rules (names and patterns) and the Amex
CSV import (every text cell).
"""

from __future__ import annotations

import re
from typing import Any

#: C0 control characters and DEL, except tab and newline.
CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")


def strip_control_chars(value: Any) -> str:
    """``value`` as text without control characters (tab and newline stay; CR LF becomes LF)."""
    text = "" if value is None else str(value)
    return CONTROL_CHARS.sub("", text.replace("\r\n", "\n").replace("\r", "\n"))


def has_control_chars(text: str) -> bool:
    """True when ``text`` holds a :data:`CONTROL_CHARS` character (CR included): for values such
    as ids and References, which are refused rather than cleaned."""
    return CONTROL_CHARS.search(text) is not None
