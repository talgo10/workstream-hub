"""SQLite WAL Engine, Atomic Session Leasing (Fencing Tokens), and 3-Tier CRUD."""

from __future__ import annotations

import json
import sqlite3
import uuid
from pathlib import Path
from typing import Any

import sqlite_vec

from workstream_hub.config import (
    ensure_workstreams_dirs,
    get_db_path,
    get_docs_dir,
)


class LeaseFencingError(RuntimeError):
    """Raised when an operation presents a stale, missing, or invalid lease_token."""


class HierarchyConstraintError(sqlite3.IntegrityError):
    """Raised when an effort violates the fixed 3-tier hierarchy constraint."""


SCHEMA_DDL = """
PRAGMA journal_mode = WAL;
PRAGMA synchronous = NORMAL;
PRAGMA busy_timeout = 5000;
PRAGMA foreign_keys = ON;

-- 1. Tier 1: Workstreams (Epics)
CREATE TABLE IF NOT EXISTS workstreams (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    goal TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'ACTIVE' CHECK (status IN ('ACTIVE', 'PAUSED', 'ARCHIVED')),
    rolling_summary TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- 2. Tier 2 (Effort) & Tier 3 (Sub-Effort)
CREATE TABLE IF NOT EXISTS efforts (
    id TEXT PRIMARY KEY,
    workstream_id TEXT NOT NULL REFERENCES workstreams(id) ON DELETE CASCADE,
    parent_effort_id TEXT REFERENCES efforts(id) ON DELETE CASCADE,
    tier INTEGER NOT NULL CHECK (tier IN (2, 3)),
    title TEXT NOT NULL,
    phase_tag TEXT NOT NULL DEFAULT 'RESEARCH'
        CHECK (phase_tag IN ('RESEARCH', 'POC', 'SPEC_GRILLING', 'EXECUTING', 'DONE', 'ABANDONED')),
    repo_path TEXT NOT NULL,
    base_branch TEXT NOT NULL DEFAULT 'main',
    git_branch TEXT NOT NULL,
    worktree_path TEXT,                  -- Set to NULL when Tier-3 worktree is auto-pruned
    worktree_pruned INTEGER NOT NULL DEFAULT 0,
    latest_git_sha TEXT,
    spec_md TEXT NOT NULL DEFAULT '',
    conclusion_md TEXT NOT NULL DEFAULT '',
    children_rollup_md TEXT NOT NULL DEFAULT '',
    -- Atomic Session Lease & Fencing Token Columns
    lease_owner TEXT DEFAULT NULL,
    lease_token TEXT DEFAULT NULL,
    lease_acquired_at TEXT DEFAULT NULL,
    lease_expires_at TEXT DEFAULT NULL,
    omni_session_url TEXT DEFAULT NULL,
    -- File Sync Tracking
    spec_file_mtime REAL DEFAULT 0.0,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    CHECK (
        (tier = 2 AND parent_effort_id IS NULL) OR
        (tier = 3 AND parent_effort_id IS NOT NULL)
    )
);

CREATE TRIGGER IF NOT EXISTS enforce_three_tier_hierarchy
BEFORE INSERT ON efforts
FOR EACH ROW
WHEN NEW.parent_effort_id IS NOT NULL
BEGIN
    SELECT RAISE(ABORT, 'Cannot create child under a Tier-3 Sub-Effort: fixed 3-tier constraint')
    WHERE (SELECT tier FROM efforts WHERE id = NEW.parent_effort_id) IS NULL
       OR (SELECT tier FROM efforts WHERE id = NEW.parent_effort_id) != 2;
END;

-- 3. Stateless Session Checkpoints (< 300 tokens)
CREATE TABLE IF NOT EXISTS session_checkpoints (
    id TEXT PRIMARY KEY,
    effort_id TEXT NOT NULL REFERENCES efforts(id) ON DELETE CASCADE,
    actor_type TEXT NOT NULL CHECK (actor_type IN ('VSCODE', 'OMNI_CLI', 'HUMAN_CLI')),
    lease_token TEXT NOT NULL,
    git_sha TEXT,
    hypothesis TEXT NOT NULL DEFAULT '',
    decisions_locked_json TEXT NOT NULL DEFAULT '[]',
    modified_files_json TEXT NOT NULL DEFAULT '[]',
    open_blockers TEXT NOT NULL DEFAULT '',
    exact_next_step TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- 4. Architectural & Effort Decisions (ADRs)
CREATE TABLE IF NOT EXISTS decisions (
    id TEXT PRIMARY KEY,
    workstream_id TEXT NOT NULL REFERENCES workstreams(id) ON DELETE CASCADE,
    effort_id TEXT NOT NULL REFERENCES efforts(id) ON DELETE CASCADE,
    title TEXT NOT NULL,
    rationale_md TEXT NOT NULL,
    scope TEXT NOT NULL DEFAULT 'EFFORT' CHECK (scope IN ('EFFORT', 'WORKSTREAM', 'CODEBASE')),
    git_sha TEXT,
    synced_to_paparats INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- 5. Extracted AST Code Artifacts (Populated via tree-sitter on `ws finalize`)
CREATE TABLE IF NOT EXISTS code_artifacts (
    id TEXT PRIMARY KEY,
    workstream_id TEXT NOT NULL REFERENCES workstreams(id) ON DELETE CASCADE,
    effort_id TEXT NOT NULL REFERENCES efforts(id) ON DELETE CASCADE,
    repo_path TEXT NOT NULL,
    git_sha TEXT NOT NULL,
    file_path TEXT NOT NULL,
    symbol_name TEXT NOT NULL,
    symbol_kind TEXT NOT NULL,           -- 'function' | 'class' | 'struct' | 'trait' | 'impl'
    signature_and_body TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- 6. Shunt Telemetry Log (Tracks offloaded bulk-reads, code-writes & saved tokens)
CREATE TABLE IF NOT EXISTS shunt_logs (
    id TEXT PRIMARY KEY,
    effort_id TEXT REFERENCES efforts(id) ON DELETE SET NULL,
    mode TEXT NOT NULL CHECK (mode IN ('GATE_BLOCKED', 'BULK_READER', 'CODE_WRITER')),
    target_files_json TEXT NOT NULL,
    raw_lines_processed INTEGER NOT NULL DEFAULT 0,
    estimated_tokens_saved INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- 7. Unified Hybrid RAG Chunks (Heading-aware Docs, ADRs, Rollups, Conclusions, & AST Snippets)
CREATE TABLE IF NOT EXISTS doc_chunks (
    chunk_id TEXT PRIMARY KEY,
    workstream_id TEXT NOT NULL,
    effort_id TEXT NOT NULL,
    doc_type TEXT NOT NULL CHECK (
        doc_type IN ('SPEC', 'DECISION_ADR', 'POC_CONCLUSION', 'SIBLING_ROLLUP', 'CODE_AST')
    ),
    file_path TEXT NOT NULL,
    section_header TEXT NOT NULL,
    content_md TEXT NOT NULL,
    git_sha TEXT,
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- 8. SQLite FTS5 Virtual Table (BM25 Keyword Search)
CREATE VIRTUAL TABLE IF NOT EXISTS doc_chunks_fts USING fts5(
    chunk_id UNINDEXED,
    section_header,
    content_md,
    tokenize = 'porter unicode61'
);

-- 9. sqlite-vec Virtual Table (384-dim BAAI/bge-small-en-v1.5 vectors)
CREATE VIRTUAL TABLE IF NOT EXISTS doc_chunks_vec USING vec0(
    chunk_id TEXT PRIMARY KEY,
    embedding float[384]
);
"""


def get_connection(db_path: Path | None = None) -> sqlite3.Connection:
    """Create and initialize a SQLite WAL connection with sqlite-vec loaded."""
    ensure_workstreams_dirs()
    target_path = db_path or get_db_path()
    target_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(target_path), timeout=30.0, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.enable_load_extension(True)
    sqlite_vec.load(conn)
    conn.enable_load_extension(False)
    conn.executescript(SCHEMA_DDL)
    conn.execute("PRAGMA foreign_keys = ON;")
    conn.execute("PRAGMA busy_timeout = 5000;")
    return conn


def init_db(db_path: Path | None = None) -> Path:
    """Initialize state.db and return its path."""
    conn = get_connection(db_path)
    conn.close()
    return db_path or get_db_path()


def _ensure_git_exclude(worktree_path: Path, pattern: str = ".ws_lock.json") -> None:
    """Ensure `.ws_lock.json` is ignored by git in the worktree."""
    git_entry = worktree_path / ".git"
    if not git_entry.exists():
        return
    try:
        if git_entry.is_dir():
            git_dir = git_entry
        else:
            content = git_entry.read_text(encoding="utf-8").strip()
            if content.startswith("gitdir:"):
                raw_dir = content.split("gitdir:", 1)[1].strip()
                git_dir = Path(raw_dir)
                if not git_dir.is_absolute():
                    git_dir = (worktree_path / git_dir).resolve()
            else:
                return

        # Check commondir if inside .git/worktrees/<name>
        info_dirs = [git_dir / "info"]
        commondir_file = git_dir / "commondir"
        if commondir_file.exists():
            rel_common = commondir_file.read_text(encoding="utf-8").strip()
            common_path = (git_dir / rel_common).resolve()
            info_dirs.append(common_path / "info")

        for info_dir in info_dirs:
            info_dir.mkdir(parents=True, exist_ok=True)
            exclude_file = info_dir / "exclude"
            existing = exclude_file.read_text(encoding="utf-8") if exclude_file.exists() else ""
            lines = [line.strip() for line in existing.splitlines()]
            if pattern not in lines:
                prefix = "\n" if existing and not existing.endswith("\n") else ""
                exclude_file.write_text(f"{existing}{prefix}{pattern}\n", encoding="utf-8")
    except OSError:
        pass


def _write_lock_file(worktree_path_str: str | None, payload: dict[str, Any]) -> None:
    if not worktree_path_str:
        return
    wt = Path(worktree_path_str)
    if wt.exists() and wt.is_dir():
        lock_file = wt / ".ws_lock.json"
        lock_file.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        _ensure_git_exclude(wt, ".ws_lock.json")


def _remove_lock_file(worktree_path_str: str | None) -> None:
    if not worktree_path_str:
        return
    wt = Path(worktree_path_str)
    lock_file = wt / ".ws_lock.json"
    if lock_file.exists():
        try:
            lock_file.unlink()
        except OSError:
            pass


def acquire_lease(
    effort_id: str,
    client_id: str,
    steal: bool = False,
    ttl_minutes: int = 60,
    db_path: Path | None = None,
) -> dict[str, Any]:
    """Atomically acquire or steal a session lease with a monotonic fencing token."""
    conn = get_connection(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            """
            SELECT
                id,
                lease_owner,
                lease_token,
                lease_acquired_at,
                lease_expires_at,
                worktree_path,
                CASE
                    WHEN lease_owner IS NOT NULL
                     AND lease_expires_at IS NOT NULL
                     AND lease_expires_at > datetime('now')
                    THEN 1
                    ELSE 0
                END AS is_locked
            FROM efforts
            WHERE id = ?
            """,
            (effort_id,),
        ).fetchone()

        if row is None:
            conn.execute("ROLLBACK")
            raise KeyError(f"Effort '{effort_id}' does not exist.")

        if row["is_locked"] and row["lease_owner"] != client_id and not steal:
            conn.execute("ROLLBACK")
            return {
                "status": "LOCKED_BY_OTHER_SESSION",
                "effort_id": effort_id,
                "active_owner": row["lease_owner"],
                "expires_at": row["lease_expires_at"],
            }

        new_token = f"tok_{uuid.uuid4().hex[:10]}"
        conn.execute(
            """
            UPDATE efforts
            SET lease_owner = ?,
                lease_token = ?,
                lease_acquired_at = datetime('now'),
                lease_expires_at = datetime('now', ?),
                updated_at = datetime('now')
            WHERE id = ?
            """,
            (client_id, new_token, f"+{int(ttl_minutes)} minutes", effort_id),
        )
        updated = conn.execute(
            "SELECT lease_owner, lease_token, lease_acquired_at, lease_expires_at, worktree_path FROM efforts WHERE id = ?",
            (effort_id,),
        ).fetchone()
        conn.execute("COMMIT")

        result = {
            "status": "ACQUIRED",
            "effort_id": effort_id,
            "lease_owner": updated["lease_owner"],
            "lease_token": updated["lease_token"],
            "acquired_at": updated["lease_acquired_at"],
            "expires_at": updated["lease_expires_at"],
        }
        _write_lock_file(updated["worktree_path"], result)
        return result
    except Exception:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()


def verify_and_refresh_lease(
    effort_id: str,
    lease_token: str,
    ttl_minutes: int = 60,
    db_path: Path | None = None,
) -> dict[str, Any]:
    """Validate fencing token and extend lease TTL by +ttl_minutes."""
    if not lease_token:
        raise LeaseFencingError("Stale or invalid lease_token. Another session took over this item.")

    conn = get_connection(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            """
            SELECT
                id,
                lease_owner,
                lease_token,
                lease_expires_at,
                CASE
                    WHEN lease_expires_at IS NOT NULL
                     AND lease_expires_at > datetime('now')
                    THEN 1
                    ELSE 0
                END AS not_expired
            FROM efforts
            WHERE id = ?
            """,
            (effort_id,),
        ).fetchone()

        if row is None:
            conn.execute("ROLLBACK")
            raise KeyError(f"Effort '{effort_id}' does not exist.")

        if row["lease_token"] != lease_token or not row["not_expired"]:
            conn.execute("ROLLBACK")
            raise LeaseFencingError(
                "Stale or invalid lease_token. Another session took over this item."
            )

        conn.execute(
            """
            UPDATE efforts
            SET lease_expires_at = datetime('now', ?),
                updated_at = datetime('now')
            WHERE id = ?
            """,
            (f"+{int(ttl_minutes)} minutes", effort_id),
        )
        updated = conn.execute(
            "SELECT id, lease_owner, lease_token, lease_expires_at FROM efforts WHERE id = ?",
            (effort_id,),
        ).fetchone()
        conn.execute("COMMIT")
        return dict(updated)
    except Exception:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()


def release_lease(effort_id: str, db_path: Path | None = None) -> None:
    """Release an active lease on an effort and clean up `.ws_lock.json`."""
    conn = get_connection(db_path)
    try:
        row = conn.execute(
            "SELECT worktree_path FROM efforts WHERE id = ?", (effort_id,)
        ).fetchone()
        conn.execute(
            """
            UPDATE efforts
            SET lease_owner = NULL,
                lease_token = NULL,
                lease_acquired_at = NULL,
                lease_expires_at = NULL,
                updated_at = datetime('now')
            WHERE id = ?
            """,
            (effort_id,),
        )
        if row and row["worktree_path"]:
            _remove_lock_file(row["worktree_path"])
    finally:
        conn.close()


def create_workstream(
    ws_id: str,
    title: str,
    goal: str,
    status: str = "ACTIVE",
    db_path: Path | None = None,
) -> dict[str, Any]:
    """Create a Tier-1 Workstream and mirror WORKSTREAM_OVERVIEW.md."""
    conn = get_connection(db_path)
    try:
        conn.execute(
            """
            INSERT INTO workstreams (id, title, goal, status)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                title = excluded.title,
                goal = excluded.goal,
                status = excluded.status,
                updated_at = datetime('now')
            """,
            (ws_id, title, goal, status),
        )
        row = conn.execute("SELECT * FROM workstreams WHERE id = ?", (ws_id,)).fetchone()
    finally:
        conn.close()

    ws_docs_dir = get_docs_dir() / ws_id
    ws_docs_dir.mkdir(parents=True, exist_ok=True)
    overview_path = ws_docs_dir / "WORKSTREAM_OVERVIEW.md"
    overview_md = (
        f"# Workstream: {title} (`{ws_id}`)\n\n"
        f"**Status:** {status}\n\n"
        f"## Goal\n\n{goal}\n"
    )
    overview_path.write_text(overview_md, encoding="utf-8")
    res = dict(row)
    res["overview_path"] = str(overview_path)
    return res


def create_effort_record(
    effort_id: str,
    ws_id: str,
    title: str,
    repo_path: str,
    base_branch: str = "main",
    git_branch: str | None = None,
    worktree_path: str | None = None,
    phase_tag: str = "RESEARCH",
    latest_git_sha: str | None = None,
    db_path: Path | None = None,
) -> dict[str, Any]:
    """Create a Tier-2 Effort record in state.db and initialize its docs directory."""
    branch = git_branch or f"ws/{effort_id}"
    conn = get_connection(db_path)
    try:
        conn.execute(
            """
            INSERT INTO efforts (
                id, workstream_id, parent_effort_id, tier, title, phase_tag,
                repo_path, base_branch, git_branch, worktree_path, latest_git_sha
            )
            VALUES (?, ?, NULL, 2, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                effort_id,
                ws_id,
                title,
                phase_tag,
                str(Path(repo_path).resolve()),
                base_branch,
                branch,
                worktree_path,
                latest_git_sha,
            ),
        )
        row = conn.execute("SELECT * FROM efforts WHERE id = ?", (effort_id,)).fetchone()
    finally:
        conn.close()

    effort_docs_dir = get_docs_dir() / ws_id / effort_id
    (effort_docs_dir / "subs").mkdir(parents=True, exist_ok=True)
    for fname in ("SPEC.md", "DECISIONS.md", "SIBLING_ROLLUP.md"):
        fpath = effort_docs_dir / fname
        if not fpath.exists():
            fpath.write_text("", encoding="utf-8")

    return dict(row)


def create_sub_effort_record(
    sub_id: str,
    parent_effort_id: str,
    title: str,
    phase_tag: str = "POC",
    git_branch: str | None = None,
    worktree_path: str | None = None,
    latest_git_sha: str | None = None,
    tier: int = 3,
    db_path: Path | None = None,
) -> dict[str, Any]:
    """Create a Tier-3 Sub-Effort under a Tier-2 Effort, enforcing the 3-tier constraint."""
    branch = git_branch or f"ws/{sub_id}"
    conn = get_connection(db_path)
    try:
        parent = conn.execute(
            "SELECT * FROM efforts WHERE id = ?", (parent_effort_id,)
        ).fetchone()
        if parent is None:
            raise KeyError(f"Parent effort '{parent_effort_id}' does not exist.")
        if parent["tier"] != 2 or tier != 3:
            raise HierarchyConstraintError(
                f"Cannot create child '{sub_id}' (tier={tier}) under '{parent_effort_id}' "
                f"(tier={parent['tier']}): Fixed 3-Tier Hierarchy constraint."
            )

        conn.execute(
            """
            INSERT INTO efforts (
                id, workstream_id, parent_effort_id, tier, title, phase_tag,
                repo_path, base_branch, git_branch, worktree_path, latest_git_sha
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                sub_id,
                parent["workstream_id"],
                parent_effort_id,
                tier,
                title,
                phase_tag,
                parent["repo_path"],
                parent["git_branch"],
                branch,
                worktree_path,
                latest_git_sha,
            ),
        )
        row = conn.execute("SELECT * FROM efforts WHERE id = ?", (sub_id,)).fetchone()
        return dict(row)
    except sqlite3.IntegrityError as exc:
        raise HierarchyConstraintError(str(exc)) from exc
    finally:
        conn.close()


def get_workstream(ws_id: str, db_path: Path | None = None) -> dict[str, Any] | None:
    conn = get_connection(db_path)
    try:
        row = conn.execute("SELECT * FROM workstreams WHERE id = ?", (ws_id,)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def get_effort(effort_id: str, db_path: Path | None = None) -> dict[str, Any] | None:
    conn = get_connection(db_path)
    try:
        row = conn.execute("SELECT * FROM efforts WHERE id = ?", (effort_id,)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def list_workstreams(db_path: Path | None = None) -> list[dict[str, Any]]:
    conn = get_connection(db_path)
    try:
        rows = conn.execute("SELECT * FROM workstreams ORDER BY created_at DESC").fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def list_efforts(
    workstream_id: str | None = None,
    parent_effort_id: str | None = None,
    tier: int | None = None,
    db_path: Path | None = None,
) -> list[dict[str, Any]]:
    conn = get_connection(db_path)
    try:
        clauses: list[str] = []
        params: list[Any] = []
        if workstream_id is not None:
            clauses.append("workstream_id = ?")
            params.append(workstream_id)
        if parent_effort_id is not None:
            clauses.append("parent_effort_id = ?")
            params.append(parent_effort_id)
        if tier is not None:
            clauses.append("tier = ?")
            params.append(tier)
        where_sql = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = conn.execute(
            f"SELECT * FROM efforts {where_sql} ORDER BY created_at ASC",
            tuple(params),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def save_checkpoint(
    effort_id: str,
    lease_token: str,
    hypothesis: str,
    decisions_locked: list[str] | str,
    modified_files: list[str] | str,
    open_blockers: str,
    exact_next_step: str,
    actor_type: str = "VSCODE",
    git_sha: str | None = None,
    release: bool = True,
    db_path: Path | None = None,
) -> dict[str, Any]:
    """Validate lease_token, persist a <300-token stateless checkpoint, and optionally release lease."""
    verify_and_refresh_lease(effort_id, lease_token, db_path=db_path)

    if isinstance(decisions_locked, str):
        try:
            json.loads(decisions_locked)
            decisions_json = decisions_locked
        except json.JSONDecodeError:
            decisions_json = json.dumps([decisions_locked] if decisions_locked else [])
    else:
        decisions_json = json.dumps(list(decisions_locked))

    if isinstance(modified_files, str):
        try:
            json.loads(modified_files)
            files_json = modified_files
        except json.JSONDecodeError:
            files_json = json.dumps([modified_files] if modified_files else [])
    else:
        files_json = json.dumps(list(modified_files))

    ckpt_id = f"ckpt_{uuid.uuid4().hex[:10]}"
    conn = get_connection(db_path)
    try:
        conn.execute(
            """
            INSERT INTO session_checkpoints (
                id, effort_id, actor_type, lease_token, git_sha,
                hypothesis, decisions_locked_json, modified_files_json,
                open_blockers, exact_next_step
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                ckpt_id,
                effort_id,
                actor_type,
                lease_token,
                git_sha,
                hypothesis,
                decisions_json,
                files_json,
                open_blockers,
                exact_next_step,
            ),
        )
        row = conn.execute(
            "SELECT * FROM session_checkpoints WHERE id = ?", (ckpt_id,)
        ).fetchone()
    finally:
        conn.close()

    if release:
        release_lease(effort_id, db_path=db_path)

    res = dict(row)
    res["lease_released"] = release
    return res


def get_last_checkpoint(effort_id: str, db_path: Path | None = None) -> dict[str, Any] | None:
    conn = get_connection(db_path)
    try:
        row = conn.execute(
            """
            SELECT * FROM session_checkpoints
            WHERE effort_id = ?
            ORDER BY created_at DESC, rowid DESC
            LIMIT 1
            """,
            (effort_id,),
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def append_child_rollup(
    parent_effort_id: str,
    sub_effort_id: str,
    title: str,
    conclusion_md: str,
    git_sha: str | None = None,
    ast_symbols: list[dict[str, Any]] | None = None,
    merged: bool = True,
    db_path: Path | None = None,
) -> dict[str, Any]:
    """Roll up a finalized Tier-3 Sub-Effort into its parent Tier-2 Effort's children_rollup_md."""
    conn = get_connection(db_path)
    try:
        parent = conn.execute(
            "SELECT * FROM efforts WHERE id = ?", (parent_effort_id,)
        ).fetchone()
        if parent is None:
            raise KeyError(f"Parent effort '{parent_effort_id}' not found.")

        ws_id = parent["workstream_id"]
        sha_display = git_sha or "N/A"
        symbols_list = ast_symbols or []
        symbol_names = [f"`{s['symbol_name']}` ({s['symbol_kind']} in `{s['file_path']}`)" for s in symbols_list]
        symbols_summary = ", ".join(symbol_names) if symbol_names else "None extracted"

        section_block = (
            f"## Sub-Effort `{sub_effort_id}`: {title}\n"
            f"- **Git SHA:** `{sha_display}`\n"
            f"- **Merged to Effort Branch:** `{merged}`\n"
            f"- **AST Symbols Modified ({len(symbols_list)}):** {symbols_summary}\n\n"
            f"### Conclusion & Metrics\n"
            f"{conclusion_md.strip()}\n"
        )

        existing_rollup = (parent["children_rollup_md"] or "").strip()
        if not existing_rollup:
            new_rollup = f"# Sibling Rollup for Effort `{parent_effort_id}`\n\n{section_block}\n"
        else:
            new_rollup = f"{existing_rollup}\n\n{section_block}\n"

        conn.execute(
            """
            UPDATE efforts
            SET children_rollup_md = ?,
                updated_at = datetime('now')
            WHERE id = ?
            """,
            (new_rollup, parent_effort_id),
        )
        conn.execute(
            """
            UPDATE efforts
            SET conclusion_md = ?,
                latest_git_sha = COALESCE(?, latest_git_sha),
                phase_tag = 'DONE',
                updated_at = datetime('now')
            WHERE id = ?
            """,
            (conclusion_md, git_sha, sub_effort_id),
        )
    finally:
        conn.close()

    effort_docs_dir = get_docs_dir() / ws_id / parent_effort_id
    subs_dir = effort_docs_dir / "subs"
    subs_dir.mkdir(parents=True, exist_ok=True)

    rollup_path = effort_docs_dir / "SIBLING_ROLLUP.md"
    rollup_path.write_text(new_rollup, encoding="utf-8")

    sub_doc_path = subs_dir / f"{sub_effort_id}.md"
    sub_doc_path.write_text(section_block, encoding="utf-8")

    return {
        "parent_effort_id": parent_effort_id,
        "sub_effort_id": sub_effort_id,
        "rollup_path": str(rollup_path),
        "sub_doc_path": str(sub_doc_path),
        "children_rollup_md": new_rollup,
    }


def log_shunt_event(
    mode: str,
    target_files: list[str],
    raw_lines_processed: int = 0,
    estimated_tokens_saved: int = 0,
    effort_id: str | None = None,
    db_path: Path | None = None,
) -> dict[str, Any]:
    """Record a Shunt telemetry event in shunt_logs."""
    log_id = f"shunt_{uuid.uuid4().hex[:10]}"
    conn = get_connection(db_path)
    try:
        # Verify effort_id exists if provided, else store NULL to avoid FK error
        valid_effort_id = None
        if effort_id:
            row = conn.execute("SELECT id FROM efforts WHERE id = ?", (effort_id,)).fetchone()
            if row:
                valid_effort_id = effort_id

        conn.execute(
            """
            INSERT INTO shunt_logs (
                id, effort_id, mode, target_files_json,
                raw_lines_processed, estimated_tokens_saved
            )
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                log_id,
                valid_effort_id,
                mode,
                json.dumps(target_files),
                int(raw_lines_processed),
                int(estimated_tokens_saved),
            ),
        )
        rec = conn.execute("SELECT * FROM shunt_logs WHERE id = ?", (log_id,)).fetchone()
        return dict(rec)
    finally:
        conn.close()


def get_shunt_stats(db_path: Path | None = None) -> dict[str, Any]:
    """Return aggregated Shunt token-savings telemetry."""
    conn = get_connection(db_path)
    try:
        row = conn.execute(
            """
            SELECT
                COUNT(*) AS total_events,
                COALESCE(SUM(CASE WHEN mode = 'GATE_BLOCKED' THEN 1 ELSE 0 END), 0) AS gate_blocks,
                COALESCE(SUM(CASE WHEN mode = 'BULK_READER' THEN 1 ELSE 0 END), 0) AS bulk_reads,
                COALESCE(SUM(CASE WHEN mode = 'CODE_WRITER' THEN 1 ELSE 0 END), 0) AS code_writes,
                COALESCE(SUM(raw_lines_processed), 0) AS total_lines_processed,
                COALESCE(SUM(estimated_tokens_saved), 0) AS total_tokens_saved
            FROM shunt_logs
            """
        ).fetchone()
        return dict(row)
    finally:
        conn.close()

