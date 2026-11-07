"""Pytest fixtures for Workstream Hub verification suite."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from workstream_hub import config
from workstream_hub.db import init_db


@pytest.fixture(autouse=True)
def ws_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Provide an isolated WORKSTREAMS_HOME for every test."""
    home = tmp_path / "workstreams_home"
    home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("WORKSTREAMS_HOME", str(home))
    monkeypatch.setattr(config, "WORKSTREAMS_HOME", home)
    init_db()
    return home


@pytest.fixture()
def temp_git_repo(tmp_path: Path) -> Path:
    """Create a temporary canonical Git repository with an initial commit on `main`."""
    repo = tmp_path / "sample_repo"
    repo.mkdir(parents=True, exist_ok=True)

    subprocess.run(["git", "init", "-b", "main"], cwd=repo, check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "agent@workstreams.local"],
        cwd=repo,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Workstream Agent"],
        cwd=repo,
        check=True,
        capture_output=True,
    )

    core_py = repo / "core.py"
    core_py.write_text(
        '"""Core module for sample repository."""\n\n\n'
        "def base_transform(x: int) -> int:\n"
        '    """Multiply input by two."""\n'
        "    return x * 2\n",
        encoding="utf-8",
    )
    subprocess.run(["git", "add", "core.py"], cwd=repo, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "Initial commit on main"],
        cwd=repo,
        check=True,
        capture_output=True,
    )
    return repo

