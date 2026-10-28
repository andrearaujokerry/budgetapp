"""launchd agents and the production install — STUB until the OPS package replaces this module.

Contract (ARCHITECTURE §2 "One production code copy", §10); keep these signatures:

* :func:`upgrade_install` is the only way production code changes: refuse if the repository has
  uncommitted changes → stop the web, sync and backup agents → take a backup →
  ``pip install -r requirements.txt`` then ``pip install --no-deps <repo>`` (never editable) into
  ``settings.install_dir`` → run migrations from the new install while holding the sync lock →
  write the install stamp (git commit) → restart the agents.
* :func:`launch_production` is what ``./run live`` runs: call :func:`upgrade_install` when the
  repository's commit differs from the stamp, then open the web agent (or serve from the install
  when no web agent is installed). Returns the process exit code.
"""

from __future__ import annotations

from budgetapp.config import Settings


def upgrade_install(settings: Settings) -> None:
    """Upgrade the Application Support install from the repository (not built yet)."""
    raise NotImplementedError(
        "Upgrading the production install isn't built yet (the OPS package adds it)."
    )


def launch_production(settings: Settings) -> int:
    """Start the production app from the Application Support install (not built yet)."""
    raise NotImplementedError(
        "Starting the production app isn't built yet (the OPS package adds it)."
    )
