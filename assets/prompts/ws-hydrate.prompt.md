---
mode: agent
description: Hydrate a Workstream Effort or Sub-Effort, acquire an atomic fencing token lease, and display sibling rollups and next steps.
---

Hydrate the requested Workstream Hub Effort or Sub-Effort following the `workstream-protocol` skill:

1. Call `ws_hydrate` for the target `effort_id` (pass `steal=true` only if explicitly requested by the user).
2. If `status == "LOCKED_BY_OTHER_SESSION"`, report the active owner and expiration timestamp and ask the user whether to force-steal (`--steal`).
3. If `status == "HYDRATED"`:
   - Record the `lease_token` (`tok_...`) for all subsequent tool calls in this window.
   - Summarize the current `phase_tag`, `worktree_path`, `sibling_conflicts` status, `children_rollup_md` highlights, and `last_checkpoint.exact_next_step`.
   - Strictly enforce the 350-line Shunt gate (`ws_check_file_gate`, `ws_shunt_read`, `ws_shunt_write`).

