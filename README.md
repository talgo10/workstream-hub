# Workstream Hub (`workstream-hub`)

A deterministic, local multi-session AI orchestration, context-shunting, and hybrid-RAG memory hub (`~/.workstreams`) that coordinates parallel **VS Code Copilot Chat** windows and **Databricks Omnigent (`omni`)** CLI sub-agents across multiple Git repositories.

---

## AI Agent Installation Runbook (macOS)

If you are an AI agent (or developer) tasked with installing `workstream-hub` on a macOS machine, execute the following steps in order.

### Step 1: Verify Prerequisites
Ensure `git` (2.38+) and `uv` are installed and on `PATH`:
```bash
export PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:$PATH"
git --version
uv --version || curl -LsSf https://astral.sh/uv/install.sh | sh
```

### Step 2: Run the 1-Command macOS Installer
From the root of this repository, run:
```bash
make install
```
*(Or directly run `./scripts/install_macos.sh`)*

**What `make install` does automatically:**
1. Installs the global CLI tools (`ws`, `ws-mcp`, `ws-ui`) into `~/.local/bin` via `uv tool install --force --editable .`.
2. Installs the pre-read interceptor hook at `~/.workstreams/bin/check_file_size_hook.py`.
3. Runs `ws init --warm-embedder` to create `~/.workstreams/` (`state.db` in SQLite WAL mode + `sqlite-vec` + `FTS5`, `docs/`, `efforts/`, `worktrees/`, `paparats/`) and pre-warms the local `BAAI/bge-small-en-v1.5` ONNX embedding model.
4. Symlinks the `workstream-protocol` and `spec-griller` skills into `~/.copilot/skills/` and `~/.omnigent/skills/`.
5. Copies `shunt_policy.yaml` into `~/.omnigent/policies/shunt_policy.yaml`.
6. Symlinks `/ws-hydrate`, `/ws-move`, `/ws-grill`, and `/ws-finalize` `.prompt.md` files into `~/Library/Application Support/Code/User/prompts/`.
7. Installs and loads `~/Library/LaunchAgents/com.workstreams.ui.plist` so the Web Cockpit runs automatically in the background at `http://localhost:8765`.

### Step 3: Register the `ws-mcp` Server in VS Code / IDE
Add `workstream-hub` to your VS Code User MCP configuration (`~/Library/Application Support/Code/User/mcp.json`):
```json
{
  "servers": {
    "workstream-hub": {
      "type": "stdio",
      "command": "ws-mcp",
      "env": {
        "SHUNT_MIN_LINES": "350"
      }
    }
  }
}
```
*(If `ws-mcp` is not on the GUI PATH, use `"command": "/Users/<username>/.local/bin/ws-mcp"`).*

### Step 4: Post-Install Verification Checklist
Run these commands to verify the installation is healthy:
```bash
# 1. Verify CLI & MCP entrypoints exist
which ws ws-mcp ws-ui
ws --help

# 2. Run the full 8-part acceptance test suite
make test

# 3. Verify the background Web Cockpit (:8765) is responding
curl -s http://127.0.0.1:8765/api/status
```

### Step 5 (Optional): Start `paparats-mcp` Call-Graph Indexer (`:9876`)
If Docker Desktop or OrbStack is running and you want live AST call-graph indexing alongside `state.db`:
```bash
docker compose -f assets/paparats/docker-compose.yml up -d
```

---

## Core Architecture & Capabilities

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

## Typical End-to-End Workflow

```bash
# 1. Initialize a Workstream (Tier 1)
ws create-ws ws-search --title "Hybrid Search Overhaul" --goal "Sub-10ms local RAG"

# 2. Create an Effort tied to a Git repo (Tier 2 persistent worktree)
ws create-effort eff-vec --ws-id ws-search --title "Vector Indexer" --repo /path/to/repo --base-branch main

# 3. Create a Sub-Effort for a specific POC (Tier 3 ephemeral worktree)
ws create-sub sub-quant --parent eff-vec --title "Int8 Quantization POC" --phase POC

# 4. Hydrate in VS Code or CLI to acquire a fencing lease_token
ws hydrate sub-quant --client-id vscode

# 5. Finalize Sub-Effort (extracts AST diff, updates parent SIBLING_ROLLUP.md, merges, and auto-prunes worktree)
ws finalize sub-quant --lease-token <tok_...> --conclusion "Reduced memory 4x with <0.5% recall drop" --merge
```

---

## CLI (`ws`) & MCP (`ws-mcp`) Reference

| CLI Command | MCP Tool Name | Description |
| :--- | :--- | :--- |
| `ws init` | *(CLI only)* | Creates `~/.workstreams/` dirs, initializes `state.db`, and optionally warms `fastembed`. |
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
