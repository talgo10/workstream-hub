"""Two-Tier Git Worktree Manager, Sub-Effort Finalization & Auto-Prune, and Sibling Conflict Radar."""

from __future__ import annotations

import itertools
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

from workstream_hub.ast_extractor import extract_and_index_git_diff_ast
from workstream_hub.config import (
    ensure_workstreams_dirs,
    get_docs_dir,
    get_efforts_dir,
    get_worktrees_dir,
)
from workstream_hub.db import (
    HierarchyConstraintError,
    _remove_lock_file,
    acquire_lease,
    append_child_rollup,
    create_effort_record,
    create_sub_effort_record,
    get_connection,
    get_effort,
    release_lease,
    verify_and_refresh_lease,
)
from workstream_hub.indexer import index_document_chunks
from workstream_hub.paparats_client import (
    register_effort_in_projects_yml,
    trigger_paparats_index,
)


class DirtyWorktreeError(RuntimeError, ValueError):
    """Raised when attempting to finalize a worktree with uncommitted changes."""


def _run_git(
    args: list[str],
    cwd: str | Path,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    cmd = ["git", "-C", str(cwd), *args]
    proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if check and proc.returncode != 0:
        raise RuntimeError(
            f"Git command failed ({' '.join(cmd)}): {proc.stderr.strip() or proc.stdout.strip()}"
        )
    return proc


def _branch_exists(repo_path: str | Path, branch_name: str) -> bool:
    proc = _run_git(
        ["show-ref", "--verify", "--quiet", f"refs/heads/{branch_name}"],
        cwd=repo_path,
        check=False,
    )
    return proc.returncode == 0


def _get_head_sha(path: str | Path) -> str:
    proc = _run_git(["rev-parse", "HEAD"], cwd=path, check=True)
    return proc.stdout.strip()


def create_effort_worktree(
    effort_id: str,
    ws_id: str,
    title: str,
    repo_path: str | Path,
    base_branch: str = "main",
    phase_tag: str = "RESEARCH",
    db_path: Path | None = None,
) -> dict[str, Any]:
    """
    Create Tier-2 Effort branch `ws/{effort_id}` from `base_branch` in `repo_path`,
    check out persistent worktree at `~/.workstreams/efforts/{effort_id}`,
    register in Paparats `projects.yml`, and trigger `:9876/trigger`.
    """
    ensure_workstreams_dirs()
    repo = Path(repo_path).resolve()
    if not (repo / ".git").exists():
        raise ValueError(f"Repository path '{repo}' is not a valid git repository.")

    branch_name = f"ws/{effort_id}"
    wt_path = get_efforts_dir() / effort_id

    if wt_path.exists():
        _run_git(["worktree", "remove", str(wt_path), "--force"], cwd=repo, check=False)
        if wt_path.exists():
            shutil.rmtree(wt_path, ignore_errors=True)
        _run_git(["worktree", "prune"], cwd=repo, check=False)

    wt_path.parent.mkdir(parents=True, exist_ok=True)
    if _branch_exists(repo, branch_name):
        _run_git(["worktree", "add", str(wt_path), branch_name], cwd=repo, check=True)
    else:
        _run_git(
            ["worktree", "add", "-b", branch_name, str(wt_path), base_branch],
            cwd=repo,
            check=True,
        )

    head_sha = _get_head_sha(wt_path)
    record = create_effort_record(
        effort_id=effort_id,
        ws_id=ws_id,
        title=title,
        repo_path=str(repo),
        base_branch=base_branch,
        git_branch=branch_name,
        worktree_path=str(wt_path),
        phase_tag=phase_tag,
        latest_git_sha=head_sha,
        db_path=db_path,
    )

    projects_yml = register_effort_in_projects_yml(
        ws_id=ws_id,
        effort_id=effort_id,
        worktree_path=wt_path,
        repo_path=repo,
    )
    paparats_status = trigger_paparats_index(effort_id=effort_id, workstream_id=ws_id)
    record["projects_yml"] = str(projects_yml)
    record["paparats_trigger"] = paparats_status
    return record


def create_sub_effort_worktree(
    sub_id: str,
    parent_effort_id: str,
    title: str,
    phase_tag: str = "POC",
    tier: int = 3,
    db_path: Path | None = None,
) -> dict[str, Any]:
    """
    Create Tier-3 Sub-Effort branch `ws/{sub_id}` branching off `ws/{parent_effort_id}`,
    and check out ephemeral worktree at `~/.workstreams/worktrees/{sub_id}`.
    """
    ensure_workstreams_dirs()
    parent = get_effort(parent_effort_id, db_path=db_path)
    if parent is None:
        raise KeyError(f"Parent effort '{parent_effort_id}' does not exist.")
    if parent["tier"] != 2 or tier != 3:
        raise HierarchyConstraintError(
            f"Cannot create child '{sub_id}' (tier={tier}) under '{parent_effort_id}' "
            f"(tier={parent['tier']}): Fixed 3-Tier Hierarchy constraint."
        )

    repo = Path(parent["repo_path"]).resolve()
    parent_branch = parent["git_branch"]
    branch_name = f"ws/{sub_id}"
    wt_path = get_worktrees_dir() / sub_id

    if wt_path.exists():
        _run_git(["worktree", "remove", str(wt_path), "--force"], cwd=repo, check=False)
        if wt_path.exists():
            shutil.rmtree(wt_path, ignore_errors=True)
        _run_git(["worktree", "prune"], cwd=repo, check=False)

    wt_path.parent.mkdir(parents=True, exist_ok=True)
    if _branch_exists(repo, branch_name):
        _run_git(["worktree", "add", str(wt_path), branch_name], cwd=repo, check=True)
    else:
        _run_git(
            ["worktree", "add", "-b", branch_name, str(wt_path), parent_branch],
            cwd=repo,
            check=True,
        )

    head_sha = _get_head_sha(wt_path)
    return create_sub_effort_record(
        sub_id=sub_id,
        parent_effort_id=parent_effort_id,
        title=title,
        phase_tag=phase_tag,
        git_branch=branch_name,
        worktree_path=str(wt_path),
        latest_git_sha=head_sha,
        tier=tier,
        db_path=db_path,
    )


def get_dirty_files(worktree_path: str | Path) -> list[str]:
    """Return uncommitted/untracked files in worktree_path (ignoring .ws_lock.json)."""
    wt = Path(worktree_path)
    if not wt.exists():
        return []
    proc = _run_git(["status", "--porcelain"], cwd=wt, check=True)
    dirty: list[str] = []
    for line in proc.stdout.splitlines():
        if not line.strip():
            continue
        file_part = line[3:].strip() if len(line) > 3 else line.strip()
        if file_part.endswith(".ws_lock.json") or file_part == ".ws_lock.json":
            continue
        dirty.append(file_part)
    return dirty


def finalize_sub_effort(
    effort_id: str,
    lease_token: str,
    conclusion_md: str,
    merge_to_effort: bool = True,
    db_path: Path | None = None,
) -> dict[str, Any]:
    """
    Finalize a Tier-3 Sub-Effort (or Tier-2 Effort):
    1. Verify lease_token and clean working tree (ignoring .ws_lock.json).
    2. Capture HEAD SHA.
    3. Run tree-sitter AST extraction on diff vs parent branch and index conclusion + AST into RAG.
    4. Roll up conclusion, metrics, Git SHA, and AST symbols into parent Effort's children_rollup_md.
    5. Optionally merge --no-ff into parent Effort worktree & trigger Paparats :9876/trigger.
    6. Auto-prune Tier-3 worktree directory while preserving branch ws/<sub_id> in Git.
    """
    verify_and_refresh_lease(effort_id, lease_token, db_path=db_path)
    effort = get_effort(effort_id, db_path=db_path)
    if effort is None:
        raise KeyError(f"Effort '{effort_id}' not found.")

    ws_id = effort["workstream_id"]
    repo_path = Path(effort["repo_path"])
    wt_path_str = effort["worktree_path"]

    if wt_path_str and Path(wt_path_str).exists():
        dirty_files = get_dirty_files(wt_path_str)
        if dirty_files:
            raise DirtyWorktreeError(
                f"Cannot finalize '{effort_id}': working tree has uncommitted files: "
                f"{', '.join(dirty_files)}"
            )
        head_sha = _get_head_sha(wt_path_str)
    else:
        proc = _run_git(["rev-parse", effort["git_branch"]], cwd=repo_path, check=True)
        head_sha = proc.stdout.strip()

    if effort["tier"] == 2:
        # Tier-2 Effort finalization
        ast_symbols = []
        if wt_path_str and Path(wt_path_str).exists():
            ast_symbols = extract_and_index_git_diff_ast(
                workstream_id=ws_id,
                effort_id=effort_id,
                repo_path=repo_path,
                worktree_path=wt_path_str,
                base_ref=effort["base_branch"],
                head_sha=head_sha,
                db_path=db_path,
            )
        conclusion_path = get_docs_dir() / ws_id / effort_id / "CONCLUSION.md"
        conclusion_path.parent.mkdir(parents=True, exist_ok=True)
        conclusion_path.write_text(conclusion_md, encoding="utf-8")

        index_document_chunks(
            ws_id=ws_id,
            effort_id=effort_id,
            doc_type="POC_CONCLUSION",
            file_path=str(conclusion_path),
            markdown_text=conclusion_md,
            git_sha=head_sha,
            replace_existing_for_type=True,
            db_path=db_path,
        )
        conn = get_connection(db_path)
        try:
            conn.execute(
                """
                UPDATE efforts
                SET conclusion_md = ?,
                    latest_git_sha = ?,
                    phase_tag = 'DONE',
                    updated_at = datetime('now')
                WHERE id = ?
                """,
                (conclusion_md, head_sha, effort_id),
            )
        finally:
            conn.close()
        release_lease(effort_id, db_path=db_path)
        return {
            "status": "FINALIZED",
            "effort_id": effort_id,
            "tier": 2,
            "git_sha": head_sha,
            "ast_symbols_extracted": [s["symbol_name"] for s in ast_symbols],
            "worktree_pruned": False,
        }

    # Tier-3 Sub-Effort finalization
    parent_id = effort["parent_effort_id"]
    parent = get_effort(parent_id, db_path=db_path)
    if parent is None:
        raise KeyError(f"Parent effort '{parent_id}' not found.")

    parent_branch = parent["git_branch"]
    ast_symbols = []
    if wt_path_str and Path(wt_path_str).exists():
        ast_symbols = extract_and_index_git_diff_ast(
            workstream_id=ws_id,
            effort_id=effort_id,
            repo_path=repo_path,
            worktree_path=wt_path_str,
            base_ref=parent_branch,
            head_sha=head_sha,
            db_path=db_path,
        )

    rollup_info = append_child_rollup(
        parent_effort_id=parent_id,
        sub_effort_id=effort_id,
        title=effort["title"],
        conclusion_md=conclusion_md,
        git_sha=head_sha,
        ast_symbols=ast_symbols,
        merged=merge_to_effort,
        db_path=db_path,
    )

    # Index sub-effort conclusion and updated parent sibling rollup into Hybrid RAG
    index_document_chunks(
        ws_id=ws_id,
        effort_id=effort_id,
        doc_type="POC_CONCLUSION",
        file_path=rollup_info["sub_doc_path"],
        markdown_text= f"## {effort['title']} (`{effort_id}`)\n{conclusion_md}",
        git_sha=head_sha,
        replace_existing_for_type=True,
        db_path=db_path,
    )
    index_document_chunks(
        ws_id=ws_id,
        effort_id=parent_id,
        doc_type="SIBLING_ROLLUP",
        file_path=rollup_info["rollup_path"],
        markdown_text=rollup_info["children_rollup_md"],
        git_sha=head_sha,
        replace_existing_for_type=True,
        db_path=db_path,
    )

    merged_ok = False
    paparats_trigger = None
    if merge_to_effort and parent.get("worktree_path"):
        parent_wt = Path(parent["worktree_path"])
        if parent_wt.exists():
            _run_git(
                [
                    "merge",
                    "--no-ff",
                    effort["git_branch"],
                    "-m",
                    f"Merge sub-effort {effort_id}",
                ],
                cwd=parent_wt,
                check=True,
            )
            parent_head = _get_head_sha(parent_wt)
            conn = get_connection(db_path)
            try:
                conn.execute(
                    "UPDATE efforts SET latest_git_sha = ?, updated_at = datetime('now') WHERE id = ?",
                    (parent_head, parent_id),
                )
            finally:
                conn.close()
            merged_ok = True
            paparats_trigger = trigger_paparats_index(
                effort_id=parent_id, workstream_id=ws_id
            )

    # Step 6: Auto-Prune Tier-3 worktree while keeping branch ws/{sub_id} intact
    if wt_path_str:
        _remove_lock_file(wt_path_str)
        wt_path = Path(wt_path_str)
        if wt_path.exists():
            _run_git(
                ["worktree", "remove", str(wt_path), "--force"],
                cwd=repo_path,
                check=False,
            )
            if wt_path.exists():
                shutil.rmtree(wt_path, ignore_errors=True)
            _run_git(["worktree", "prune"], cwd=repo_path, check=False)

    conn = get_connection(db_path)
    try:
        conn.execute(
            """
            UPDATE efforts
            SET worktree_pruned = 1,
                worktree_path = NULL,
                phase_tag = 'DONE',
                latest_git_sha = ?,
                conclusion_md = ?,
                lease_owner = NULL,
                lease_token = NULL,
                lease_acquired_at = NULL,
                lease_expires_at = NULL,
                updated_at = datetime('now')
            WHERE id = ?
            """,
            (head_sha, conclusion_md, effort_id),
        )
    finally:
        conn.close()

    return {
        "status": "FINALIZED",
        "effort_id": effort_id,
        "parent_effort_id": parent_id,
        "tier": 3,
        "git_branch": effort["git_branch"],
        "git_sha": head_sha,
        "merged_to_effort": merged_ok,
        "worktree_pruned": True,
        "ast_symbols_extracted": [s["symbol_name"] for s in ast_symbols],
        "rollup_path": rollup_info["rollup_path"],
        "paparats_trigger": paparats_trigger,
    }


# Alias for convenience
finalize_effort = finalize_sub_effort


def reopen_worktree(
    sub_id: str,
    client_id: str = "vscode",
    db_path: Path | None = None,
) -> dict[str, Any]:
    """Re-materialize a previously pruned Tier-3 worktree from branch `ws/{sub_id}` and acquire a lease."""
    ensure_workstreams_dirs()
    effort = get_effort(sub_id, db_path=db_path)
    if effort is None:
        raise KeyError(f"Sub-effort '{sub_id}' not found.")

    repo_path = Path(effort["repo_path"])
    branch_name = effort["git_branch"]
    wt_path = get_worktrees_dir() / sub_id
    wt_path.parent.mkdir(parents=True, exist_ok=True)

    if not wt_path.exists():
        _run_git(["worktree", "prune"], cwd=repo_path, check=False)
        _run_git(["worktree", "add", str(wt_path), branch_name], cwd=repo_path, check=True)

    head_sha = _get_head_sha(wt_path)
    conn = get_connection(db_path)
    try:
        conn.execute(
            """
            UPDATE efforts
            SET worktree_path = ?,
                worktree_pruned = 0,
                phase_tag = CASE WHEN phase_tag = 'DONE' THEN 'EXECUTING' ELSE phase_tag END,
                latest_git_sha = ?,
                updated_at = datetime('now')
            WHERE id = ?
            """,
            (str(wt_path), head_sha, sub_id),
        )
    finally:
        conn.close()

    lease = acquire_lease(sub_id, client_id=client_id, steal=True, db_path=db_path)
    return {
        "status": "REOPENED",
        "sub_id": sub_id,
        "git_branch": branch_name,
        "worktree_path": str(wt_path),
        "git_sha": head_sha,
        "lease": lease,
    }


def check_sibling_conflicts(
    effort_or_parent_id: str,
    db_path: Path | None = None,
) -> dict[str, Any]:
    """
    In-memory sibling conflict detection via `git merge-tree --write-tree ws/{sub_a} ws/{sub_b}`.
    Zero working-tree mutation.
    """
    effort = get_effort(effort_or_parent_id, db_path=db_path)
    if effort is None:
        return {"has_conflict": False, "conflicting_files": [], "conflict_pairs": []}

    parent_id = (
        effort["parent_effort_id"]
        if effort["tier"] == 3 and effort["parent_effort_id"]
        else effort["id"]
    )
    repo_path = Path(effort["repo_path"])
    if not (repo_path / ".git").exists():
        return {"has_conflict": False, "conflicting_files": [], "conflict_pairs": []}

    conn = get_connection(db_path)
    try:
        siblings = conn.execute(
            """
            SELECT id, git_branch, phase_tag
            FROM efforts
            WHERE parent_effort_id = ?
              AND phase_tag != 'ABANDONED'
            ORDER BY created_at ASC
            """,
            (parent_id,),
        ).fetchall()
    finally:
        conn.close()

    active_siblings = [
        dict(s) for s in siblings if _branch_exists(repo_path, s["git_branch"])
    ]
    if len(active_siblings) < 2:
        return {"has_conflict": False, "conflicting_files": [], "conflict_pairs": []}

    conflicting_files_set: set[str] = set()
    conflict_pairs: list[dict[str, Any]] = []

    for sub_a, sub_b in itertools.combinations(active_siblings, 2):
        branch_a = sub_a["git_branch"]
        branch_b = sub_b["git_branch"]
        proc = _run_git(
            ["merge-tree", "--write-tree", branch_a, branch_b],
            cwd=repo_path,
            check=False,
        )
        if proc.returncode != 0:
            pair_files: set[str] = set()
            combined_out = f"{proc.stdout}\n{proc.stderr}"
            for line in combined_out.splitlines():
                # Format 1: `<mode> <oid> <stage>\t<path>`
                if "\t" in line:
                    path_part = line.split("\t", 1)[1].strip()
                    if path_part:
                        pair_files.add(path_part)
                # Format 2: `CONFLICT (...): Merge conflict in <path>`
                m = re.search(r"Merge conflict in\s+(.+)$", line.strip())
                if m:
                    pair_files.add(m.group(1).strip())

            conflicting_files_set.update(pair_files)
            conflict_pairs.append(
                {
                    "sub_a": sub_a["id"],
                    "sub_b": sub_b["id"],
                    "branch_a": branch_a,
                    "branch_b": branch_b,
                    "files": sorted(pair_files),
                }
            )

    return {
        "has_conflict": len(conflicting_files_set) > 0 or len(conflict_pairs) > 0,
        "conflicting_files": sorted(conflicting_files_set),
        "conflict_pairs": conflict_pairs,
    }


def get_branch_diff(
    effort_id: str,
    db_path: Path | None = None,
) -> str:
    """
    Return unified git diff for an Effort or Sub-Effort (`git -C {repo_path} diff {base}..{branch}`).
    Works even after a Tier-3 Sub-Effort worktree has been auto-pruned.
    """
    effort = get_effort(effort_id, db_path=db_path)
    if effort is None:
        return ""
    repo_path = Path(effort["repo_path"])
    if not (repo_path / ".git").exists():
        return ""

    base_ref = effort["base_branch"]
    head_ref = effort["git_branch"]
    proc = _run_git(["diff", f"{base_ref}..{head_ref}"], cwd=repo_path, check=False)
    return proc.stdout

