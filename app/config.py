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

    # Docker image used for each per-review container. Must have git and the
    # harness CLIs (claude, codex) installed. See app/container.py.
    review_image: str = os.getenv("REVIEW_IMAGE", "agent-fight-review:latest")

    # Host directory holding one clone of each skills repo. Cloned/pulled by the
    # admin "sync" on /admin/skills and bind-mounted read-only into review
    # containers — see app/skills.py. The repo URLs live in DB settings.
    skills_root: str = os.getenv("SKILLS_ROOT", str(REPO_ROOT / "data" / "skills"))

    # SQLite database. Single file, no server. Override with an absolute path
    # to relocate it (e.g. onto a mounted volume).
    db_path: str = os.getenv("DB_PATH", str(REPO_ROOT / "data" / "agent_fight.db"))

    # Session signing key for the admin console cookie.
    session_secret: str = os.getenv("SESSION_SECRET", "change-me-in-production")

    # First-user seed / break-glass credentials. Operators are rows in the
    # `users` table (see app/orm.py), managed through /admin/users. On startup,
    # if that table is empty, one user is seeded from these values (see
    # app/main.py::_seed_first_user). Once any user exists these are ignored for
    # login — change your password in-app, not here.
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
