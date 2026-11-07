"""Databricks Omnigent (`omni`) CLI sub-agent bridge."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

from workstream_hub.config import OMNI_UI_URL
from workstream_hub.db import acquire_lease, get_connection, get_effort


def delegate_to_omni(
    sub_id: str,
    task_prompt: str,
    dry_run: bool = False,
    db_path: Path | None = None,
) -> dict[str, Any]:
    """
    Acquire lease for `omni-cli` on `sub_id`, configure callback instructions,
    and spawn (or prepare) a headless Databricks `omni` sub-agent inside the sub-effort worktree.
    """
    effort = get_effort(sub_id, db_path=db_path)
    if effort is None:
        raise KeyError(f"Effort '{sub_id}' not found.")

    lease = acquire_lease(
        effort_id=sub_id,
        client_id="omni-cli",
        steal=True,
        db_path=db_path,
    )
    lease_token = lease["lease_token"]
    worktree_path = effort.get("worktree_path") or effort["repo_path"]
    session_url = f"{OMNI_UI_URL.rstrip('/')}/sessions/{sub_id}"

    conn = get_connection(db_path)
    try:
        conn.execute(
            """
            UPDATE efforts
            SET omni_session_url = ?,
                phase_tag = CASE WHEN phase_tag IN ('RESEARCH', 'POC') THEN 'EXECUTING' ELSE phase_tag END,
                updated_at = datetime('now')
            WHERE id = ?
            """,
            (session_url, sub_id),
        )
    finally:
        conn.close()

    callback_prompt = (
        f"[Workstream Hub Sub-Agent Assignment]\n"
        f"Effort ID: {sub_id}\n"
        f"Lease Token: {lease_token}\n"
        f"Worktree Path: {worktree_path}\n"
        f"Shunt Policy: Enforce SHUNT_MIN_LINES=350 (use ws_shunt_read / ws_shunt_write).\n"
        f"Task: {task_prompt}\n"
        f"Callback: Upon completion, commit all changes and run:\n"
        f"  ws finalize {sub_id} --lease-token {lease_token} --conclusion \"<summary>\"\n"
    )

    omni_bin = shutil.which("omni")
    spawned_pid: int | None = None
    if omni_bin and not dry_run and os.getenv("WS_OMNI_DRY_RUN", "0") != "1":
        try:
            proc = subprocess.Popen(
                [omni_bin, "run", "--headless", "--prompt", callback_prompt],
                cwd=worktree_path if Path(worktree_path).exists() else None,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            spawned_pid = proc.pid
        except Exception:
            spawned_pid = None

    return {
        "status": "DELEGATED",
        "sub_id": sub_id,
        "lease_owner": "omni-cli",
        "lease_token": lease_token,
        "worktree_path": worktree_path,
        "omni_session_url": session_url,
        "spawned_pid": spawned_pid,
        "callback_prompt": callback_prompt,
    }


def check_omni_health() -> bool:
    """Return True if omni CLI or local Omnigent UI (:6767) is reachable."""
    if shutil.which("omni"):
        return True
    try:
        import httpx

        with httpx.Client(timeout=0.3) as client:
            resp = client.get(f"{OMNI_UI_URL.rstrip('/')}/health")
            return resp.status_code < 500
    except Exception:
        return False

