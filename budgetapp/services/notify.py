"""macOS notifications via ``osascript`` (ARCHITECTURE §11).

The title and message are passed as ``argv`` to an ``on run argv`` script, never interpolated
into AppleScript, so a merchant name can never run as code; ``--`` stops option parsing so a
message such as ``-5.00 refund`` is not read as a flag. macOS attributes these notifications to
Script Editor and drops them silently until Script Editor is allowed to notify (System Settings →
Notifications → Script Editor); ``./run notify-test`` walks the user through that once.
"""

from __future__ import annotations

import logging
import subprocess
import sys
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    from budgetapp.config import Settings

log = logging.getLogger(__name__)

OSASCRIPT = "/usr/bin/osascript"
_SCRIPT = (
    "on run argv",
    "display notification (item 2 of argv) with title (item 1 of argv) subtitle (item 3 of argv)",
    "end run",
)
MAX_TITLE = 120
MAX_MESSAGE = 500


@dataclass(frozen=True, slots=True)
class NotifyResult:
    """Whether the notification was handed to macOS (delivery itself can't be confirmed)."""

    ok: bool
    error: str | None = None


@runtime_checkable
class Notifier(Protocol):
    """Sends a user notification. Implementations never raise."""

    def notify(self, title: str, message: str, *, subtitle: str = "") -> NotifyResult: ...


def _clip(text: str, limit: int) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


class OsascriptNotifier:
    """Posts with ``/usr/bin/osascript``; returns a failed result instead of raising."""

    def __init__(self, *, timeout: float = 15.0, runner=subprocess.run) -> None:
        self.timeout = timeout
        self._runner = runner

    def command(self, title: str, message: str, subtitle: str = "") -> list[str]:
        """The exact argv used (title/message/subtitle are data, never script text)."""
        args = [OSASCRIPT]
        for line in _SCRIPT:
            args += ["-e", line]
        args += [
            "--",
            _clip(title, MAX_TITLE),
            _clip(message, MAX_MESSAGE),
            _clip(subtitle, MAX_TITLE),
        ]
        return args

    def notify(self, title: str, message: str, *, subtitle: str = "") -> NotifyResult:
        try:
            result = self._runner(
                self.command(title, message, subtitle),
                capture_output=True,
                text=True,
                timeout=self.timeout,
                check=False,
                env={"PATH": "/usr/bin:/bin"},
            )
        except subprocess.TimeoutExpired:
            return NotifyResult(False, "osascript timed out")
        except Exception as exc:  # never let a notification break the caller
            return NotifyResult(False, f"{type(exc).__name__}: {exc}")
        if result.returncode != 0:
            detail = (result.stderr or "").strip()[:200]
            return NotifyResult(False, f"osascript exited {result.returncode}: {detail}")
        return NotifyResult(True)


@dataclass
class SentNotification:
    title: str
    message: str
    subtitle: str = ""


@dataclass
class RecordingNotifier:
    """Test double: records notifications; set ``fail_with`` to simulate a failure."""

    sent: list[SentNotification] = field(default_factory=list)
    fail_with: str | None = None

    def notify(self, title: str, message: str, *, subtitle: str = "") -> NotifyResult:
        self.sent.append(SentNotification(title, message, subtitle))
        if self.fail_with:
            return NotifyResult(False, self.fail_with)
        return NotifyResult(True)

    @property
    def titles(self) -> list[str]:
        return [n.title for n in self.sent]


class NullNotifier:
    """Discards notifications (tests, and platforms without osascript)."""

    def notify(self, title: str, message: str, *, subtitle: str = "") -> NotifyResult:
        return NotifyResult(True)


def get_notifier(settings: Settings) -> Notifier:
    """The notifier for an environment: osascript on macOS, a no-op under pytest."""
    if settings.env == "test" or sys.platform != "darwin":
        return NullNotifier()
    return OsascriptNotifier()
