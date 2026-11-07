# Workstream Hub (`workstream-hub`)

A deterministic, local multi-session AI orchestration, context-shunting, and hybrid-RAG memory hub (`~/.workstreams`) that coordinates parallel **VS Code Copilot Chat** windows and **Databricks Omnigent (`omni`)** CLI sub-agents across multiple Git repositories.

---

## Core Capabilities

1. **Strict Decoupling of Memory & Code:** All Workstream state, specs, ADR decisions, checkpoints, and RAG vectors live in `~/.workstreams/`, completely outside source code repositories.
2. **Fixed 3-Tier Hierarchy with Sibling Inheritance:**
   - **Tier 1 (`Workstream`):** Multi-repo Epic (`~/.workstreams/docs/<ws_id>/WORKSTREAM_OVERVIEW.md`).
   - **Tier 2 (`Effort`):** Feature / Umbrella effort tied to 1 Repo and 1 Persistent Integration Worktree (`~/.workstreams/efforts/<effort_id>`).
   - **Tier 3 (`Sub-Effort`):** Atomic POC / task tied to 1 Ephemeral Worktree (`~/.workstreams/worktrees/<sub_id>`) that branches off the parent Effort branch and is **auto-pruned** upon finalization while rolling up its conclusion, metrics, Git SHA, and extracted AST signatures into the parent Effort's `SIBLING_ROLLUP.md`.
3. **Stateless Windows + Atomic Session Leasing (Fencing Tokens):**
   - `BEGIN IMMEDIATE` SQLite WAL lock with monotonic `lease_token` (`tok_...`) and 60-minute sliding TTL.
   - Prevents stale windows from overwriting state (`LeaseFencingError`).
4. **Dual-Tier Hybrid RAG (Reciprocal Rank Fusion):**
   - Heading-aware Markdown chunker (`#` / `##` / `###`) + two-way `mtime` disk sync (`SPEC.md`, `DECISIONS.md`, `SIBLING_ROLLUP.md`).
   - Combines SQLite `FTS5` (BM25 keyword search) + `sqlite-vec` (`BAAI/bge-small-en-v1.5` 384-dim semantic vectors via `fastembed`) using Reciprocal Rank Fusion (RRF), plus `tree-sitter` zero-token AST diff extraction.
5. **Local "Shunt" Context-Protection Layer (350-Line Hard Gate):**
   - Blocks un-ranged full-file reads `> 350 lines` (`SHUNT_MIN_LINES = 350`).
   - Provides out-of-band `ws_shunt_read` (`bulk-reader` mode returning `< 250 token` bullet summaries with `file:start-end` citations) and `ws_shunt_write` (`code-writer` mode generating code/tests directly on disk modeled after `--reference`).
6. **Unified "Back to Work" Web Cockpit (`http://localhost:8765`):**
   - FastAPI + HTMX + Tailwind CSS + Mermaid.js + Diff2Html dashboard with 3-tier Kanban swimlanes, Shunt token-savings counter, `git merge-tree` sibling conflict radar, and side-by-side branch diff inspector (even after Tier-3 worktrees are pruned).

---

## Quick Start (macOS)

### Prerequisites
- macOS (Apple Silicon / Intel)
- `uv` (Python 3.11 / 3.12+)
- `git` 2.38+ (for in-memory `git merge-tree --write-tree`)
- Optional: Docker Desktop / OrbStack (for `paparats-mcp` on `:9876`)

### 1-Command Installation
```bash
make install
```
This installs `ws`, `ws-mcp`, and `ws-ui`, initializes `~/.workstreams/state.db`, symlinks skills/prompts into VS Code and Omnigent, and loads the background LaunchAgent for `http://localhost:8765`.

### Running Tests
```bash
make test
# or directly:
uv run pytest -v
```

---

## CLI (`ws`) & MCP (`ws-mcp`) Reference

| CLI Command | MCP Tool Name | Description |
| :--- | :--- | :--- |
| `ws init` | *(CLI only)* | Creates `~/.workstreams/` dirs and initializes `state.db`. |
| `ws create-ws` | `ws_create_workstream` | Creates Tier-1 Workstream + `WORKSTREAM_OVERVIEW.md`. |
| `ws create-effort` | `ws_create_effort` | Creates Tier-2 Effort + persistent worktree at `~/.workstreams/efforts/<id>`. |
| `ws create-sub` | `ws_create_sub_effort` | Creates Tier-3 Sub-Effort + ephemeral worktree at `~/.workstreams/worktrees/<id>`. |
| `ws hydrate` | `ws_hydrate` | Syncs disk `mtime`, acquires fencing `lease_token`, checks sibling conflicts, returns `<500 token` hydration payload. |
| `ws checkpoint` | `ws_checkpoint` | Saves `<300 token` stateless checkpoint and optionally releases lease. |
| `ws save-spec` | `ws_save_spec` | Saves `SPEC.md`, chunks by headings, and indexes into `FTS5` + `sqlite-vec`. |
| `ws log-decision` | `ws_log_decision` | Appends ADR to `DECISIONS.md` + DB and indexes into Hybrid RAG. |
| `ws finalize` | `ws_finalize` | Verifies clean tree, extracts `tree-sitter` AST diff, updates parent `SIBLING_ROLLUP.md`, merges to Effort branch, and auto-prunes Tier-3 worktree. |
| `ws reopen` | `ws_reopen_worktree` | Re-materializes a pruned Tier-3 worktree from branch `ws/<sub_id>`. |
| `ws search` | `ws_rag_search` | Runs Hybrid RRF search (`sqlite-vec` + `FTS5`) across Specs, ADRs, Rollups, Conclusions, and AST snippets. |
| `ws check-file` | `ws_check_file_gate` | Enforces the `SHUNT_MIN_LINES=350` hard gate. |
| `ws shunt-read` | `ws_shunt_read` | Out-of-band `bulk-reader` returning `<250 token` citations/summaries. |
| `ws shunt-write` | `ws_shunt_write` | Out-of-band `code-writer` writing directly to worktree disk modeled after `--reference`. |
| `ws delegate` | `ws_delegate_omni` | Acquires lease for `omni-cli` and delegates task to Databricks Omnigent. |

