# Local Workstream Orchestration Hub — Master Specification & Agent Execution Plan

**Target OS:** macOS (Apple Silicon / Intel)  
**Target Audience:** Autonomous AI Coding Agent / Senior Systems Engineer  
**Package Manager & Runtime:** `uv` (Python 3.12+), Docker Desktop / OrbStack (for `paparats-mcp`), Git 2.38+  
**Primary Goal:** Build an easily installable, deterministic, local multi-session AI orchestration, context-shunting, and hybrid-RAG memory hub (`~/.workstreams`) that coordinates parallel **VS Code Copilot Chat** windows and **Databricks Omnigent (`omni`)** CLI sub-agents across multiple Git repositories.

---

## 1. System Architecture & Core Design Principles

1. **Strict Decoupling of Memory & Code:** All Workstream state, specs, ADR decisions, checkpoints, and RAG vectors live in `~/.workstreams/`, completely outside source code repositories. Source repositories are referenced strictly via `repo_path`, `worktree_path`, `git_branch`, and `git_sha`.
2. **Fixed 3-Tier Hierarchy with Sibling Inheritance:**
   * **Tier 1: `Workstream`** (Jira Epic equivalent; can span multiple repos).
   * **Tier 2: `Effort`** (Feature / Umbrella effort tied to **1 Repo** and **1 Persistent Integration Worktree** at `~/.workstreams/efforts/<effort_id>`).
   * **Tier 3: `Sub-Effort`** (Atomic unit of work / POC tied to **1 Ephemeral Worktree** at `~/.workstreams/worktrees/<sub_id>` that branches off the parent Effort branch and is **auto-pruned** upon finalization).
   * **Bottom-Up Sibling Rollup:** When a Tier-3 `Sub-Effort` is finalized, its structured conclusion, metrics, Git SHA, and extracted AST signatures automatically roll up into the parent Tier-2 `Effort.children_rollup_md`. Subsequent sibling sub-efforts automatically receive this rollup on hydration.
3. **Stateless Windows + Atomic Session Leasing (Fencing Tokens):**
   * VS Code Copilot Chat windows are treated as stateless compute clients.
   * Only one session can hold an active lease on an Effort/Sub-Effort at a time (`BEGIN IMMEDIATE` SQLite lock + monotonic `lease_token` with 60-minute sliding TTL).
   * Moving a session between windows uses a `< 300 token` structured checkpoint (`ws checkpoint`) or explicit lease steal (`ws hydrate <id> --steal`), invalidating stale tokens from previous windows.
4. **Dual-Tier Hybrid RAG (Ephemeral/Docs + Live Call Graph):**
   * **Engine 1 (`state.db` — Single-File SQLite WAL + `FTS5` + `sqlite-vec` + `fastembed`):** Indexes all Markdown Specs (`SPEC.md`), ADR Decisions (`DECISIONS.md`), Sibling Rollups (`SIBLING_ROLLUP.md`), POC conclusions (including rejected/unmerged POCs), and `tree-sitter` diff AST signatures. Uses Heading-Aware Chunking (`##` / `###`) and **Reciprocal Rank Fusion (RRF)** combining BM25 keyword search (`FTS5`) and 384-dim semantic vectors (`BAAI/bge-small-en-v1.5` via local ONNX `fastembed`).
   * **Engine 2 (`paparats-mcp` on `http://localhost:9876`):** Indexes canonical repositories AND active **Tier-2 Effort Integration Worktrees** via `projects.yml`. Provides AST `search_code`, `find_usages`, `change_impact` (call-graph blast radius), Git history, and Architectural Memory (`decisions`/`lessons`). Triggered on-demand via `POST http://localhost:9876/trigger`.
5. **The Local "Shunt" Context-Protection Layer (82–94% Token Savings):**
   * Inspired by Spotify's `shunt` architecture, the main orchestrator window is strictly protected from context bloat caused by reading large files or generating repetitive boilerplate:
   * **350-Line Hard Gate (`SHUNT_MIN_LINES = 350`):** Full-file reads on files `> 350 lines` without explicit `offset`/`limit` ranges are intercepted/blocked by Omnigent policy (`shunt_policy.yaml`) and the `workstream-protocol` skill.
   * **Tiered Redirection:** Agents must first query `paparats-mcp` (`search_code` / `find_usages`) for AST-scoped retrieval. When raw bulk file or log inspection is required, agents call `ws_shunt_read` (`bulk-reader` mode), which uses an ephemeral, low-cost worker call (`temp=0.2`) to read the files out-of-band and return **only concise, structured bullet points** (`< 250 tokens`) to the main session.
   * **Out-of-Band Boilerplate Writer (`ws_shunt_write` / `code-writer` mode):** Repetitive code/test generation is shunted to an ephemeral worker using `--reference <existing_file>` (to match local repo conventions) and `--target <output_file>` (writing directly to the worktree disk without flooding the main chat transcript).
6. **Deterministic Docs Routing & Two-Way Sync:**
   * Agents NEVER invent file paths or write design docs inside Git repos. Agents call MCP/CLI tools (`ws_save_spec`, `ws_log_decision`, `ws_finalize`), which atomically write to `state.db` AND mirror clean Markdown files into `~/.workstreams/docs/<ws_id>/<effort_id>/`.
   * If a human edits a mirrored `.md` file in VS Code, an `mtime` check automatically syncs changes back into `state.db` and re-indexes modified heading chunks.
7. **Unified "Back to Work" Web Cockpit (`http://localhost:8765`):**
   * FastAPI + HTMX + Tailwind + Mermaid.js + **Diff2Html** single-process dashboard.
   * Displays: Delta Brief since last active, 3-Tier Kanban swimlanes, Shunt token-savings telemetry, `git merge-tree` + Paparats `change_impact` sibling conflict radar, side-by-side branch diff drawer (Diff2Html), `vscode://file/...` 1-click worktree launchers, and embedded views for Omnigent (`:6767`) and Paparats Map (`:9876/ui/map`).

---

## 2. Target Filesystem & Repository Structure

The AI coding agent implementing this project MUST create an installable repository (`workstream-hub`) that installs into and manages `~/.workstreams/`:

```text
workstream-hub/                          # Source repository to be implemented
├── pyproject.toml                       # uv package definition (`ws` CLI + `ws-mcp` + `ws-ui`)
├── Makefile                             # 1-command macOS installer (`make install`, `make test`)
├── scripts/
│   ├── install_macos.sh                 # Sets up ~/.workstreams, symlinks skills/prompts, LaunchAgents
│   ├── check_file_size_hook.py          # Pre-read interceptor enforcing SHUNT_MIN_LINES (350 lines)
│   └── com.workstreams.ui.plist         # macOS LaunchAgent for localhost:8765 background UI
├── assets/
│   ├── skills/
│   │   ├── workstream-protocol/SKILL.md # Includes Shunt 350-line gate & routing rules
│   │   └── spec-griller/SKILL.md
│   ├── prompts/
│   │   ├── ws-hydrate.prompt.md
│   │   ├── ws-move.prompt.md
│   │   ├── ws-grill.prompt.md
│   │   └── ws-finalize.prompt.md
│   ├── omnigent/
│   │   └── shunt_policy.yaml            # Omnigent policy gating >350 line reads to ws_shunt_read
│   └── paparats/
│       └── docker-compose.yml           # Local paparats-mcp (:9876) + Qdrant setup
├── src/workstream_hub/
│   ├── __init__.py
│   ├── config.py                        # Paths & env vars (WORKSTREAMS_HOME, SHUNT_MIN_LINES=350)
│   ├── db.py                            # SQLite WAL + Leasing/Fencing Tokens + 3-Tier CRUD
│   ├── indexer.py                       # Heading-aware MD chunker + fastembed + sqlite-vec + FTS5 RRF
│   ├── ast_extractor.py                 # tree-sitter git-diff function/class signature extractor
│   ├── worktree_mgr.py                  # Tier-2 Effort & Tier-3 Sub-Effort git worktree + merge-tree
│   ├── shunt.py                         # Local Shunt Engine: check_file_gate, bulk_reader, code_writer
│   ├── paparats_client.py               # Manages projects.yml, POST :9876/trigger, & proxy queries
│   ├── omni_bridge.py                   # Spawns Databricks Omnigent (`omni`) CLI sub-agents
│   ├── cli_mcp.py                       # Unified Typer CLI (`ws`) + FastMCP Server (`ws-mcp`)
│   └── ui_server.py                     # FastAPI + HTMX + Diff2Html + Mermaid Web Cockpit (:8765)
└── tests/
    ├── conftest.py
    ├── test_leasing_concurrency.py      # Verifies atomic leases, fencing tokens, and --steal
    ├── test_hierarchy_and_rollup.py     # Verifies 3-tier constraint & child->parent sibling rollup
    ├── test_hybrid_rag_and_sync.py      # Verifies heading chunker, FTS5+vec RRF, & 2-way mtime sync
    ├── test_worktree_lifecycle.py       # Verifies Tier-2/Tier-3 worktrees, AST diff, & auto-prune
    └── test_shunt_engine.py             # Verifies 350-line gate, bulk_reader, & code_writer
```

### Runtime Directory Layout Created on macOS (`~/.workstreams/`)
```text
~/.workstreams/
├── state.db                             # Single-file SQLite (WAL + FTS5 + sqlite-vec)
├── paparats/
│   └── projects.yml                     # Auto-managed by worktree_mgr.py for paparats-mcp (:9876)
├── docs/                                # Mirrored Markdown hierarchy (2-way synced with state.db)
│   └── <ws_id>/
│       ├── WORKSTREAM_OVERVIEW.md
│       └── <effort_id>/
│           ├── SPEC.md
│           ├── DECISIONS.md
│           ├── SIBLING_ROLLUP.md
│           └── subs/
│               └── <sub_id>.md
├── efforts/                             # Tier-2 Persistent Integration Worktrees (branch: ws/<effort_id>)
│   └── <effort_id>/
└── worktrees/                           # Tier-3 Ephemeral Sub-Effort Worktrees (auto-pruned on finalize)
    └── <sub_id>/
```

---

## 3. Exact Database Schema (`src/workstream_hub/db.py`)

The SQLite connection MUST initialize `sqlite_vec` and execute the following DDL on every connection startup:

```sql
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
```

---

## 4. Detailed Component Specifications

### 4.1 Atomic Session Leasing & Fencing Tokens (`db.py`)
* **`acquire_lease(effort_id: str, client_id: str, steal: bool = False, ttl_minutes: int = 60) -> dict`**:
  1. Executes `BEGIN IMMEDIATE` to serialize concurrent connection attempts.
  2. Checks if `lease_owner IS NOT NULL AND lease_expires_at > datetime('now')`.
  3. If locked by a different `client_id` and `steal=False`, rolls back and returns:
     `{"status": "LOCKED_BY_OTHER_SESSION", "active_owner": ..., "expires_at": ...}`.
  4. Otherwise, generates `new_token = f"tok_{uuid.uuid4().hex[:10]}"`, updates `lease_owner`, `lease_token`, `lease_acquired_at = datetime('now')`, `lease_expires_at = datetime('now', f'+{ttl_minutes} minutes')`, and writes `.ws_lock.json` inside `worktree_path` (if `worktree_path` exists on disk) and appends `.ws_lock.json` to `<worktree>/.git/info/exclude` (or gitdir exclude).
* **`verify_and_refresh_lease(effort_id: str, lease_token: str) -> None`**:
  1. Every mutating tool (`ws_checkpoint`, `ws_save_spec`, `ws_log_decision`, `ws_finalize`, `ws_shunt_write`) MUST validate that `efforts.lease_token == lease_token`.
  2. If mismatched, raise `LeaseFencingError("Stale or invalid lease_token. Another session took over this item.")`.
  3. If valid, extend `lease_expires_at` by `+60 minutes`.

### 4.2 Heading-Aware Markdown Chunker & Hybrid RRF Search (`indexer.py`)
* **Local Embedding Model:** Initialize `fastembed.TextEmbedding(model_name="BAAI/bge-small-en-v1.5")` as a lazy singleton.
* **Markdown Heading Splitter (`chunk_markdown_by_headers`)**:
  * Splits any Markdown document on `^#{1,3}\s+(.+)$`.
  * Prepends a deterministic lineage prefix to every chunk before embedding and FTS indexing:
    `[Workstream: {ws_id} | Effort: {effort_id} | Type: {doc_type} | Section: {header}]\n{body}`
* **Two-Way Disk Mirror & `mtime` Sync (`sync_docs_from_disk`)**:
  * Mirror paths:
    * Tier-2 Spec: `~/.workstreams/docs/{ws_id}/{effort_id}/SPEC.md`
    * Tier-2 Decisions: `~/.workstreams/docs/{ws_id}/{effort_id}/DECISIONS.md`
    * Tier-2 Sibling Rollup: `~/.workstreams/docs/{ws_id}/{effort_id}/SIBLING_ROLLUP.md`
    * Tier-3 Sub-Effort Conclusion: `~/.workstreams/docs/{ws_id}/{parent_id}/subs/{sub_id}.md`
  * Before `ws_hydrate` or `ws_search`, check if `SPEC.md` exists on disk with `os.path.getmtime(path) > effort.spec_file_mtime`. If newer on disk, read the file into `efforts.spec_md`, update `spec_file_mtime`, and re-index `SPEC` chunks in `doc_chunks`, `doc_chunks_fts`, and `doc_chunks_vec`.
* **Reciprocal Rank Fusion (`hybrid_search`)**:
  * Accepts `query: str`, `workstream_id: str | None`, `effort_id: str | None`, `scope: Literal["ALL", "DOCS_AND_DECISIONS", "SIBLINGS_ONLY", "CODE_ONLY"] = "ALL"`, `top_k: int = 5`.
  * Runs query against `doc_chunks_vec` (cosine/L2 distance top 20) and `doc_chunks_fts` (`BM25` top 20).
  * Computes RRF score for each `chunk_id`:
    $$\text{RRF\_Score}(d) = \frac{1}{60 + \text{rank}_{\text{vec}}(d)} + \frac{1}{60 + \text{rank}_{\text{fts}}(d)}$$
  * Filters by `workstream_id` / `effort_id` / `doc_type` and returns the top-$K$ chunks with metadata.

### 4.3 Two-Tier Git Worktree Manager & AST Diff Extractor (`worktree_mgr.py` & `ast_extractor.py`)
* **Tier-2 Effort Creation (`create_effort_worktree`)**:
  * Creates branch `ws/{effort_id}` from `base_branch` in `repo_path`.
  * Checks out persistent worktree at `~/.workstreams/efforts/{effort_id}`.
  * Registers `~/.workstreams/efforts/{effort_id}` in `~/.workstreams/paparats/projects.yml` under group `{workstream_id}` and fires non-blocking `POST http://localhost:9876/trigger`.
* **Tier-3 Sub-Effort Creation (`create_sub_effort_worktree`)**:
  * Creates branch `ws/{sub_id}` branching off the parent Effort's branch (`ws/{parent_effort_id}`).
  * Checks out ephemeral worktree at `~/.workstreams/worktrees/{sub_id}`.
* **Zero-Token AST Extraction (`ast_extractor.py`)**:
  * Uses `tree-sitter-language-pack` (supporting Python, Rust, Scala, SQL, TypeScript, Go, Java).
  * On `ws finalize`, runs `git -C <worktree_path> diff --name-only ws/{parent_effort_id}..HEAD`.
  * Parses modified files into ASTs and extracts top-level `function_definition`, `class_definition`, `function_item`, `struct_item`, `trait_item`, `impl_item` nodes (truncating bodies > 50 lines while preserving signatures and docstrings).
  * Stores extracted nodes in `code_artifacts` and indexes them into `doc_chunks` (`doc_type='CODE_AST'`).
* **Sub-Effort Finalization & Auto-Prune (`finalize_sub_effort`)**:
  1. Verifies clean working tree (`git status --porcelain` must be empty, ignoring `.ws_lock.json`). Raises error if dirty.
  2. Captures `HEAD` SHA (`latest_git_sha`).
  3. Runs `ast_extractor` on changed files and indexes `conclusion_md` + AST chunks into `state.db`.
  4. Appends structured block to parent Effort's `children_rollup_md` and mirrors to `SIBLING_ROLLUP.md` + `subs/{sub_id}.md`.
  5. If `merge_to_effort=True`: runs `git -C ~/.workstreams/efforts/{parent_id} merge --no-ff ws/{sub_id} -m "Merge sub-effort {sub_id}"` and calls `POST http://localhost:9876/trigger` so Paparats updates the live call graph.
  6. **Auto-Prune:** Removes `.ws_lock.json`, runs `git -C <repo_path> worktree remove ~/.workstreams/worktrees/{sub_id} --force`, updates `worktree_pruned = 1`, `worktree_path = NULL`, `phase_tag = 'DONE'`, and releases the session lease.
* **Sibling Conflict Radar (`check_sibling_conflicts`)**:
  * For all active Tier-3 sibling branches under the same `parent_effort_id`, runs `git -C <repo_path> merge-tree --write-tree ws/{sub_a} ws/{sub_b}` (Git 2.38+ in-memory merge check).
  * Returns `{"has_conflict": bool, "conflicting_files": list[str]}` with zero working-tree mutation.

### 4.4 The Local Shunt Engine (`src/workstream_hub/shunt.py`)
Implements the local equivalent of Spotify's `shunt` plugin with zero external enterprise dependencies:
* **`check_file_gate(file_path: str, offset: int | None = None, limit: int | None = None) -> dict`**:
  * Reads `SHUNT_MIN_LINES` from `config.py` (default `350`).
  * If `offset` and `limit` are provided (`limit <= 350`), returns `{"allowed": True}`.
  * Counts lines in `file_path`. If `line_count <= SHUNT_MIN_LINES`, returns `{"allowed": True, "line_count": line_count}`.
  * If `line_count > SHUNT_MIN_LINES`, logs a `GATE_BLOCKED` entry in `shunt_logs` and returns:
    ```json
    {
      "allowed": false,
      "line_count": 840,
      "threshold": 350,
      "directive": "BLOCKED: File exceeds 350 lines. Use paparats-mcp `search_code` for AST symbols, pass explicit `offset`/`limit` ranges, or call `ws_shunt_read` to summarize out-of-band."
    }
    ```
* **`shunt_bulk_read(files: list[str], question: str, effort_id: str | None = None) -> dict` (`bulk-reader` mode)**:
  * Reads all target files from disk, counts total lines, and extracts relevant sections:
    1. **Worker Execution:** If `SHUNT_WORKER_CMD` is configured (defaults to headless `omni run --fast --temp 0.2` or `gh copilot`) or an local/OpenAI-compatible endpoint is set, invokes the fast worker model at `temperature=0.2` with system prompt: *"You are a factual bulk-reader worker. Answer the question using ONLY concise bullet points with exact file:line citations. Max 250 tokens."*
    2. **Deterministic AST + BM25 Fallback (Zero-LLM Fallback):** If no worker LLM CLI is active, runs `tree-sitter` symbol extraction + local `fastembed` chunk ranking over the target files and returns the top-5 matching symbol signatures and line ranges (`file:start_line-end_line`).
  * Logs `raw_lines_processed` and `estimated_tokens_saved` (`max(0, total_lines * 8 - response_tokens)`) to `shunt_logs`.
* **`shunt_code_write(target_file: str, spec_prompt: str, reference_file: str | None = None, effort_id: str = "", lease_token: str = "") -> dict` (`code-writer` mode)**:
  * Verifies `verify_and_refresh_lease(effort_id, lease_token)`.
  * Reads `reference_file` (if provided) so the worker strictly mirrors imports, naming, type annotations, and test conventions.
  * Invokes the background worker (`omni` / configured worker command) to generate and write the code directly to `target_file` inside the effort's worktree.
  * Logs `estimated_tokens_saved` to `shunt_logs` and returns ONLY a compact summary to the main window: `{"status": "WRITTEN", "target_file": target_file, "lines_written": N, "ast_symbols_created": [...]}`.

---

## 5. Deterministic CLI (`ws`) & MCP Server (`ws-mcp`) Contract (`cli_mcp.py`)

Both `Typer` CLI commands and `FastMCP` tools MUST call the exact same underlying Python functions and return JSON-serializable dictionaries:

| CLI Command | MCP Tool Name | Parameters | Deterministic Return / Behavior |
| :--- | :--- | :--- | :--- |
| `ws init` | *(CLI only)* | None | Creates `~/.workstreams/` dirs, initializes `state.db`, verifies `fastembed` model cache. |
| `ws create-ws` | `ws_create_workstream` | `ws_id, title, goal` | Creates Tier-1 Workstream + `~/.workstreams/docs/{ws_id}/WORKSTREAM_OVERVIEW.md`. |
| `ws create-effort` | `ws_create_effort` | `effort_id, ws_id, title, repo_path, base_branch="main"` | Creates Tier-2 Effort, persistent worktree at `~/.workstreams/efforts/{effort_id}`, updates Paparats `projects.yml`. |
| `ws create-sub` | `ws_create_sub_effort` | `sub_id, parent_effort_id, title, phase_tag="POC"` | Creates Tier-3 Sub-Effort & worktree at `~/.workstreams/worktrees/{sub_id}` off parent branch. |
| `ws hydrate` | `ws_hydrate` | `effort_id, client_id="vscode", steal=False, rag_query=None` | Syncs disk `mtime` -> Acquires lease (`lease_token`) -> Checks sibling `merge-tree` conflicts -> Returns `<500 token` hydration payload with `children_rollup_md`, `last_checkpoint`, `docs_routing_contract`, and top-3 RAG hits. |
| `ws checkpoint` | `ws_checkpoint` | `effort_id, lease_token, hypothesis, decisions_locked, modified_files, open_blockers, exact_next_step, release_lease=True` | Verifies `lease_token`, saves `<300 token` checkpoint to `session_checkpoints`, optionally releases lease for window handoff. |
| `ws save-spec` | `ws_save_spec` | `effort_id, lease_token, spec_md, phase_tag="SPEC_GRILLING"` | Verifies `lease_token`, writes `spec_md` to DB + `SPEC.md` mirror, chunks by headers, indexes in `FTS5` + `sqlite-vec`. |
| `ws log-decision` | `ws_log_decision` | `effort_id, lease_token, title, rationale_md, scope="EFFORT"` | Verifies `lease_token`, appends ADR to `DECISIONS.md` + DB, indexes in RAG, and if `scope=="CODEBASE"` pushes to `paparats-mcp`. |
| `ws finalize` | `ws_finalize` | `effort_id, lease_token, conclusion_md, merge_to_effort=True` | Verifies clean git status & `lease_token`, extracts AST diff via `tree-sitter`, indexes RAG, updates parent `children_rollup_md`, optionally merges to Effort branch + triggers Paparats `:9876/trigger`, and **auto-prunes** Tier-3 worktree. |
| `ws reopen` | `ws_reopen_worktree` | `sub_id, client_id="vscode"` | Re-materializes a previously pruned Tier-3 worktree from branch `ws/{sub_id}` and acquires a new lease. |
| `ws search` | `ws_rag_search` | `query, workstream_id=None, effort_id=None, scope="ALL", top_k=5` | Runs Hybrid RRF (`sqlite-vec` + `FTS5`) across Specs, ADRs, Rollups, Conclusions, and AST snippets (+ queries Paparats `:9876` if `scope=="ALL"` and Paparats is reachable). |
| `ws check-file` | `ws_check_file_gate` | `file_path, offset=None, limit=None` | Enforces the `SHUNT_MIN_LINES=350` gate; blocks full reads of files >350 lines and directs agent to Paparats AST search or `ws_shunt_read`. |
| `ws shunt-read` | `ws_shunt_read` | `files, question, effort_id=None` | Out-of-band `bulk-reader`: extracts facts/citations (`<250 tokens`) from 1+ large files without loading raw files into the main context window. |
| `ws shunt-write` | `ws_shunt_write` | `effort_id, lease_token, target_file, spec_prompt, reference_file=None` | Out-of-band `code-writer`: generates boilerplate/tests directly to `target_file` modeled after `reference_file`, returning only file stats + AST symbols. |
| `ws delegate` | `ws_delegate_omni` | `sub_id, task_prompt` | Acquires lease for `omni-cli`, spawns headless Databricks `omni` sub-agent inside the sub-effort worktree with callback instructions. |

---

## 6. Unified Web Cockpit Specification (`src/workstream_hub/ui_server.py`)

A single-file FastAPI application (`http://localhost:8765`) using CDN-loaded **HTMX**, **Tailwind CSS**, **Mermaid.js**, and **Diff2Html (`d2h`)**:

1. **`GET /` (Main Cockpit View):**
   * **Top Bar:** Global Hybrid RAG Search input (`hx-get="/ui/search"`), **Shunt Token-Savings Counter** (`SELECT SUM(estimated_tokens_saved) FROM shunt_logs`), and status indicators for SQLite WAL, Paparats (`:9876`), and Omnigent (`:6767`).
   * **Delta Brief Banner:** Lists efforts updated or finalized in the last 12 hours, active session leases, and any sibling conflict warnings (`check_sibling_conflicts`).
   * **Workstream Selector & 3-Tier Board:** Shows each Tier-2 `Effort` as a horizontal swimlane containing its Tier-3 `Sub-Effort` cards grouped by `phase_tag` (`RESEARCH`, `POC`, `SPEC_GRILLING`, `EXECUTING`, `DONE`).
   * **Card Badges & Actions:**
     * Lease Status: `🟢 Active (<owner>)`, `⏸️ Parked (Ready to Hydrate)`, or `✅ Pruned (Branch: ws/<sub_id>)`.
     * Conflict Badge: `🟢 Merge Clean` or `🔴 Conflict (<files>)` via `git merge-tree`.
     * Action Buttons:
       * **`[Open in VS Code]`** (`vscode://file/{worktree_path}`) — or **`[Re-open Worktree]`** (`POST /ui/reopen/{sub_id}`) if pruned.
       * **`[Copy /ws-hydrate]`** (copies `/ws-hydrate {id}` to clipboard).
       * **`[Inspect Diff & Spec]`** (`hx-get="/ui/drawer/{id}"`) — opens slide-over drawer.
2. **`GET /ui/drawer/{effort_id}` (Slide-Over Inspector Drawer):**
   * **Tab 1 — Side-by-Side Git Diff (Diff2Html):** Runs `git -C {repo_path} diff ws/{parent_id}..ws/{sub_id}` (works even after the sub-effort worktree is pruned!) and renders interactive side-by-side HTML diffs via `Diff2HtmlUI`.
   * **Tab 2 — Spec, Decisions & Sibling Rollup:** Renders `SPEC.md`, `DECISIONS.md`, and `SIBLING_ROLLUP.md`.
   * **Tab 3 — Live Harnesses:** Links/iframes to `omni_session_url` (`http://localhost:6767/...`) and Paparats Architecture Map (`http://localhost:9876/ui/map`).

---

## 7. Step-by-Step Implementation Plan for the Coding Agent

Execute these 5 milestones sequentially. **Do not skip writing or running the `pytest` suite at each milestone.**

### Milestone 1: Project Scaffolding & Dependencies
1. Initialize `pyproject.toml` with `uv`:
   * **Dependencies:** `typer>=0.12.0`, `fastmcp>=0.4.0`, `sqlite-vec>=0.1.6`, `fastembed>=0.3.6`, `tree-sitter-language-pack>=0.6.0`, `fastapi>=0.115.0`, `uvicorn>=0.30.0`, `httpx>=0.27.0`, `pyyaml>=6.0.1`, `jinja2>=3.1.4`.
   * **Dev Dependencies:** `pytest>=8.3.0`, `pytest-asyncio>=0.24.0`.
   * **Entrypoints (`[project.scripts]`):**
     * `ws = "workstream_hub.cli_mcp:app"`
     * `ws-mcp = "workstream_hub.cli_mcp:run_mcp"`
     * `ws-ui = "workstream_hub.ui_server:run_ui"`
2. Create `src/workstream_hub/config.py` supporting `WORKSTREAMS_HOME` env override (defaulting to `~/.workstreams` in production and `tmp_path` in `pytest`) and `SHUNT_MIN_LINES = int(os.getenv("SHUNT_MIN_LINES", "350"))`.

### Milestone 2: Core SQLite WAL Engine, Leasing & Hybrid RAG (`db.py` + `indexer.py`)
1. Implement `db.py` with `sqlite-vec` extension loading (`conn.enable_load_extension(True); sqlite_vec.load(conn)`), full schema creation (including `shunt_logs`), 3-tier validation, `acquire_lease`, `verify_and_refresh_lease`, and child-to-parent rollup (`children_rollup_md`).
2. Implement `indexer.py` with `chunk_markdown_by_headers`, 2-way `mtime` disk sync under `~/.workstreams/docs/`, `fastembed` 384-dim vector generation, and `hybrid_search` (RRF over `doc_chunks_vec` + `doc_chunks_fts`).
3. **Run Verification:** `uv run pytest tests/test_leasing_concurrency.py tests/test_hierarchy_and_rollup.py tests/test_hybrid_rag_and_sync.py`.

### Milestone 3: Two-Tier Git Worktrees, `tree-sitter` Extractor, Shunt Engine & Paparats Client (`worktree_mgr.py` + `ast_extractor.py` + `shunt.py` + `paparats_client.py`)
1. Implement `worktree_mgr.py` for Tier-2 (`~/.workstreams/efforts/<id>`) and Tier-3 (`~/.workstreams/worktrees/<id>`) creation, dirty-tree check, optional merge to Tier-2 Effort branch, auto-pruning of Tier-3 directories, `reopen_worktree`, and `git merge-tree` conflict detection.
2. Implement `ast_extractor.py` using `tree-sitter-language-pack` to extract modified function/class/struct definitions from `git diff` with zero LLM usage.
3. Implement `shunt.py` (`check_file_gate`, `shunt_bulk_read`, `shunt_code_write`) and `scripts/check_file_size_hook.py` (executable hook that exits non-zero when an un-ranged file read exceeds 350 lines).
4. Implement `paparats_client.py` to update `~/.workstreams/paparats/projects.yml` and fire `POST http://localhost:9876/trigger` (gracefully degrading if `:9876` is offline).
5. **Run Verification:** `uv run pytest tests/test_worktree_lifecycle.py tests/test_shunt_engine.py`.

### Milestone 4: CLI + MCP Router (`cli_mcp.py`), Skills, Omnigent Policy & Prompts
1. Implement `cli_mcp.py` exposing all 15 commands/tools via both `Typer` and `FastMCP`.
2. Create `assets/skills/workstream-protocol/SKILL.md` (encoding Hydrate-First, `<300-token` Checkpoints, Docs-Routing separation, and the **350-Line Shunt Protocol**: never read full files `>350` lines; use Paparats `search_code`, `offset`/`limit`, or `ws_shunt_read`; offload boilerplate to `ws_shunt_write`), `assets/skills/spec-griller/SKILL.md`, `assets/omnigent/shunt_policy.yaml`, and the 4 `.prompt.md` files (`ws-hydrate`, `ws-move`, `ws-grill`, `ws-finalize`).

### Milestone 5: Unified Web Cockpit (`ui_server.py`) & macOS Installer (`Makefile` + `install_macos.sh`)
1. Implement `ui_server.py` on port `8765` with Kanban swimlanes, Shunt token-savings telemetry badge, Diff2Html drawer, `git merge-tree` badges, and RAG search UI.
2. Create `scripts/install_macos.sh` and `Makefile` (`make install`):
   * Installs the `ws`, `ws-mcp`, and `ws-ui` binaries globally via `uv tool install --editable .`.
   * Runs `ws init` to create `~/.workstreams/`.
   * Symlinks `assets/skills/*` into `~/.copilot/skills/` and `~/.omnigent/skills/`.
   * Copies `assets/omnigent/shunt_policy.yaml` into `~/.omnigent/policies/`.
   * Symlinks `assets/prompts/*.prompt.md` into `~/Library/Application Support/Code/User/prompts/`.
   * Loads `com.workstreams.ui.plist` into `~/Library/LaunchAgents/` so `http://localhost:8765` runs automatically in the background on macOS login.

---

## 8. Acceptance Criteria & Verification Suite (Definition of Done)

The implementation is ONLY complete when all **8 Acceptance Tests** pass deterministically via `uv run pytest -v`:

* **AC-1 (Mutual Exclusion & Fencing Tokens):**
  * *Test:* Session A calls `ws_hydrate("sub-1", client_id="win-1")` and receives `tok_A`. Session B calls `ws_hydrate("sub-1", client_id="win-2", steal=False)` and receives `LOCKED_BY_OTHER_SESSION`. Session B then calls `ws_hydrate("sub-1", client_id="win-2", steal=True)` and receives `tok_B`. Session A attempts `ws_checkpoint("sub-1", lease_token=tok_A, ...)` and fails with `LeaseFencingError`.
* **AC-2 (Fixed 3-Tier Constraint & Bottom-Up Sibling Inheritance):**
  * *Test:* Attempting to create a Tier-4 child under a Tier-3 `Sub-Effort` raises a database constraint error. Finalizing `sub-1` under `eff-1` automatically populates `eff-1.children_rollup_md` and mirrors `SIBLING_ROLLUP.md`. Hydrating sibling `sub-2` under `eff-1` includes `sub-1`'s conclusion, metrics, and Git SHA in its hydration payload.
* **AC-3 (Heading-Aware Hybrid RAG & Two-Way `mtime` Sync):**
  * *Test:* Calling `ws_save_spec` and `ws_log_decision` writes `SPEC.md` and `DECISIONS.md` to `~/.workstreams/docs/...` and indexes each `##` section into `doc_chunks_fts` and `doc_chunks_vec`. Modifying `SPEC.md` directly on disk and calling `ws_rag_search` automatically detects the newer `mtime`, re-indexes the updated section, and retrieves the newly edited text via both semantic query and exact BM25 keyword match.
* **AC-4 (Two-Tier Worktree Lifecycle, Zero-Token AST Extraction & Auto-Prune):**
  * *Test:* Using a temporary local Git repo, creating `eff-1` creates worktree `~/.workstreams/efforts/eff-1` (branch `ws/eff-1`). Creating `sub-1` creates worktree `~/.workstreams/worktrees/sub-1` (branch `ws/sub-1`). Committing a new Python function `def compute_sparse_hash(df): ...` in `sub-1` and running `ws_finalize("sub-1", ..., merge_to_effort=True)`:
    1. Extracts `compute_sparse_hash` into `code_artifacts` via `tree-sitter`.
    2. Merges `ws/sub-1` into `ws/eff-1`.
    3. Deletes the physical directory `~/.workstreams/worktrees/sub-1` (`os.path.exists` is `False`) while keeping branch `ws/sub-1` intact in Git so `git diff ws/eff-1~1..ws/sub-1` still succeeds.
* **AC-5 (Dirty Worktree Protection):**
  * *Test:* If `sub-1` has uncommitted files in its worktree, `ws_finalize` aborts without pruning or indexing and returns a clear error listing the uncommitted files.
* **AC-6 (In-Memory Sibling Conflict Detection):**
  * *Test:* Two parallel sub-efforts (`sub-a` and `sub-b`) modify the same lines of `core.py` on their respective branches. Calling `check_sibling_conflicts("eff-1")` (or rendering the UI card) returns `has_conflict=True` and lists `["core.py"]` without modifying either working tree.
* **AC-7 (macOS CLI, MCP & Web Cockpit Smoke Test):**
  * *Test:* `ws --help` exits 0; FastAPI `TestClient` against `ui_server.app` returns HTTP 200 on `GET /`, `GET /ui/search?q=sparse`, and `GET /ui/drawer/sub-1` (returning valid Diff2Html markup even after `sub-1`'s worktree has been auto-pruned).
* **AC-8 (Shunt 350-Line Hard Gate, `bulk-reader` & `code-writer` Telemetry):**
  * *Test:* Calling `check_file_gate` on a 120-line file returns `allowed=True`. Calling `check_file_gate` on a 500-line file without `offset`/`limit` returns `allowed=False` and logs `GATE_BLOCKED` in `shunt_logs`. Calling `check_file_gate` on the same 500-line file with `offset=10, limit=80` returns `allowed=True`. Calling `ws_shunt_read` on the 500-line file returns a compact `<250 token` summary/symbol map and increments `estimated_tokens_saved` in `shunt_logs`. Calling `ws_shunt_write` with a valid `lease_token` and `reference_file` writes the target file in the worktree and returns extracted AST symbols.