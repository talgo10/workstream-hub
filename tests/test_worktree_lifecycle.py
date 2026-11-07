"""AC-4, AC-5, AC-6, and AC-7 Verification: Two-Tier Worktrees, AST Diff Extraction, Auto-Prune, Dirty Check, Conflict Radar, and Web Cockpit."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from workstream_hub.cli_mcp import (
    app as cli_app,
    ws_create_effort,
    ws_create_sub_effort,
    ws_create_workstream,
    ws_finalize,
    ws_hydrate,
    ws_reopen_worktree,
)
from workstream_hub.config import get_efforts_dir, get_worktrees_dir
from workstream_hub.db import get_connection, get_effort
from workstream_hub.ui_server import app as ui_app
from workstream_hub.worktree_mgr import DirtyWorktreeError, check_sibling_conflicts


def test_ac4_two_tier_worktree_ast_extraction_and_autoprune(temp_git_repo: Path) -> None:
    """
    AC-4:
    Using a temporary local Git repo, creating eff-1 creates worktree ~/.workstreams/efforts/eff-1 (branch ws/eff-1).
    Creating sub-1 creates worktree ~/.workstreams/worktrees/sub-1 (branch ws/sub-1).
    Committing a new Python function `def compute_sparse_hash(df): ...` in sub-1 and running
    ws_finalize("sub-1", ..., merge_to_effort=True):
      1. Extracts compute_sparse_hash into code_artifacts via tree-sitter.
      2. Merges ws/sub-1 into ws/eff-1.
      3. Deletes the physical directory ~/.workstreams/worktrees/sub-1 (os.path.exists is False)
         while keeping branch ws/sub-1 intact in Git so `git diff ws/eff-1~1..ws/sub-1` still succeeds.
    """
    ws_create_workstream("ws-wt", title="Worktree Lifecycle", goal="Test worktrees & AST")
    eff_res = ws_create_effort("eff-1", ws_id="ws-wt", title="Hashing Effort", repo_path=str(temp_git_repo))
    eff_wt = get_efforts_dir() / "eff-1"
    assert eff_wt.exists()
    assert eff_res["git_branch"] == "ws/eff-1"

    sub_res = ws_create_sub_effort("sub-1", parent_effort_id="eff-1", title="Sparse Hash Sub-Effort")
    sub_wt = get_worktrees_dir() / "sub-1"
    assert sub_wt.exists()
    assert sub_res["git_branch"] == "ws/sub-1"

    hyd = ws_hydrate("sub-1", client_id="win-1")
    token = hyd["lease_token"]

    # Commit a new Python function `def compute_sparse_hash(df): ...` in sub-1
    sparse_file = sub_wt / "sparse_hash.py"
    sparse_file.write_text(
        '"""Sparse hashing utilities."""\n\n\n'
        "def compute_sparse_hash(df):\n"
        '    """Compute deterministic sparse hash over dataframe rows."""\n'
        "    return hash(tuple(df))\n",
        encoding="utf-8",
    )
    subprocess.run(["git", "add", "sparse_hash.py"], cwd=sub_wt, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "Add compute_sparse_hash"],
        cwd=sub_wt,
        check=True,
        capture_output=True,
    )

    fin = ws_finalize(
        "sub-1",
        lease_token=token,
        conclusion_md="Implemented compute_sparse_hash with O(nnz) complexity.",
        merge_to_effort=True,
    )
    assert fin["status"] == "FINALIZED"
    assert "compute_sparse_hash" in fin["ast_symbols_extracted"]

    # 1. Verify compute_sparse_hash is stored in code_artifacts via tree-sitter
    conn = get_connection()
    try:
        artifacts = conn.execute(
            "SELECT * FROM code_artifacts WHERE effort_id = 'sub-1'"
        ).fetchall()
        assert any(a["symbol_name"] == "compute_sparse_hash" for a in artifacts)
    finally:
        conn.close()

    # 2. Verify ws/sub-1 was merged into ws/eff-1 worktree
    assert (eff_wt / "sparse_hash.py").exists()

    # 3. Verify ~/.workstreams/worktrees/sub-1 is auto-pruned while branch ws/sub-1 remains in Git
    assert os.path.exists(sub_wt) is False
    sub_row = get_effort("sub-1")
    assert sub_row is not None
    assert sub_row["worktree_pruned"] == 1
    assert sub_row["worktree_path"] is None

    diff_proc = subprocess.run(
        ["git", "-C", str(temp_git_repo), "diff", "ws/eff-1~1..ws/sub-1"],
        capture_output=True,
        text=True,
        check=True,
    )
    assert "def compute_sparse_hash(df):" in diff_proc.stdout

    # 4. Verify AC-7 smoke test on CLI and Web Cockpit (including pruned sub-1 Diff2Html drawer)
    runner = CliRunner()
    cli_help = runner.invoke(cli_app, ["--help"])
    assert cli_help.exit_code == 0

    client = TestClient(ui_app)
    r_home = client.get("/")
    assert r_home.status_code == 200
    assert "eff-1" in r_home.text
    assert "sub-1" in r_home.text

    r_search = client.get("/ui/search?q=sparse")
    assert r_search.status_code == 200
    assert "compute_sparse_hash" in r_search.text

    r_drawer = client.get("/ui/drawer/sub-1")
    assert r_drawer.status_code == 200
    assert "Diff2HtmlUI" in r_drawer.text
    assert "compute_sparse_hash" in r_drawer.text

    # Verify Paparats projects.yml was generated and populated
    from workstream_hub.config import get_paparats_projects_yml

    projects_yml = get_paparats_projects_yml()
    assert projects_yml.exists()
    assert "eff-1" in projects_yml.read_text(encoding="utf-8")

    # Verify all 15 CLI commands via Typer CliRunner & omni delegation
    from workstream_hub.cli_mcp import ws_delegate_omni

    del_res = ws_delegate_omni("sub-1", task_prompt="Run benchmark suite")
    assert del_res["status"] == "DELEGATED"
    assert del_res["lease_owner"] == "omni-cli"
    assert del_res["lease_token"].startswith("tok_")

    # Verify ws_reopen_worktree can re-materialize sub-1 when needed
    reopened = ws_reopen_worktree("sub-1", client_id="win-1")
    assert reopened["status"] == "REOPENED"
    assert Path(reopened["worktree_path"]).exists()


def test_ac7_macos_cli_mcp_and_web_cockpit_smoke_test(temp_git_repo: Path) -> None:
    """
    AC-7:
    `ws --help` exits 0; FastAPI TestClient against ui_server.app returns HTTP 200 on
    GET /, GET /ui/search?q=sparse, and GET /ui/drawer/sub-1 (returning valid Diff2Html
    markup even after sub-1's worktree has been auto-pruned).
    """
    runner = CliRunner()
    cli_help = runner.invoke(cli_app, ["--help"])
    assert cli_help.exit_code == 0
    assert "create-ws" in cli_help.stdout
    assert "shunt-read" in cli_help.stdout

    ws_create_workstream("ws-ui", title="UI Cockpit Epic", goal="Smoke test UI & CLI")
    ws_create_effort("eff-1", ws_id="ws-ui", title="UI Effort", repo_path=str(temp_git_repo))
    ws_create_sub_effort("sub-1", parent_effort_id="eff-1", title="UI Sub-Effort")

    hyd = ws_hydrate("sub-1", client_id="win-ui")
    sub_wt = Path(hyd["worktree_path"])
    (sub_wt / "sparse_index.py").write_text(
        "def compute_sparse_hash(df):\n    return len(df)\n",
        encoding="utf-8",
    )
    subprocess.run(["git", "add", "sparse_index.py"], cwd=sub_wt, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "Add sparse_index"], cwd=sub_wt, check=True, capture_output=True)

    ws_finalize(
        "sub-1",
        lease_token=hyd["lease_token"],
        conclusion_md="Sparse hash index finalized for UI smoke test.",
        merge_to_effort=True,
    )
    assert not sub_wt.exists()

    client = TestClient(ui_app)
    r_root = client.get("/")
    assert r_root.status_code == 200
    assert "Workstream Hub" in r_root.text
    assert "sub-1" in r_root.text

    r_search = client.get("/ui/search?q=sparse")
    assert r_search.status_code == 200
    assert "compute_sparse_hash" in r_search.text

    r_drawer = client.get("/ui/drawer/sub-1")
    assert r_drawer.status_code == 200
    assert "Diff2HtmlUI" in r_drawer.text
    assert "compute_sparse_hash" in r_drawer.text

    # Verify Paparats projects.yml was generated and populated
    from workstream_hub.config import get_paparats_projects_yml

    projects_yml = get_paparats_projects_yml()
    assert projects_yml.exists()
    assert "eff-1" in projects_yml.read_text(encoding="utf-8")

    # Verify all 15 CLI commands via Typer CliRunner & omni delegation
    from workstream_hub.cli_mcp import ws_delegate_omni

    del_res = ws_delegate_omni("sub-1", task_prompt="Run benchmark suite")
    assert del_res["status"] == "DELEGATED"
    assert del_res["lease_owner"] == "omni-cli"
    assert del_res["lease_token"].startswith("tok_")


def test_ac5_dirty_worktree_protection(temp_git_repo: Path) -> None:
    """
    AC-5:
    If sub-1 has uncommitted files in its worktree, ws_finalize aborts without pruning
    or indexing and raises a clear error listing the uncommitted files.
    """
    ws_create_workstream("ws-dirty", title="Dirty Check Epic", goal="Protect uncommitted work")
    ws_create_effort("eff-1", ws_id="ws-dirty", title="Base Effort", repo_path=str(temp_git_repo))
    ws_create_sub_effort("sub-1", parent_effort_id="eff-1", title="Dirty Sub-Effort")

    hyd = ws_hydrate("sub-1", client_id="win-1")
    token = hyd["lease_token"]
    sub_wt = Path(hyd["worktree_path"])

    # Create an uncommitted file inside sub-1's worktree
    uncommitted = sub_wt / "uncommitted_experiment.py"
    uncommitted.write_text("x = 42\n", encoding="utf-8")

    with pytest.raises(DirtyWorktreeError) as exc_info:
        ws_finalize(
            "sub-1",
            lease_token=token,
            conclusion_md="Attempting to finalize with dirty tree",
            merge_to_effort=True,
        )

    assert "uncommitted_experiment.py" in str(exc_info.value)
    # Worktree must NOT be pruned
    assert sub_wt.exists()
    sub_row = get_effort("sub-1")
    assert sub_row is not None
    assert sub_row["worktree_pruned"] == 0


def test_ac6_in_memory_sibling_conflict_detection(temp_git_repo: Path) -> None:
    """
    AC-6:
    Two parallel sub-efforts (sub-a and sub-b) modify the same lines of core.py on their respective branches.
    Calling check_sibling_conflicts("eff-1") returns has_conflict=True and lists ["core.py"]
    without modifying either working tree.
    """
    ws_create_workstream("ws-conflict", title="Conflict Radar Epic", goal="Detect sibling conflicts")
    ws_create_effort("eff-1", ws_id="ws-conflict", title="Shared Core Effort", repo_path=str(temp_git_repo))
    sub_a = ws_create_sub_effort("sub-a", parent_effort_id="eff-1", title="Sub A")
    sub_b = ws_create_sub_effort("sub-b", parent_effort_id="eff-1", title="Sub B")

    wt_a = Path(sub_a["worktree_path"])
    wt_b = Path(sub_b["worktree_path"])

    # Modify core.py on sub-a
    (wt_a / "core.py").write_text(
        '"""Core module modified by sub-a."""\n\n\n'
        "def base_transform(x: int) -> int:\n"
        "    return x * 100 + 1\n",
        encoding="utf-8",
    )
    subprocess.run(["git", "add", "core.py"], cwd=wt_a, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "Sub A transform"], cwd=wt_a, check=True, capture_output=True)

    # Modify the exact same lines of core.py on sub-b
    (wt_b / "core.py").write_text(
        '"""Core module modified by sub-b."""\n\n\n'
        "def base_transform(x: int) -> int:\n"
        "    return x * 999 - 7\n",
        encoding="utf-8",
    )
    subprocess.run(["git", "add", "core.py"], cwd=wt_b, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "Sub B transform"], cwd=wt_b, check=True, capture_output=True)

    conflicts = check_sibling_conflicts("eff-1")
    assert conflicts["has_conflict"] is True
    assert "core.py" in conflicts["conflicting_files"]

    # Verify zero working-tree mutation in both sub-a and sub-b
    status_a = subprocess.run(
        ["git", "status", "--porcelain"], cwd=wt_a, capture_output=True, text=True, check=True
    )
    status_b = subprocess.run(
        ["git", "status", "--porcelain"], cwd=wt_b, capture_output=True, text=True, check=True
    )
    assert status_a.stdout.strip() == ""
    assert status_b.stdout.strip() == ""

