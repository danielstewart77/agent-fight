"""Configuration loaded from environment variables."""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

REPO_ROOT = Path(__file__).resolve().parent.parent


class Config:
    # Server
    port: int = int(os.getenv("PORT", "8890"))
    enable_logging: bool = os.getenv("ENABLE_LOGGING", "true").lower() == "true"

    # SQLite database. Single file, no server. Override with an absolute path
    # to relocate it (e.g. onto a mounted volume).
    db_path: str = os.getenv("DB_PATH", str(REPO_ROOT / "data" / "agent_fight.db"))

    # Admin console. The admin is a single operator identity held in env, not
    # a row in the database. All run settings (ADO connection, webhook
    # secret, clone credential) live in the `settings` table instead, managed
    # through the admin console — see app/admin/settings.py.
    session_secret: str = os.getenv("SESSION_SECRET", "change-me-in-production")
    admin_username: str = os.getenv("ADMIN_USERNAME", "")
    admin_password: str = os.getenv("ADMIN_PASSWORD", "")


config = Config()


def database_url(*, driver: str = "aiosqlite") -> str:
    """Build the SQLAlchemy connection URL for the SQLite database.

    `driver` selects the SQLAlchemy driver:
        - 'aiosqlite' for async (used by the app at runtime)
        - '' (empty) for sync (used by Alembic migrations)
    """
    path = Path(config.db_path)
    if not path.is_absolute():
        path = REPO_ROOT / path
    path.parent.mkdir(parents=True, exist_ok=True)
    prefix = f"sqlite+{driver}" if driver else "sqlite"
    return f"{prefix}:///{path}"
