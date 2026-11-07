---
mode: agent
description: Finalize a Sub-Effort or Effort, extract tree-sitter AST signatures, roll up into parent Effort, merge, and auto-prune the worktree.
---

Finalize the active Sub-Effort or Effort:

1. Verify that all changes in the worktree are committed (`git status --porcelain` must be clean, ignoring `.ws_lock.json`).
2. Prepare a structured `conclusion_md` covering key findings, metrics, verification results, and architectural impact.
3. Call `ws_finalize(effort_id, lease_token, conclusion_md, merge_to_effort=true)`.
4. Report the extracted AST symbols, merged commit SHA, updated `SIBLING_ROLLUP.md` path, and confirmation that the ephemeral Tier-3 worktree has been auto-pruned.

