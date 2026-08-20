"""Keep the sanitized request body of a call the gateway rejected.

A provider that answers nothing but "General error" leaves the request as the only
place the cause can be — a field one level too deep, an enum value it does not accept.
Without it, diagnosing a rejection means guessing at the payload from outside, which is
slow and gets it wrong. Stored only for failed calls, and only after the same sanitizer
that guards every other stored string: card numbers truncated to a last four, CVVs
dropped by name, secrets redacted.

Nullable with no default, so it costs nothing on existing rows.

Revision ID: c1a5f2d34e88
Revises: e70cbcbf8065
Create Date: 2026-08-20 11:30:00.000000
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = 'c1a5f2d34e88'
down_revision: Union[str, None] = 'e70cbcbf8065'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("api_measurements", schema=None) as batch_op:
        batch_op.add_column(sa.Column("request_snippet", sa.Text(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("api_measurements", schema=None) as batch_op:
        batch_op.drop_column("request_snippet")
