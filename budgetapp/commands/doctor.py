"""``doctor``: check security (SEC-1..6), backups, agents, Keychain and schema; re-approve Keychain.

Prints PASS / WARN / FAIL lines with a one-line fix for each problem and exits with status 1 when
anything FAILs. It only reads; nothing is changed. Run it for the environment you care about
(``./run doctor --env production`` for the real app).

Run in Terminal, it reads every Keychain item of both environments: after a ``brew upgrade`` of
Python, macOS asks once per item; click *Always Allow* so the background agents can read them
again without a dialog. Values are never shown.
"""

from __future__ import annotations

import argparse
from typing import Any

from budgetapp.commands import add_command
from budgetapp.config import Settings
from budgetapp.services import doctor


def register(subparsers: Any) -> None:
    add_command(
        subparsers,
        "doctor",
        handler=run,
        help="check security, backups, agents, keys and schema; re-approve Keychain access",
        description=__doc__,
    )


def run(args: argparse.Namespace, settings: Settings) -> int:
    report = doctor.run_checks(doctor.DoctorContext.for_settings(settings))
    print(doctor.format_report(report))
    return 1 if report.failed else 0
