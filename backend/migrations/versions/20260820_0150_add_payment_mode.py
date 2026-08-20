"""Record the Direct API payment mode on transactions and runs.

A stored-credential payment is a different flow from a one-off card payment — Geidea
charges a stored token in two calls where a fresh card takes four — so the mode is
recorded per transaction and every comparison groups by it. Averaging the two together
would describe neither.

Revision ID: e70cbcbf8065
Revises: ab4d07a46a2e
Create Date: 2026-08-20 01:50:30.450542
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = 'e70cbcbf8065'
down_revision: Union[str, None] = 'ab4d07a46a2e'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # A server default is required here, not optional: these tables already hold rows
    # in any deployment worth migrating, and adding a NOT NULL column without one
    # fails outright. Every existing transaction was a standard card payment, which is
    # exactly what the default backfills.
    for table in ("transactions", "benchmark_runs"):
        with op.batch_alter_table(table, schema=None) as batch_op:
            batch_op.add_column(sa.Column("payment_mode", sa.String(length=20),
                                          nullable=False, server_default="standard"))

    # The default has done its job. Dropping it leaves the application as the single
    # source of the default, so a future change to it does not have to be made twice.
    for table in ("transactions", "benchmark_runs"):
        with op.batch_alter_table(table, schema=None) as batch_op:
            batch_op.alter_column("payment_mode", server_default=None)


def downgrade() -> None:
    for table in ("transactions", "benchmark_runs"):
        with op.batch_alter_table(table, schema=None) as batch_op:
            batch_op.drop_column("payment_mode")
