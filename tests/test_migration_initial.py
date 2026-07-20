"""The initial migration builds the same schema the ORM declares."""

from __future__ import annotations

from pathlib import Path

from alembic import command
from alembic.config import Config as AlembicConfig
from sqlalchemy import create_engine, inspect

REPO_ROOT = Path(__file__).resolve().parent.parent
EXPECTED_TABLES = {"settings"}
EXPECTED_SETTINGS_COLUMNS = {
    "id",
    "round_cap",
    "ado_organization",
    "ado_project",
    "ado_pat",
    "webhook_secret",
    "clone_credential_kind",
    "clone_credential_secret",
    "updated_at",
}


def _upgrade_into(db_path: Path) -> AlembicConfig:
    cfg = AlembicConfig(str(REPO_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(REPO_ROOT / "migrations"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    command.upgrade(cfg, "head")
    return cfg


def test_upgrade_creates_settings_table(tmp_path):
    db = tmp_path / "agent_fight.db"
    _upgrade_into(db)
    engine = create_engine(f"sqlite:///{db}")
    try:
        tables = set(inspect(engine).get_table_names())
    finally:
        engine.dispose()
    assert EXPECTED_TABLES <= tables


def test_settings_table_has_expected_columns(tmp_path):
    db = tmp_path / "agent_fight.db"
    _upgrade_into(db)
    engine = create_engine(f"sqlite:///{db}")
    try:
        cols = {c["name"] for c in inspect(engine).get_columns("settings")}
    finally:
        engine.dispose()
    assert EXPECTED_SETTINGS_COLUMNS <= cols


def test_migration_does_not_insert_the_singleton_row(tmp_path):
    """Schema-only migration — the id=1 row is created lazily by the app."""
    db = tmp_path / "agent_fight.db"
    _upgrade_into(db)
    engine = create_engine(f"sqlite:///{db}")
    try:
        with engine.connect() as conn:
            count = conn.exec_driver_sql("SELECT COUNT(*) FROM settings").scalar()
    finally:
        engine.dispose()
    assert count == 0


def test_downgrade_drops_everything(tmp_path):
    db = tmp_path / "agent_fight.db"
    cfg = _upgrade_into(db)
    command.downgrade(cfg, "base")
    engine = create_engine(f"sqlite:///{db}")
    try:
        tables = set(inspect(engine).get_table_names())
    finally:
        engine.dispose()
    assert not (EXPECTED_TABLES & tables)
