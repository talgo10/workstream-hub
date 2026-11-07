"""AC-8 Verification: Shunt 350-Line Hard Gate, bulk-reader, code-writer, and Telemetry."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from workstream_hub.cli_mcp import (
    ws_check_file_gate,
    ws_create_effort,
    ws_create_sub_effort,
    ws_create_workstream,
    ws_hydrate,
    ws_shunt_read,
    ws_shunt_write,
)
from workstream_hub.db import get_connection, get_shunt_stats


def test_ac8_shunt_gate_bulk_reader_and_code_writer(
    temp_git_repo: Path, tmp_path: Path
) -> None:
    """
    AC-8:
    1. Calling check_file_gate on a 120-line file returns allowed=True.
    2. Calling check_file_gate on a 500-line file without offset/limit returns allowed=False
       and logs GATE_BLOCKED in shunt_logs.
    3. Calling check_file_gate on the same 500-line file with offset=10, limit=80 returns allowed=True.
    4. Calling ws_shunt_read on the 500-line file returns a compact <250 token summary/symbol map
       and increments estimated_tokens_saved in shunt_logs.
    5. Calling ws_shunt_write with a valid lease_token and reference_file writes the target file
       in the worktree and returns extracted AST symbols.
    """
    ws_create_workstream("ws-shunt", title="Shunt Telemetry Epic", goal="Protect context window")
    ws_create_effort("eff-shunt", ws_id="ws-shunt", title="Shunt Effort", repo_path=str(temp_git_repo))
    ws_create_sub_effort("sub-shunt", parent_effort_id="eff-shunt", title="Shunt Sub-Effort")

    hyd = ws_hydrate("sub-shunt", client_id="win-shunt")
    token = hyd["lease_token"]
    wt_path = Path(hyd["worktree_path"])

    # Create a 120-line file and a 500-line Python file
    small_file = tmp_path / "small_module.py"
    small_file.write_text(
        "\n".join(f"x_{i} = {i}" for i in range(120)) + "\n",
        encoding="utf-8",
    )

    large_lines: list[str] = ['"""Large data processing module exceeding 350 lines."""\n']
    for idx in range(25):
        large_lines.extend(
            [
                f"def process_partition_batch_{idx}(records: list[dict]) -> int:",
                f'    """Process batch partition {idx} and compute checksum."""',
                f"    total = {idx}",
                "    for item in records:",
                "        total += len(item)",
                "    # Padding lines to simulate a 500-line production file",
                "    step_a = total * 2",
                "    step_b = step_a + 1",
                "    step_c = step_b - 1",
                "    step_d = step_c * 3",
                "    step_e = step_d // 3",
                "    step_f = step_e + 7",
                "    step_g = step_f - 7",
                "    step_h = step_g + 0",
                "    step_i = step_h * 1",
                "    step_j = step_i + 2",
                "    step_k = step_j - 2",
                "    step_l = step_k + 4",
                "    step_m = step_l - 4",
                "    return step_m\n",
            ]
        )
    large_file = tmp_path / "large_pipeline.py"
    large_file.write_text("\n".join(large_lines), encoding="utf-8")
    assert len(large_file.read_text(encoding="utf-8").splitlines()) >= 500

    # 1. check_file_gate on 120-line file -> allowed=True
    gate_small = ws_check_file_gate(str(small_file))
    assert gate_small["allowed"] is True
    assert gate_small["line_count"] == 120

    # 2. check_file_gate on 500-line file without offset/limit -> allowed=False & logs GATE_BLOCKED
    gate_large_blocked = ws_check_file_gate(str(large_file), effort_id="sub-shunt")
    assert gate_large_blocked["allowed"] is False
    assert gate_large_blocked["line_count"] >= 500
    assert gate_large_blocked["threshold"] == 350
    assert "BLOCKED" in gate_large_blocked["directive"]

    conn = get_connection()
    try:
        blocked_logs = conn.execute(
            "SELECT * FROM shunt_logs WHERE mode = 'GATE_BLOCKED'"
        ).fetchall()
        assert len(blocked_logs) == 1
    finally:
        conn.close()

    # Verify executable hook script check_file_size_hook.py exits non-zero (2) on un-ranged 500-line file
    hook_script = Path(__file__).resolve().parents[1] / "scripts" / "check_file_size_hook.py"
    proc_block = subprocess.run(
        [sys.executable, str(hook_script), str(large_file)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc_block.returncode != 0

    # 3. check_file_gate on the same 500-line file with offset=10, limit=80 -> allowed=True
    gate_large_ranged = ws_check_file_gate(str(large_file), offset=10, limit=80)
    assert gate_large_ranged["allowed"] is True

    proc_allow = subprocess.run(
        [sys.executable, str(hook_script), str(large_file), "--offset", "10", "--limit", "80"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc_allow.returncode == 0

    # 4. ws_shunt_read on the 500-line file -> compact <250 token summary & increments estimated_tokens_saved
    read_res = ws_shunt_read(
        files=[str(large_file)],
        question="Which functions process partition batch 12 and compute checksum?",
        effort_id="sub-shunt",
    )
    assert read_res["status"] == "OK"
    assert read_res["response_tokens"] < 250
    assert read_res["estimated_tokens_saved"] > 1000
    assert "large_pipeline.py:" in read_res["summary"]
    assert len(read_res["top_symbols"]) > 0

    stats_after_read = get_shunt_stats()
    assert stats_after_read["bulk_reads"] == 1
    assert stats_after_read["total_tokens_saved"] >= read_res["estimated_tokens_saved"]

    # 5. ws_shunt_write with valid lease_token and reference_file -> writes target file & returns AST symbols
    write_res = ws_shunt_write(
        effort_id="sub-shunt",
        lease_token=token,
        target_file="generated_validators.py",
        spec_prompt=(
            "Create `def validate_sparse_frame(df)` and `class PartitionValidator` "
            "following the conventions of `core.py`."
        ),
        reference_file="core.py",
    )
    assert write_res["status"] == "WRITTEN"
    written_path = Path(write_res["target_file"])
    assert written_path.exists()
    assert written_path.parent == wt_path
    assert "validate_sparse_frame" in write_res["ast_symbols_created"]
    assert "PartitionValidator" in write_res["ast_symbols_created"]

    stats_after_write = get_shunt_stats()
    assert stats_after_write["code_writes"] == 1
    assert stats_after_write["total_tokens_saved"] > stats_after_read["total_tokens_saved"]

