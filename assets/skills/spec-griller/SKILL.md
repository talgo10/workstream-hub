---
name: spec-griller
description: Interactive architectural specification interrogation and ADR extraction workflow before transitioning an Effort from SPEC_GRILLING to EXECUTING.
---

# Spec Griller Skill

Use this skill when the user invokes `/ws-grill` or wants to stress-test an Effort specification before implementation.

## Workflow
1. **Hydrate & Load Context:**
   - Call `ws_hydrate(effort_id)` to acquire the `lease_token`, load `spec_md`, `children_rollup_md`, and sibling conflict status.
   - Run `ws_rag_search(query=effort_title, workstream_id=ws_id, scope="ALL")` to check prior specs, rejected POC conclusions, and codebase ADRs.
2. **Adversarial Architecture Review:**
   - Identify ambiguities, missing failure modes, concurrency hazards, schema migration risks, and blast-radius impacts across sibling sub-efforts.
   - Present a concise, prioritized list of 3–5 concrete design questions with recommended defaults.
3. **Lock Decisions & Persist Spec:**
   - For each architectural choice resolved with the user, call `ws_log_decision(effort_id, lease_token, title, rationale_md, scope)`.
   - Write the updated, unambiguous specification using `ws_save_spec(effort_id, lease_token, spec_md, phase_tag="EXECUTING")`.

