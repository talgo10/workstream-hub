"""Configuration and runtime paths for Workstream Hub."""

from __future__ import annotations

import os
from pathlib import Path

_DEFAULT_HOME = Path(os.path.expanduser("~/.workstreams"))
WORKSTREAMS_HOME: Path = Path(os.getenv("WORKSTREAMS_HOME", str(_DEFAULT_HOME)))

_DEFAULT_SHUNT_MIN_LINES = 350
SHUNT_MIN_LINES: int = int(os.getenv("SHUNT_MIN_LINES", str(_DEFAULT_SHUNT_MIN_LINES)))

PAPARATS_URL: str = os.getenv("PAPARATS_URL", "http://localhost:9876")
OMNI_UI_URL: str = os.getenv("OMNI_UI_URL", "http://localhost:6767")
UI_PORT: int = int(os.getenv("WS_UI_PORT", "8765"))
SHUNT_WORKER_CMD: str = os.getenv("SHUNT_WORKER_CMD", "")


def get_workstreams_home() -> Path:
    """Return the active WORKSTREAMS_HOME directory, respecting env and monkeypatches."""
    if WORKSTREAMS_HOME != _DEFAULT_HOME:
        return Path(WORKSTREAMS_HOME).expanduser().resolve()
    env_val = os.getenv("WORKSTREAMS_HOME")
    if env_val:
        return Path(env_val).expanduser().resolve()
    return Path(WORKSTREAMS_HOME).expanduser().resolve()


def get_shunt_min_lines() -> int:
    """Return the active SHUNT_MIN_LINES threshold, respecting env and monkeypatches."""
    if SHUNT_MIN_LINES != _DEFAULT_SHUNT_MIN_LINES:
        return int(SHUNT_MIN_LINES)
    return int(os.getenv("SHUNT_MIN_LINES", str(SHUNT_MIN_LINES)))


def get_db_path() -> Path:
    return get_workstreams_home() / "state.db"


def get_docs_dir() -> Path:
    return get_workstreams_home() / "docs"


def get_efforts_dir() -> Path:
    return get_workstreams_home() / "efforts"


def get_worktrees_dir() -> Path:
    return get_workstreams_home() / "worktrees"


def get_paparats_dir() -> Path:
    return get_workstreams_home() / "paparats"


def get_paparats_projects_yml() -> Path:
    return get_paparats_dir() / "projects.yml"


def ensure_workstreams_dirs() -> Path:
    """Ensure the ~/.workstreams runtime directory hierarchy exists."""
    home = get_workstreams_home()
    home.mkdir(parents=True, exist_ok=True)
    get_docs_dir().mkdir(parents=True, exist_ok=True)
    get_efforts_dir().mkdir(parents=True, exist_ok=True)
    get_worktrees_dir().mkdir(parents=True, exist_ok=True)
    get_paparats_dir().mkdir(parents=True, exist_ok=True)
    return home

