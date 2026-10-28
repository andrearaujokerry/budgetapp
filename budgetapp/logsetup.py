"""Logging with secret redaction (SEC-2).

Every secret read through :mod:`budgetapp.secrets` is registered here, and anything shaped like a
Plaid token (``access-sandbox-…``, ``public-production-…``, ``link-sandbox-…``) is masked, so
neither ever reaches a log file, the console or a traceback.
"""

from __future__ import annotations

import logging
import os
import re
import sys
import threading
import traceback
from pathlib import Path
from typing import Any

REDACTED = "[REDACTED]"
_TOKEN_RE = re.compile(r"\b(access|public|link)-(sandbox|production|development)-[A-Za-z0-9-]+")
_MIN_SECRET_LENGTH = 6

_lock = threading.Lock()
_secrets: set[str] = set()
_pattern: re.Pattern[str] | None = None

LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"


def register_secret(value: str | None) -> None:
    """Mask ``value`` in every log line from now on (values shorter than 6 chars are ignored)."""
    global _pattern
    if not value or len(value) < _MIN_SECRET_LENGTH:
        return
    with _lock:
        if value in _secrets:
            return
        _secrets.add(value)
        ordered = sorted(_secrets, key=len, reverse=True)
        _pattern = re.compile("|".join(re.escape(s) for s in ordered))


def clear_registered_secrets() -> None:
    """Forget registered secrets (tests only)."""
    global _pattern
    with _lock:
        _secrets.clear()
        _pattern = None


def redact(text: str) -> str:
    """Return ``text`` with registered secrets and Plaid-token-shaped strings masked."""
    if not text:
        return text
    pattern = _pattern
    if pattern is not None:
        text = pattern.sub(REDACTED, text)
    return _TOKEN_RE.sub(lambda m: f"{m.group(1)}-{m.group(2)}-{REDACTED}", text)


class RedactingFilter(logging.Filter):
    """Redacts the formatted message of every record that passes through a handler."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:  # a broken %-format must not lose the record
            message = str(record.msg)
        redacted = redact(message)
        if redacted != message or record.args:
            record.msg = redacted
            record.args = None
        return True


class RedactingFormatter(logging.Formatter):
    """Formatter whose whole output (including tracebacks) is redacted."""

    def format(self, record: logging.LogRecord) -> str:
        return redact(super().format(record))

    def formatException(self, ei: Any) -> str:
        return redact(super().formatException(ei))


def _private_file_handler(path: Path) -> logging.Handler:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if not path.exists():
        fd = os.open(path, os.O_CREAT | os.O_WRONLY | os.O_APPEND, 0o600)
        os.close(fd)
    handler = logging.FileHandler(path, encoding="utf-8")
    return handler


def make_handler(handler: logging.Handler, fmt: str = LOG_FORMAT) -> logging.Handler:
    """Attach the redacting filter and formatter to ``handler``."""
    handler.addFilter(RedactingFilter())
    handler.setFormatter(RedactingFormatter(fmt))
    return handler


def setup_logging(
    settings: Any = None,
    *,
    log_name: str | None = None,
    level: int = logging.INFO,
    console_level: int = logging.WARNING,
    console: bool = True,
) -> None:
    """Configure the root logger with redacting handlers (idempotent).

    ``log_name`` adds ``<settings.logs_dir>/<log_name>.log`` (0600, directory 0700), e.g. ``"sync"``
    for the sync agent. Console output goes to stderr at ``console_level``.
    """
    root = logging.getLogger()
    for existing in list(root.handlers):
        if getattr(existing, "_budgetapp", False):
            root.removeHandler(existing)
            existing.close()
    root.setLevel(min(level, console_level))
    if console:
        stream = make_handler(logging.StreamHandler())
        stream.setLevel(console_level)
        stream._budgetapp = True  # type: ignore[attr-defined]
        root.addHandler(stream)
    if log_name and settings is not None:
        file_handler = make_handler(
            _private_file_handler(Path(settings.logs_dir) / f"{log_name}.log")
        )
        file_handler.setLevel(level)
        file_handler._budgetapp = True  # type: ignore[attr-defined]
        root.addHandler(file_handler)
    logging.getLogger("sqlalchemy.engine").setLevel(logging.WARNING)


def install_excepthook() -> None:
    """Print uncaught exceptions with secrets redacted (CLI entry points call this)."""

    def hook(exc_type: Any, exc: BaseException, tb: Any) -> None:
        text = "".join(traceback.format_exception(exc_type, exc, tb))
        sys.stderr.write(redact(text))

    sys.excepthook = hook


def uvicorn_log_config(level: str = "info") -> dict[str, Any]:
    """A ``logging.config.dictConfig`` dict for uvicorn that redacts like the app's own logs."""
    fmt = "%(asctime)s %(levelname)s %(name)s: %(message)s"
    return {
        "version": 1,
        "disable_existing_loggers": False,
        "filters": {"redact": {"()": RedactingFilter}},
        "formatters": {"redacting": {"()": RedactingFormatter, "fmt": fmt}},
        "handlers": {
            "default": {
                "class": "logging.StreamHandler",
                "formatter": "redacting",
                "filters": ["redact"],
                "stream": "ext://sys.stderr",
            }
        },
        "loggers": {
            "uvicorn": {"handlers": ["default"], "level": level.upper(), "propagate": False},
            "uvicorn.error": {"level": level.upper()},
            "uvicorn.access": {"handlers": ["default"], "level": level.upper(), "propagate": False},
        },
    }
