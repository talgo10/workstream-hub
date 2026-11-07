---
mode: agent
description: Interrogate the Effort specification, resolve edge cases, log ADRs, and lock SPEC.md before execution.
---

Run the `spec-griller` workflow on the active Effort:

1. Ensure the Effort is hydrated via `ws_hydrate` and you hold a valid `lease_token`.
2. Query `ws_rag_search` for related specs, ADRs, and sibling POC conclusions across the workstream.
3. Grill the specification for missing edge cases, concurrency/leasing issues, API contracts, and testability.
4. Log finalized architectural choices via `ws_log_decision` and persist the updated spec via `ws_save_spec`.

