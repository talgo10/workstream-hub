---
name: workstream-protocol
description: Mandatory orchestration, fencing-token leasing, docs-routing, and 350-line Shunt context-protection protocol for Workstream Hub sessions.
---

# Workstream Protocol Skill

Follow this deterministic protocol in every VS Code Copilot Chat or Databricks Omnigent (`omni`) session attached to a Workstream Hub Effort or Sub-Effort.

## 1. Hydrate-First & Fencing Tokens (`lease_token`)
1. **Always Hydrate First:** Begin every session by calling `ws_hydrate(effort_id, client_id="vscode")` (or `ws hydrate <effort_id>`).
2. **Respect Mutual Exclusion:** If `ws_hydrate` returns `LOCKED_BY_OTHER_SESSION`, stop and ask the user whether to checkpoint the other window or force-steal the lease (`steal=True`).
3. **Preserve Your Fencing Token:** Store the returned `lease_token` (`tok_...`). Every mutating tool (`ws_checkpoint`, `ws_save_spec`, `ws_log_decision`, `ws_finalize`, `ws_shunt_write`) requires a valid `lease_token`.

## 2. The 350-Line Shunt Context-Protection Protocol (`SHUNT_MIN_LINES = 350`)
Never flood the main orchestrator context window with large raw files or repetitive boilerplate:
1. **Hard Gate (`350 lines`):** Before reading an unfamiliar file, call `ws_check_file_gate(file_path, offset, limit)`. Never read a full file `> 350 lines` without explicit `offset` and `limit` (`<= 350`).
2. **Tiered Retrieval:**
   - **Step A (AST Call Graph):** Query `paparats-mcp` (`search_code`, `find_usages`, `change_impact`) or `ws_rag_search(query, scope="CODE_ONLY")` first.
   - **Step B (Out-of-Band Bulk Reader):** When inspecting large files, logs, or multiple modules, call `ws_shunt_read(files=[...], question="...", effort_id=effort_id)`. It summarizes findings into `< 250 tokens` with exact `file:start-end` citations.
3. **Out-of-Band Boilerplate & Test Generation (`ws_shunt_write`):**
   - Offload repetitive code or test scaffolding to `ws_shunt_write(effort_id, lease_token, target_file, spec_prompt, reference_file)`.

## 3. Strict Docs-Routing Separation
1. **Zero Design Docs in Git Repos:** Never create or edit `SPEC.md`, `DECISIONS.md`, or scratch notes inside the source Git repository.
2. **Use Deterministic Tools:**
   - Save specs via `ws_save_spec(effort_id, lease_token, spec_md, phase_tag)`.
   - Record ADRs via `ws_log_decision(effort_id, lease_token, title, rationale_md, scope)`.
   - All docs are automatically mirrored to `~/.workstreams/docs/<ws_id>/<effort_id>/` and indexed into SQLite FTS5 + `sqlite-vec`.

## 4. Stateless Window Handoffs (`< 300 Tokens`) & Finalization
1. **Moving Windows:** Call `ws_checkpoint(effort_id, lease_token, hypothesis, decisions_locked, modified_files, open_blockers, exact_next_step, release_lease=True)` before closing or switching windows.
2. **Finalizing Sub-Efforts:** Ensure `git status --porcelain` is clean, then call `ws_finalize(effort_id, lease_token, conclusion_md, merge_to_effort=True)`. This extracts modified AST signatures via `tree-sitter`, updates the parent Effort's `SIBLING_ROLLUP.md`, merges to the integration branch, and auto-prunes the ephemeral Tier-3 worktree.

