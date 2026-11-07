"""AC-2 Verification: Fixed 3-Tier Constraint & Bottom-Up Sibling Rollup Inheritance."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from workstream_hub.cli_mcp import (
    ws_create_effort,
    ws_create_sub_effort,
    ws_create_workstream,
    ws_finalize,
    ws_hydrate,
)
from workstream_hub.config import get_docs_dir
from workstream_hub.db import (
    HierarchyConstraintError,
    create_sub_effort_record,
    get_connection,
    get_effort,
)


def test_ac2_three_tier_constraint_and_sibling_rollup(temp_git_repo: Path) -> None:
    """
    AC-2:
    1. Attempting to create a Tier-4 child under a Tier-3 Sub-Effort raises a database constraint error.
    2. Finalizing sub-1 under eff-1 automatically populates eff-1.children_rollup_md and mirrors SIBLING_ROLLUP.md.
    3. Hydrating sibling sub-2 under eff-1 includes sub-1's conclusion, metrics, and Git SHA in its hydration payload.
    """
    ws_create_workstream("ws-hier", title="Hierarchy Epic", goal="Enforce 3-tier tree")
    ws_create_effort("eff-1", ws_id="ws-hier", title="Parent Feature Effort", repo_path=str(temp_git_repo))
    ws_create_sub_effort("sub-1", parent_effort_id="eff-1", title="First POC Sub-Effort")
    ws_create_sub_effort("sub-2", parent_effort_id="eff-1", title="Second Sibling Sub-Effort")

    # 1a. Attempting to create a child under Tier-3 sub-1 via API raises HierarchyConstraintError (IntegrityError)
    with pytest.raises((HierarchyConstraintError, sqlite3.IntegrityError)):
        ws_create_sub_effort("tier4-illegal", parent_effort_id="sub-1", title="Illegal Tier 4")

    with pytest.raises((HierarchyConstraintError, sqlite3.IntegrityError)):
        create_sub_effort_record("tier4-rec", parent_effort_id="sub-1", title="Illegal Tier 4", tier=4)

    # 1b. Attempting raw SQL insert with tier=4 or parent_effort_id='sub-1' also fails DB constraints/triggers
    conn = get_connection()
    try:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                """
                INSERT INTO efforts (id, workstream_id, parent_effort_id, tier, title, repo_path, git_branch)
                VALUES ('raw-tier4', 'ws-hier', 'sub-1', 4, 'Illegal', ?, 'ws/raw-tier4')
                """,
                (str(temp_git_repo),),
            )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                """
                INSERT INTO efforts (id, workstream_id, parent_effort_id, tier, title, repo_path, git_branch)
                VALUES ('raw-child-of-sub', 'ws-hier', 'sub-1', 3, 'Illegal Child of Tier 3', ?, 'ws/raw-child')
                """,
                (str(temp_git_repo),),
            )
    finally:
        conn.close()

    # 2. Hydrate and finalize sub-1 under eff-1
    hyd_sub1 = ws_hydrate("sub-1", client_id="win-1")
    tok_sub1 = hyd_sub1["lease_token"]
    conclusion_text = "Validated vectorized bloom filter with p99 latency = 1.4ms and 0% false negatives."

    fin_res = ws_finalize(
        "sub-1",
        lease_token=tok_sub1,
        conclusion_md=conclusion_text,
        merge_to_effort=True,
    )
    assert fin_res["status"] == "FINALIZED"
    sub1_sha = fin_res["git_sha"]

    # Check eff-1.children_rollup_md in state.db and mirrored SIBLING_ROLLUP.md on disk
    eff1_row = get_effort("eff-1")
    assert eff1_row is not None
    assert "sub-1" in eff1_row["children_rollup_md"]
    assert conclusion_text in eff1_row["children_rollup_md"]
    assert sub1_sha in eff1_row["children_rollup_md"]

    rollup_file = get_docs_dir() / "ws-hier" / "eff-1" / "SIBLING_ROLLUP.md"
    assert rollup_file.exists()
    rollup_disk_text = rollup_file.read_text(encoding="utf-8")
    assert conclusion_text in rollup_disk_text
    assert sub1_sha in rollup_disk_text

    # 3. Hydrating sibling sub-2 under eff-1 includes sub-1's conclusion, metrics, and Git SHA
    hyd_sub2 = ws_hydrate("sub-2", client_id="win-2")
    assert hyd_sub2["status"] == "HYDRATED"
    inherited_rollup = hyd_sub2["children_rollup_md"]
    assert "sub-1" in inherited_rollup
    assert conclusion_text in inherited_rollup
    assert sub1_sha in inherited_rollup

