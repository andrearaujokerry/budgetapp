"""``notify-test`` — send a test Mac notification (the one-time Script Editor permission step).

macOS attributes notifications from ``osascript`` to Script Editor and drops them silently until
Script Editor is allowed to notify (ARCHITECTURE §11).
"""

from __future__ import annotations

import argparse
from typing import Any

from budgetapp.commands import add_command
from budgetapp.config import Settings
from budgetapp.services.notify import get_notifier

INSTRUCTIONS = """\
If no notification appeared:
  1. Open System Settings → Notifications → Script Editor.
     (Script Editor appears there only after it has tried to notify once, which just happened.)
  2. Turn on "Allow notifications" and choose the "Alerts" style so sync problems stay visible.
  3. Run ./run notify-test again to check.
Notifications are shown under Script Editor's name and icon; that is expected."""


def register(subparsers: Any) -> None:
    add_command(
        subparsers,
        "notify-test",
        handler=run,
        help="send a test Mac notification (one-time Script Editor permission)",
    )


def run(args: argparse.Namespace, settings: Settings) -> int:
    notifier = get_notifier(settings)
    result = notifier.notify(
        "Budget Tracker",
        "Test notification: budget and sync alerts will look like this.",
        subtitle=f"{settings.env} environment",
    )
    if not result.ok:
        print(f"Could not send the notification: {result.error}")
        return 1
    print("Sent a test notification.")
    print(INSTRUCTIONS)
    return 0
