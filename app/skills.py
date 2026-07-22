"""Agent skills — shared, read-only, host-managed (modeled on frame-main).

Each harness discovers skills under its home in the review container: Claude
reads ``~/.claude/skills``, Codex ``~/.codex/skills``. We keep one clone of each
skills repo on the host (``config.skills_root``) and bind-mount the selected
skill folders read-only into each review container, so:

  * containers stay disposable — skills are never copied in per run, and the
    agent cannot mutate the shared set;
  * the clone credential never enters a container — the host git credential
    helper authenticates the clone/pull;
  * updating skills is a ``git pull`` in one place (the admin "sync" button).

On top of frame-main's whole-repo mount, agent-fight adds a per-harness picker:
only the skill folders the operator selected (``Settings.*_skills_enabled``,
a JSON list) are mounted for that harness.
"""

from __future__ import annotations

import base64
import json
import subprocess
from pathlib import Path
from typing import Any

from app.config import config

CONTAINER_HOME = "/home/node"

# role -> (repo dir under skills_root, container skills dir, settings attr prefix)
REPOS: tuple[tuple[str, str, str, str], ...] = (
    ("claude", "claude-skills", f"{CONTAINER_HOME}/.claude/skills", "claude"),
    ("codex", "codex-skills", f"{CONTAINER_HOME}/.codex/skills", "codex"),
)

_PATH = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"


def _repo_url(settings: Any, role: str) -> str:
    return getattr(settings, f"{role}_skills_repo") or ""


def _selected(settings: Any, role: str) -> list[str]:
    raw = getattr(settings, f"{role}_skills_enabled") or ""
    try:
        val = json.loads(raw) if raw else []
    except (ValueError, TypeError):
        return []
    return [str(x) for x in val] if isinstance(val, list) else []


def _auth_config(settings: Any) -> list[str]:
    """`-c http.extraHeader=...` carrying the ADO PAT, so clone/pull authenticate
    the same way the rest of the app reaches ADO (the host's ambient git
    credential is not reliably present for every repo). Empty when no PAT."""
    pat = getattr(settings, "ado_pat", None)
    if not pat:
        return []
    token = base64.b64encode(f":{pat}".encode()).decode()
    return ["-c", f"http.extraHeader=Authorization: Basic {token}"]


def _git(*args: str, cwd: Path | None = None) -> tuple[int, str]:
    """Run git non-interactively (a missing credential fails fast, never hangs)."""
    result = subprocess.run(
        ["git", *args],
        cwd=str(cwd) if cwd else None,
        capture_output=True,
        text=True,
        env={"GIT_TERMINAL_PROMPT": "0", "PATH": _PATH},
    )
    return result.returncode, (result.stdout + result.stderr).strip()


def _head(dest: Path) -> str | None:
    code, out = _git("-C", str(dest), "log", "-1", "--format=%h %s")
    return out if code == 0 else None


def list_skills(skills_root: Path | str, repo_dir: str) -> list[str]:
    """Skill-folder names (dirs containing SKILL.md) in a cloned repo."""
    root = Path(skills_root) / repo_dir
    if not root.is_dir():
        return []
    return sorted(
        p.name for p in root.iterdir() if p.is_dir() and (p / "SKILL.md").is_file()
    )


def status(settings: Any) -> list[dict[str, Any]]:
    """Per-repo view for the admin panel."""
    root = Path(config.skills_root)
    out: list[dict[str, Any]] = []
    for role, repo_dir, mount, _prefix in REPOS:
        dest = root / repo_dir
        present = (dest / ".git").is_dir()
        out.append(
            {
                "role": role,
                "repo": repo_dir,
                "mount": mount,
                "url": _repo_url(settings, role),
                "configured": bool(_repo_url(settings, role)),
                "present": present,
                "head": _head(dest) if present else None,
                "available": list_skills(root, repo_dir) if present else [],
                "selected": _selected(settings, role),
            }
        )
    return out


def sync(settings: Any) -> list[dict[str, Any]]:
    """Clone each configured skills repo, or fast-forward it if already cloned."""
    root = Path(config.skills_root)
    root.mkdir(parents=True, exist_ok=True)
    auth = _auth_config(settings)
    results: list[dict[str, Any]] = []
    for role, repo_dir, _mount, _prefix in REPOS:
        url = _repo_url(settings, role)
        dest = root / repo_dir
        if not url:
            results.append({"repo": repo_dir, "ok": False, "action": "skip",
                            "detail": "no repo configured"})
            continue
        if (dest / ".git").is_dir():
            code, out = _git(*auth, "-C", str(dest), "pull", "--ff-only")
            action = "pull"
        else:
            code, out = _git(*auth, "clone", "--depth", "1", url, str(dest))
            action = "clone"
        results.append({
            "repo": repo_dir,
            "ok": code == 0,
            "action": action,
            "detail": out[-500:] if code != 0 else (_head(dest) or "ok"),
        })
    return results


def selected_skill_mounts(settings: Any) -> list[tuple[str, str]]:
    """(host_path, container_path) read-only mounts for the selected skills.

    Only skills that are both selected for the harness AND present in its cloned
    repo are mounted; anything else is silently skipped so a missing skill never
    breaks a run."""
    root = Path(config.skills_root)
    mounts: list[tuple[str, str]] = []
    for role, repo_dir, mount, _prefix in REPOS:
        present = set(list_skills(root, repo_dir))
        for name in _selected(settings, role):
            if name in present:
                mounts.append((str(root / repo_dir / name), f"{mount}/{name}"))
    return mounts
