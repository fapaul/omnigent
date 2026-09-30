"""Migration ``mm1a2b3c4d5e`` adds ``ix_agents_kind_owner_name``.

The new-session picker lists "operator templates plus mine" on every open, so
this index must exist at head with ``kind`` leading (skipping the
one-per-session agent rows), and the downgrade must remove it.
"""

from __future__ import annotations

from pathlib import Path

import sqlalchemy as sa
from alembic import command

from omnigent.db.utils import _build_alembic_config, clear_engine_cache, get_or_create_engine

_INDEX = "ix_agents_kind_owner_name"


def _agent_indexes(engine: sa.Engine) -> dict[str, list[str]]:
    return {i["name"]: i["column_names"] for i in sa.inspect(engine).get_indexes("agents")}


def test_index_at_head(tmp_path: Path) -> None:
    engine = get_or_create_engine(f"sqlite:///{tmp_path / 'test.db'}")
    try:
        assert _agent_indexes(engine)[_INDEX] == ["workspace_id", "kind", "created_by", "name"]
    finally:
        clear_engine_cache()


def test_downgrade_drops_index(tmp_path: Path) -> None:
    uri = f"sqlite:///{tmp_path / 'roundtrip.db'}"
    cfg = _build_alembic_config(uri)
    engine = sa.create_engine(uri)
    try:
        with engine.begin() as conn:
            cfg.attributes["connection"] = conn
            command.upgrade(cfg, "mm1a2b3c4d5e")
        with engine.begin() as conn:
            cfg.attributes["connection"] = conn
            command.downgrade(cfg, "ll1a2b3c4d5e")
        assert _INDEX not in _agent_indexes(engine)
    finally:
        engine.dispose()
        clear_engine_cache()
