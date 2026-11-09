"""The "Show income" switch (wave 3, INC-2): ``app_settings.show_income``.

Revision ID: 0006_income_preference
Revises: 0003_f1_verification_fixes (the integrator chains the wave-3 migrations 0004 → 0005 →
0006, so this one may end up after 0005_bank_categories)
Create Date: 2026-11-09

The Dashboard and Trends show income and net next to spending while the switch is on; it starts
on. Adding a ``NOT NULL`` column with a constant default fills the existing single settings row
with ``true`` without rewriting anything else, so a live database keeps every setting the owner
chose. The downgrade drops the column (the choice is a view preference, nothing else reads it).

Once applied to production, a migration is never edited: add a new one instead.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0006_income_preference"
down_revision: str | None = "0003_f1_verification_fixes"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "app_settings",
        sa.Column("show_income", sa.Boolean(), server_default=sa.text("true"), nullable=False),
    )


def downgrade() -> None:
    op.drop_column("app_settings", "show_income")
