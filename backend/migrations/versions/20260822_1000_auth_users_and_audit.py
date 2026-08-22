"""Usernames, account status, audit log and transaction ownership.

Specification sections 9 and 10. Four changes, each with a data step, because every
one of them lands on a database that already has rows in it:

* ``users.username`` — new, unique, and NOT NULL, so existing accounts are backfilled
  from the local part of their email address before the constraint goes on. Collisions
  (``a@x.com`` and ``a@y.com``) are broken with a numeric suffix rather than left to
  fail the unique index.
* ``users.status`` replaces ``users.is_active``. A boolean cannot express ``LOCKED``,
  which is the state brute-force protection puts an account into. True becomes
  ``ACTIVE`` and false ``INACTIVE``; nothing starts out locked.
* Roles move from ``admin``/``viewer`` to ``ADMIN``/``USER``. A ``viewer`` is not a
  ``USER`` — the new role runs payment tests, which a viewer could not — so this is a
  deliberate widening of what existing non-admin accounts may do, and it is the
  mapping section 10 asks for.
* ``audit_logs`` is created, and ``transactions`` gains the ownership columns. Both
  are additive and cost nothing on existing rows.

The downgrade is lossy and says so: ``LOCKED`` has no boolean to go back to, and
collapses to inactive.

Revision ID: 7b41c9de20a4
Revises: c1a5f2d34e88
Create Date: 2026-08-22 10:00:00.000000
"""
from __future__ import annotations

import re
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = '7b41c9de20a4'
down_revision: Union[str, None] = 'c1a5f2d34e88'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

#: Same rule the API enforces, applied to the values this migration invents.
_ALLOWED = re.compile(r"[^a-zA-Z0-9._-]")


def _username_for(email: str, taken: set) -> str:
    base = _ALLOWED.sub(".", (email or "user").split("@")[0].lower()).strip("._-") or "user"
    base = base[:60]
    if len(base) < 3:
        base = f"{base}.user"
    candidate, suffix = base, 1
    while candidate in taken:
        suffix += 1
        candidate = f"{base}{suffix}"[:64]
    taken.add(candidate)
    return candidate


def upgrade() -> None:
    connection = op.get_bind()

    # -- users.username ---------------------------------------------------- #
    with op.batch_alter_table("users", schema=None) as batch_op:
        batch_op.add_column(sa.Column("username", sa.String(length=64), nullable=True))
        batch_op.add_column(sa.Column("status", sa.String(length=20), nullable=True))
        batch_op.add_column(sa.Column("last_login_ip", sa.String(length=64), nullable=True))
        batch_op.add_column(sa.Column("failed_login_count", sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column("locked_at", sa.DateTime(timezone=True), nullable=True))
        batch_op.add_column(sa.Column("password_changed_at", sa.DateTime(timezone=True),
                                      nullable=True))
        batch_op.add_column(sa.Column("created_by_user_id", sa.String(length=32),
                                      nullable=True))

    taken: set = set()
    for row in connection.execute(sa.text("SELECT id, email FROM users ORDER BY created_at")):
        connection.execute(
            sa.text("UPDATE users SET username = :username WHERE id = :id"),
            {"username": _username_for(row[1] or "", taken), "id": row[0]})

    # An account that was switched off stays switched off; nothing starts locked.
    connection.execute(sa.text(
        "UPDATE users SET status = CASE WHEN is_active THEN 'ACTIVE' ELSE 'INACTIVE' END"))
    connection.execute(sa.text("UPDATE users SET failed_login_count = 0"))
    connection.execute(sa.text("UPDATE users SET role = 'ADMIN' WHERE lower(role) = 'admin'"))
    connection.execute(sa.text(
        "UPDATE users SET role = 'USER' WHERE lower(role) IN ('viewer', 'user')"))

    with op.batch_alter_table("users", schema=None) as batch_op:
        batch_op.alter_column("username", existing_type=sa.String(length=64),
                              nullable=False)
        batch_op.alter_column("status", existing_type=sa.String(length=20),
                              nullable=False, server_default="ACTIVE")
        batch_op.alter_column("failed_login_count", existing_type=sa.Integer(),
                              nullable=False, server_default="0")
        batch_op.create_index(batch_op.f("ix_users_username"), ["username"], unique=True)
        batch_op.create_index(batch_op.f("ix_users_status"), ["status"], unique=False)
        batch_op.create_index(batch_op.f("ix_users_created_by_user_id"),
                              ["created_by_user_id"], unique=False)
        batch_op.create_foreign_key("fk_users_created_by_user_id_users", "users",
                                    ["created_by_user_id"], ["id"], ondelete="SET NULL")
        batch_op.drop_column("is_active")

    # -- audit_logs -------------------------------------------------------- #
    op.create_table(
        "audit_logs",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("event", sa.String(length=40), nullable=False),
        sa.Column("user_id", sa.String(length=32), nullable=True),
        sa.Column("performed_by_user_id", sa.String(length=32), nullable=True),
        sa.Column("subject_label", sa.String(length=255), nullable=True),
        sa.Column("performed_by_label", sa.String(length=255), nullable=True),
        sa.Column("ip_address", sa.String(length=64), nullable=True),
        sa.Column("user_agent", sa.String(length=500), nullable=True),
        sa.Column("detail", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="SET NULL",
                                name="fk_audit_logs_user_id_users"),
        sa.ForeignKeyConstraint(["performed_by_user_id"], ["users.id"], ondelete="SET NULL",
                                name="fk_audit_logs_performed_by_user_id_users"),
        sa.PrimaryKeyConstraint("id", name="pk_audit_logs"))
    op.create_index("ix_audit_logs_event", "audit_logs", ["event"])
    op.create_index("ix_audit_logs_created_at", "audit_logs", ["created_at"])
    op.create_index("ix_audit_logs_user_id", "audit_logs", ["user_id"])
    op.create_index("ix_audit_logs_performed_by_user_id", "audit_logs",
                    ["performed_by_user_id"])
    op.create_index("ix_audit_logs_event_created", "audit_logs", ["event", "created_at"])

    # -- transaction ownership --------------------------------------------- #
    with op.batch_alter_table("transactions", schema=None) as batch_op:
        batch_op.add_column(sa.Column("created_by_user_id", sa.String(length=32),
                                      nullable=True))
        batch_op.add_column(sa.Column("requested_by_user_id", sa.String(length=32),
                                      nullable=True))
        batch_op.add_column(sa.Column("requested_operation", sa.String(length=40),
                                      nullable=True))
        batch_op.create_index(batch_op.f("ix_transactions_created_by_user_id"),
                              ["created_by_user_id"], unique=False)
        batch_op.create_index(batch_op.f("ix_transactions_requested_by_user_id"),
                              ["requested_by_user_id"], unique=False)
        batch_op.create_foreign_key("fk_transactions_created_by_user_id_users", "users",
                                    ["created_by_user_id"], ["id"], ondelete="SET NULL")
        batch_op.create_foreign_key("fk_transactions_requested_by_user_id_users", "users",
                                    ["requested_by_user_id"], ["id"], ondelete="SET NULL")


def downgrade() -> None:
    connection = op.get_bind()

    with op.batch_alter_table("transactions", schema=None) as batch_op:
        batch_op.drop_constraint("fk_transactions_requested_by_user_id_users",
                                 type_="foreignkey")
        batch_op.drop_constraint("fk_transactions_created_by_user_id_users",
                                 type_="foreignkey")
        batch_op.drop_index(batch_op.f("ix_transactions_requested_by_user_id"))
        batch_op.drop_index(batch_op.f("ix_transactions_created_by_user_id"))
        batch_op.drop_column("requested_operation")
        batch_op.drop_column("requested_by_user_id")
        batch_op.drop_column("created_by_user_id")

    op.drop_table("audit_logs")

    with op.batch_alter_table("users", schema=None) as batch_op:
        batch_op.add_column(sa.Column("is_active", sa.Boolean(), nullable=True))

    # Lossy on purpose: a locked account has no boolean to go back to, so it lands as
    # inactive, which is the safe direction.
    connection.execute(sa.text(
        "UPDATE users SET is_active = (status = 'ACTIVE')"))
    connection.execute(sa.text("UPDATE users SET role = 'admin' WHERE role = 'ADMIN'"))
    connection.execute(sa.text("UPDATE users SET role = 'viewer' WHERE role = 'USER'"))

    with op.batch_alter_table("users", schema=None) as batch_op:
        batch_op.alter_column("is_active", existing_type=sa.Boolean(), nullable=False)
        batch_op.drop_constraint("fk_users_created_by_user_id_users", type_="foreignkey")
        batch_op.drop_index(batch_op.f("ix_users_created_by_user_id"))
        batch_op.drop_index(batch_op.f("ix_users_status"))
        batch_op.drop_index(batch_op.f("ix_users_username"))
        batch_op.drop_column("created_by_user_id")
        batch_op.drop_column("password_changed_at")
        batch_op.drop_column("locked_at")
        batch_op.drop_column("failed_login_count")
        batch_op.drop_column("last_login_ip")
        batch_op.drop_column("status")
        batch_op.drop_column("username")
