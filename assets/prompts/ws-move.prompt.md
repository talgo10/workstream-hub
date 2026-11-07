---
mode: agent
description: Save a compact <300-token stateless checkpoint and release the session lease so the Effort can be resumed in another window.
---

Park and hand off the active Effort or Sub-Effort for window migration:

1. Gather the current `effort_id` and active `lease_token`.
2. Summarize the session state in `< 300 tokens`:
   - `hypothesis`: Current working hypothesis or objective.
   - `decisions_locked`: List of locked architectural or implementation decisions.
   - `modified_files`: List of files touched in the worktree.
   - `open_blockers`: Any remaining blockers or failing tests.
   - `exact_next_step`: Single concrete next action for the next window.
3. Call `ws_checkpoint` with `release_lease=true`.
4. Output the `/ws-hydrate <effort_id>` command ready to paste into the next window.

