"""Run code that must finish quickly in a fresh interpreter, with a hard time limit.

A runaway regular expression holds the GIL, so the test process couldn't stop it: in a
subprocess a regression fails the test instead of hanging the suite.
"""

from __future__ import annotations

import subprocess
import sys
import textwrap
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


def run_bounded(code: str, limit: float = 5.0) -> str | None:
    """What ``code`` prints (run from the repository), or ``None`` when it runs past ``limit`` s."""
    try:
        done = subprocess.run(
            [sys.executable, "-c", textwrap.dedent(code)],
            cwd=REPO,
            capture_output=True,
            text=True,
            timeout=limit,
            check=True,
        )
    except subprocess.TimeoutExpired:
        return None
    return done.stdout.strip()
