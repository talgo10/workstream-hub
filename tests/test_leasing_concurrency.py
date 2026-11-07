"""AC-1 Verification: Atomic Session Leases, Monotonic Fencing Tokens, and --steal."""

from __future__ import annotations

from pathlib import Path

import pytest

from workstream_hub.cli_mcp import (
    ws_checkpoint,
    ws_create_effort,
    ws_create_sub_effort,
    ws_create_workstream,
    ws_hydrate,
    ws_save_spec,
)
from workstream_hub.db import LeaseFencingError


def test_ac1_mutual_exclusion_and_fencing_tokens(temp_git_repo: Path) -> None:
    """
    AC-1:
    Session A calls ws_hydrate("sub-1", client_id="win-1") and receives tok_A.
    Session B calls ws_hydrate("sub-1", client_id="win-2", steal=False) and receives LOCKED_BY_OTHER_SESSION.
    Session B then calls ws_hydrate("sub-1", client_id="win-2", steal=True) and receives tok_B.
    Session A attempts ws_checkpoint("sub-1", lease_token=tok_A, ...) and fails with LeaseFencingError.
    """
    ws_create_workstream("ws-1", title="Core Platform", goal="Validate fencing tokens")
    ws_create_effort("eff-1", ws_id="ws-1", title="Leasing Feature", repo_path=str(temp_git_repo))
    ws_create_sub_effort("sub-1", parent_effort_id="eff-1", title="Sub-Effort 1")

    # 1. Session A hydrates and acquires lease token tok_A
    res_a = ws_hydrate("sub-1", client_id="win-1", steal=False)
    assert res_a["status"] == "HYDRATED"
    assert res_a["lease_owner"] == "win-1"
    tok_a = res_a["lease_token"]
    assert tok_a.startswith("tok_")

    # Verify .ws_lock.json was written into sub-1's worktree
    wt_path = Path(res_a["worktree_path"])
    assert (wt_path / ".ws_lock.json").exists()

    # 2. Session B attempts to hydrate without steal -> LOCKED_BY_OTHER_SESSION
    res_b_blocked = ws_hydrate("sub-1", client_id="win-2", steal=False)
    assert res_b_blocked["status"] == "LOCKED_BY_OTHER_SESSION"
    assert res_b_blocked["active_owner"] == "win-1"

    # 3. Session B hydrates with steal=True -> receives tok_B
    res_b_stolen = ws_hydrate("sub-1", client_id="win-2", steal=True)
    assert res_b_stolen["status"] == "HYDRATED"
    assert res_b_stolen["lease_owner"] == "win-2"
    tok_b = res_b_stolen["lease_token"]
    assert tok_b.startswith("tok_")
    assert tok_b != tok_a

    # 4. Session A attempts mutating calls with stale tok_A -> fails with LeaseFencingError
    with pytest.raises(LeaseFencingError):
        ws_checkpoint(
            "sub-1",
            lease_token=tok_a,
            hypothesis="Stale session hypothesis",
            exact_next_step="Should be rejected",
        )

    with pytest.raises(LeaseFencingError):
        ws_save_spec("sub-1", lease_token=tok_a, spec_md="## Unauthorized spec write")

    # 5. Session B checkpoints with valid tok_B and releases lease
    ckpt = ws_checkpoint(
        "sub-1",
        lease_token=tok_b,
        hypothesis="Validated fencing token takeover",
        decisions_locked=["Use SQLite BEGIN IMMEDIATE"],
        modified_files=["core.py"],
        open_blockers="None",
        exact_next_step="Proceed with sub-2",
        release_lease=True,
    )
    assert ckpt["lease_released"] is True
    assert not (wt_path / ".ws_lock.json").exists()

    # 6. Now Session A can acquire lease cleanly without steal
    res_a_reacquired = ws_hydrate("sub-1", client_id="win-1", steal=False)
    assert res_a_reacquired["status"] == "HYDRATED"
    assert res_a_reacquired["last_checkpoint"]["exact_next_step"] == "Proceed with sub-2"

