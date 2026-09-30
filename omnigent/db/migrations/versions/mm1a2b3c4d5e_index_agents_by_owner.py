"""index agents by kind, owner, and name

Revision ID: mm1a2b3c4d5e
Revises: ll1a2b3c4d5e
Create Date: 2026-09-30 00:00:00.000000

Adds ``ix_agents_kind_owner_name`` on ``(workspace_id, kind, created_by,
name)``. Template agents can now be owned by a user (``omnigent agent add``),
and the new-session picker lists "global templates plus mine" on every open.
``kind`` leads so the lookup skips the session-scoped rows, which outnumber
templates by one row per session.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "mm1a2b3c4d5e"
down_revision: str | None = "ll1a2b3c4d5e"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_INDEX = "ix_agents_kind_owner_name"
_TABLE = "agents"


def upgrade() -> None:
    op.create_index(_INDEX, _TABLE, ["workspace_id", "kind", "created_by", "name"])


def downgrade() -> None:
    op.drop_index(_INDEX, table_name=_TABLE)
