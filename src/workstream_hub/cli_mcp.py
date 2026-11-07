"""Unified Typer CLI (`ws`) and FastMCP Server (`ws-mcp`) for Workstream Hub."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated, Any, Literal

import typer
from fastmcp import FastMCP

from workstream_hub.config import (
    ensure_workstreams_dirs,
    get_docs_dir,
    get_shunt_min_lines,
)
from workstream_hub.db import (
    acquire_lease,
    create_workstream,
    get_effort,
    get_last_checkpoint,
    init_db,
    save_checkpoint,
)
from workstream_hub.indexer import (
    get_embedder,
    hybrid_search,
    log_decision,
    save_spec,
    sync_docs_from_disk,
)
from workstream_hub.omni_bridge import delegate_to_omni
from workstream_hub.paparats_client import query_paparats_search
from workstream_hub.shunt import (
    check_file_gate,
    shunt_bulk_read,
    shunt_code_write,
)
from workstream_hub.worktree_mgr import (
    check_sibling_conflicts,
    create_effort_worktree,
    create_sub_effort_worktree,
    finalize_sub_effort,
    reopen_worktree,
)

app = typer.Typer(
    name="ws",
    help="Workstream Hub: Local multi-session AI orchestration, context-shunting & hybrid-RAG memory hub.",
    no_args_is_help=True,
)
mcp = FastMCP("workstream-hub")


def _emit(payload: dict[str, Any]) -> dict[str, Any]:
    typer.echo(json.dumps(payload, indent=2, default=str))
    return payload


# ============================================================================
# Core Deterministic Tool Functions (Shared by CLI, MCP, and Python callers)
# ============================================================================


def ws_init(warm_embedder: bool = False) -> dict[str, Any]:
    """Initialize ~/.workstreams directories, SQLite WAL state.db, and optional fastembed cache."""
    home = ensure_workstreams_dirs()
    db_file = init_db()
    embedder_ready = False
    if warm_embedder:
        embedder_ready = bool(get_embedder())
    return {
        "status": "INITIALIZED",
        "workstreams_home": str(home),
        "db_path": str(db_file),
        "shunt_min_lines": get_shunt_min_lines(),
        "embedder_warmed": embedder_ready,
    }


def ws_create_workstream(
    ws_id: str,
    title: str,
    goal: str,
) -> dict[str, Any]:
    """Create a Tier-1 Workstream and mirror ~/.workstreams/docs/{ws_id}/WORKSTREAM_OVERVIEW.md."""
    return create_workstream(ws_id=ws_id, title=title, goal=goal)


def ws_create_effort(
    effort_id: str,
    ws_id: str,
    title: str,
    repo_path: str,
    base_branch: str = "main",
) -> dict[str, Any]:
    """Create Tier-2 Effort, persistent worktree at ~/.workstreams/efforts/{effort_id}, and update projects.yml."""
    return create_effort_worktree(
        effort_id=effort_id,
        ws_id=ws_id,
        title=title,
        repo_path=repo_path,
        base_branch=base_branch,
    )


def ws_create_sub_effort(
    sub_id: str,
    parent_effort_id: str,
    title: str,
    phase_tag: str = "POC",
) -> dict[str, Any]:
    """Create Tier-3 Sub-Effort & ephemeral worktree at ~/.workstreams/worktrees/{sub_id} off parent branch."""
    return create_sub_effort_worktree(
        sub_id=sub_id,
        parent_effort_id=parent_effort_id,
        title=title,
        phase_tag=phase_tag,
    )


def ws_hydrate(
    effort_id: str,
    client_id: str = "vscode",
    steal: bool = False,
    rag_query: str | None = None,
) -> dict[str, Any]:
    """
    Sync disk mtime -> Acquire atomic session lease (fencing token) -> Check sibling merge-tree conflicts
    -> Return <500 token hydration payload with children_rollup_md, last_checkpoint, docs_routing_contract, and top-3 RAG hits.
    """
    effort = get_effort(effort_id)
    if effort is None:
        raise KeyError(f"Effort '{effort_id}' does not exist.")

    ws_id = effort["workstream_id"]
    parent_id = effort["parent_effort_id"]
    sync_docs_from_disk(effort_id=effort_id, workstream_id=ws_id)
    if parent_id:
        sync_docs_from_disk(effort_id=parent_id, workstream_id=ws_id)

    lease_res = acquire_lease(effort_id=effort_id, client_id=client_id, steal=steal)
    if lease_res["status"] == "LOCKED_BY_OTHER_SESSION":
        return lease_res

    # Refresh effort after mtime sync & lease acquisition
    effort = get_effort(effort_id) or effort
    parent_effort = get_effort(parent_id) if parent_id else None

    # Bottom-up sibling inheritance: Tier-3 sub-efforts inherit parent's children_rollup_md
    rollup_md = (
        parent_effort["children_rollup_md"]
        if (effort["tier"] == 3 and parent_effort)
        else effort["children_rollup_md"]
    )
    effective_spec_md = (
        effort["spec_md"]
        if effort.get("spec_md")
        else (parent_effort["spec_md"] if parent_effort else "")
    )

    last_ckpt = get_last_checkpoint(effort_id)
    conflicts = check_sibling_conflicts(parent_id or effort_id)

    search_q = rag_query or f"{effort['title']} {effort['phase_tag']}"
    rag_hits = hybrid_search(
        query=search_q,
        workstream_id=ws_id,
        effort_id=effort_id,
        scope="ALL",
        top_k=3,
    )

    target_effort_dir = get_docs_dir() / ws_id / (parent_id or effort_id)
    docs_routing_contract = {
        "rule": "NEVER write design docs inside the git repo. Use ws_save_spec, ws_log_decision, and ws_finalize.",
        "shunt_gate_lines": get_shunt_min_lines(),
        "spec_mirror_path": str(target_effort_dir / "SPEC.md"),
        "decisions_mirror_path": str(target_effort_dir / "DECISIONS.md"),
        "sibling_rollup_mirror_path": str(target_effort_dir / "SIBLING_ROLLUP.md"),
    }

    return {
        "status": "HYDRATED",
        "lease_status": lease_res["status"],
        "effort_id": effort_id,
        "workstream_id": ws_id,
        "parent_effort_id": parent_id,
        "tier": effort["tier"],
        "title": effort["title"],
        "phase_tag": effort["phase_tag"],
        "repo_path": effort["repo_path"],
        "git_branch": effort["git_branch"],
        "worktree_path": effort["worktree_path"],
        "latest_git_sha": effort["latest_git_sha"],
        "lease_owner": lease_res["lease_owner"],
        "lease_token": lease_res["lease_token"],
        "lease_expires_at": lease_res["expires_at"],
        "spec_md": effective_spec_md[:1200],
        "children_rollup_md": rollup_md,
        "last_checkpoint": last_ckpt,
        "sibling_conflicts": conflicts,
        "docs_routing_contract": docs_routing_contract,
        "rag_hits": [
            {
                "chunk_id": h["chunk_id"],
                "doc_type": h["doc_type"],
                "section_header": h["section_header"],
                "rrf_score": h["rrf_score"],
                "snippet": h["content_md"][:300],
            }
            for h in rag_hits
        ],
    }


def ws_checkpoint(
    effort_id: str,
    lease_token: str,
    hypothesis: str = "",
    decisions_locked: list[str] | str | None = None,
    modified_files: list[str] | str | None = None,
    open_blockers: str = "",
    exact_next_step: str = "",
    release_lease: bool = True,
    actor_type: str = "VSCODE",
) -> dict[str, Any]:
    """Verify lease_token, save <300 token checkpoint to session_checkpoints, and optionally release lease."""
    return save_checkpoint(
        effort_id=effort_id,
        lease_token=lease_token,
        hypothesis=hypothesis,
        decisions_locked=decisions_locked or [],
        modified_files=modified_files or [],
        open_blockers=open_blockers,
        exact_next_step=exact_next_step,
        actor_type=actor_type,
        release=release_lease,
    )


def ws_save_spec(
    effort_id: str,
    lease_token: str,
    spec_md: str,
    phase_tag: str = "SPEC_GRILLING",
) -> dict[str, Any]:
    """Verify lease_token, write spec_md to DB + SPEC.md mirror, chunk by headers, and index in FTS5 + sqlite-vec."""
    return save_spec(
        effort_id=effort_id,
        lease_token=lease_token,
        spec_md=spec_md,
        phase_tag=phase_tag,
    )


def ws_log_decision(
    effort_id: str,
    lease_token: str,
    title: str,
    rationale_md: str,
    scope: Literal["EFFORT", "WORKSTREAM", "CODEBASE"] = "EFFORT",
) -> dict[str, Any]:
    """Verify lease_token, append ADR to DECISIONS.md + DB, index in RAG, and sync CODEBASE decisions to Paparats."""
    return log_decision(
        effort_id=effort_id,
        lease_token=lease_token,
        title=title,
        rationale_md=rationale_md,
        scope=scope,
    )


def ws_finalize(
    effort_id: str,
    lease_token: str,
    conclusion_md: str,
    merge_to_effort: bool = True,
) -> dict[str, Any]:
    """Verify clean git status & lease_token, extract AST diff, index RAG, update sibling rollup, merge, and auto-prune."""
    return finalize_sub_effort(
        effort_id=effort_id,
        lease_token=lease_token,
        conclusion_md=conclusion_md,
        merge_to_effort=merge_to_effort,
    )


def ws_reopen_worktree(
    sub_id: str,
    client_id: str = "vscode",
) -> dict[str, Any]:
    """Re-materialize a previously pruned Tier-3 worktree from branch ws/{sub_id} and acquire a new lease."""
    return reopen_worktree(sub_id=sub_id, client_id=client_id)


def ws_rag_search(
    query: str,
    workstream_id: str | None = None,
    effort_id: str | None = None,
    scope: Literal["ALL", "DOCS_AND_DECISIONS", "SIBLINGS_ONLY", "CODE_ONLY"] = "ALL",
    top_k: int = 5,
) -> dict[str, Any]:
    """Run Hybrid RRF (sqlite-vec + FTS5) across Specs, ADRs, Rollups, Conclusions, and AST snippets (+ Paparats if reachable)."""
    local_hits = hybrid_search(
        query=query,
        workstream_id=workstream_id,
        effort_id=effort_id,
        scope=scope,
        top_k=top_k,
    )
    paparats_hits: list[dict[str, Any]] = []
    if scope == "ALL":
        paparats_hits = query_paparats_search(
            query=query,
            workstream_id=workstream_id,
            effort_id=effort_id,
            top_k=top_k,
        )
    return {
        "query": query,
        "workstream_id": workstream_id,
        "effort_id": effort_id,
        "scope": scope,
        "count": len(local_hits),
        "results": local_hits,
        "paparats_results": paparats_hits,
    }


def ws_check_file_gate(
    file_path: str,
    offset: int | None = None,
    limit: int | None = None,
    effort_id: str | None = None,
) -> dict[str, Any]:
    """Enforce the SHUNT_MIN_LINES=350 gate; block full reads of files >350 lines."""
    return check_file_gate(
        file_path=file_path,
        offset=offset,
        limit=limit,
        effort_id=effort_id,
    )


def ws_shunt_read(
    files: list[str] | str,
    question: str,
    effort_id: str | None = None,
) -> dict[str, Any]:
    """Out-of-band bulk-reader: extract facts/citations (<250 tokens) from 1+ large files."""
    return shunt_bulk_read(files=files, question=question, effort_id=effort_id)


def ws_shunt_write(
    effort_id: str,
    lease_token: str,
    target_file: str,
    spec_prompt: str,
    reference_file: str | None = None,
) -> dict[str, Any]:
    """Out-of-band code-writer: generate code/tests directly to target_file modeled after reference_file."""
    return shunt_code_write(
        target_file=target_file,
        spec_prompt=spec_prompt,
        reference_file=reference_file,
        effort_id=effort_id,
        lease_token=lease_token,
    )


def ws_delegate_omni(
    sub_id: str,
    task_prompt: str,
) -> dict[str, Any]:
    """Acquire lease for omni-cli and spawn headless Databricks omni sub-agent in the sub-effort worktree."""
    return delegate_to_omni(sub_id=sub_id, task_prompt=task_prompt)


# Register all 14 MCP tools on FastMCP without shadowing the plain Python functions
for _fn in (
    ws_create_workstream,
    ws_create_effort,
    ws_create_sub_effort,
    ws_hydrate,
    ws_checkpoint,
    ws_save_spec,
    ws_log_decision,
    ws_finalize,
    ws_reopen_worktree,
    ws_rag_search,
    ws_check_file_gate,
    ws_shunt_read,
    ws_shunt_write,
    ws_delegate_omni,
):
    mcp.tool()(_fn)


# ============================================================================
# Typer CLI Commands (`ws ...`)
# ============================================================================


@app.command("init")
def cli_init(
    warm_embedder: Annotated[
        bool, typer.Option("--warm-embedder", help="Pre-download and warm fastembed ONNX cache")
    ] = False,
) -> None:
    """Create ~/.workstreams/ directories, initialize state.db, and verify fastembed cache."""
    _emit(ws_init(warm_embedder=warm_embedder))


@app.command("create-ws")
def cli_create_ws(
    ws_id: Annotated[str, typer.Argument(help="Workstream ID (e.g. ws-auth)")],
    title: Annotated[str, typer.Option("--title", "-t", help="Workstream title")],
    goal: Annotated[str, typer.Option("--goal", "-g", help="Workstream high-level goal")],
) -> None:
    """Create a Tier-1 Workstream and WORKSTREAM_OVERVIEW.md."""
    _emit(ws_create_workstream(ws_id=ws_id, title=title, goal=goal))


@app.command("create-effort")
def cli_create_effort(
    effort_id: Annotated[str, typer.Argument(help="Tier-2 Effort ID")],
    ws_id: Annotated[str, typer.Option("--ws-id", "-w", help="Parent Workstream ID")],
    title: Annotated[str, typer.Option("--title", "-t", help="Effort title")],
    repo_path: Annotated[str, typer.Option("--repo", "-r", help="Path to canonical Git repository")],
    base_branch: Annotated[str, typer.Option("--base-branch", "-b", help="Base git branch")] = "main",
) -> None:
    """Create a Tier-2 Effort and persistent integration worktree."""
    _emit(
        ws_create_effort(
            effort_id=effort_id,
            ws_id=ws_id,
            title=title,
            repo_path=repo_path,
            base_branch=base_branch,
        )
    )


@app.command("create-sub")
def cli_create_sub(
    sub_id: Annotated[str, typer.Argument(help="Tier-3 Sub-Effort ID")],
    parent_effort_id: Annotated[str, typer.Option("--parent", "-p", help="Parent Tier-2 Effort ID")],
    title: Annotated[str, typer.Option("--title", "-t", help="Sub-Effort title")],
    phase_tag: Annotated[str, typer.Option("--phase", help="Initial phase tag")] = "POC",
) -> None:
    """Create a Tier-3 Sub-Effort and ephemeral worktree branching off parent Effort."""
    _emit(
        ws_create_sub_effort(
            sub_id=sub_id,
            parent_effort_id=parent_effort_id,
            title=title,
            phase_tag=phase_tag,
        )
    )


@app.command("hydrate")
def cli_hydrate(
    effort_id: Annotated[str, typer.Argument(help="Effort or Sub-Effort ID to hydrate")],
    client_id: Annotated[str, typer.Option("--client-id", "-c", help="Session client ID")] = "vscode",
    steal: Annotated[bool, typer.Option("--steal", help="Force-steal active lease")] = False,
    rag_query: Annotated[str | None, typer.Option("--rag-query", "-q", help="Optional RAG query")] = None,
) -> None:
    """Hydrate context for an Effort/Sub-Effort and acquire an atomic session lease."""
    _emit(ws_hydrate(effort_id=effort_id, client_id=client_id, steal=steal, rag_query=rag_query))


@app.command("checkpoint")
def cli_checkpoint(
    effort_id: Annotated[str, typer.Argument(help="Effort or Sub-Effort ID")],
    lease_token: Annotated[str, typer.Option("--lease-token", "-k", help="Active fencing token")],
    exact_next_step: Annotated[str, typer.Option("--next-step", "-n", help="Exact next step")],
    hypothesis: Annotated[str, typer.Option("--hypothesis", help="Current hypothesis")] = "",
    decisions_locked: Annotated[str, typer.Option("--decisions", help="JSON or comma-separated decisions")] = "[]",
    modified_files: Annotated[str, typer.Option("--files", help="JSON or comma-separated modified files")] = "[]",
    open_blockers: Annotated[str, typer.Option("--blockers", help="Open blockers")] = "",
    release_lease: Annotated[bool, typer.Option("--release/--keep-lease", help="Release lease on checkpoint")] = True,
) -> None:
    """Save a stateless <300-token checkpoint and optionally release the session lease."""
    _emit(
        ws_checkpoint(
            effort_id=effort_id,
            lease_token=lease_token,
            hypothesis=hypothesis,
            decisions_locked=decisions_locked,
            modified_files=modified_files,
            open_blockers=open_blockers,
            exact_next_step=exact_next_step,
            release_lease=release_lease,
        )
    )


@app.command("save-spec")
def cli_save_spec(
    effort_id: Annotated[str, typer.Argument(help="Effort or Sub-Effort ID")],
    lease_token: Annotated[str, typer.Option("--lease-token", "-k", help="Active fencing token")],
    spec_md: Annotated[str | None, typer.Option("--spec", "-s", help="Markdown content")] = None,
    spec_file: Annotated[Path | None, typer.Option("--file", "-f", help="Path to Markdown file")] = None,
    phase_tag: Annotated[str, typer.Option("--phase", help="Phase tag")] = "SPEC_GRILLING",
) -> None:
    """Save and index SPEC.md for an Effort."""
    content = spec_md or (spec_file.read_text(encoding="utf-8") if spec_file else "")
    _emit(ws_save_spec(effort_id=effort_id, lease_token=lease_token, spec_md=content, phase_tag=phase_tag))


@app.command("log-decision")
def cli_log_decision(
    effort_id: Annotated[str, typer.Argument(help="Effort or Sub-Effort ID")],
    lease_token: Annotated[str, typer.Option("--lease-token", "-k", help="Active fencing token")],
    title: Annotated[str, typer.Option("--title", "-t", help="Decision title")],
    rationale_md: Annotated[str, typer.Option("--rationale", "-r", help="Decision rationale Markdown")],
    scope: Annotated[str, typer.Option("--scope", help="EFFORT | WORKSTREAM | CODEBASE")] = "EFFORT",
) -> None:
    """Log an Architectural or Effort Decision (ADR) and index into Hybrid RAG."""
    _emit(
        ws_log_decision(
            effort_id=effort_id,
            lease_token=lease_token,
            title=title,
            rationale_md=rationale_md,
            scope=scope,  # type: ignore[arg-type]
        )
    )


@app.command("finalize")
def cli_finalize(
    effort_id: Annotated[str, typer.Argument(help="Sub-Effort or Effort ID to finalize")],
    lease_token: Annotated[str, typer.Option("--lease-token", "-k", help="Active fencing token")],
    conclusion_md: Annotated[str, typer.Option("--conclusion", "-c", help="Structured conclusion Markdown")],
    merge_to_effort: Annotated[bool, typer.Option("--merge/--no-merge", help="Merge into parent Effort branch")] = True,
) -> None:
    """Finalize a Sub-Effort: extract AST diff, update parent rollup, merge, and auto-prune worktree."""
    _emit(
        ws_finalize(
            effort_id=effort_id,
            lease_token=lease_token,
            conclusion_md=conclusion_md,
            merge_to_effort=merge_to_effort,
        )
    )


@app.command("reopen")
def cli_reopen(
    sub_id: Annotated[str, typer.Argument(help="Pruned Sub-Effort ID to reopen")],
    client_id: Annotated[str, typer.Option("--client-id", "-c", help="Client ID")] = "vscode",
) -> None:
    """Re-materialize a previously pruned Tier-3 worktree from branch ws/{sub_id}."""
    _emit(ws_reopen_worktree(sub_id=sub_id, client_id=client_id))


@app.command("search")
def cli_search(
    query: Annotated[str, typer.Argument(help="Search query")],
    workstream_id: Annotated[str | None, typer.Option("--ws-id", "-w", help="Filter by Workstream ID")] = None,
    effort_id: Annotated[str | None, typer.Option("--effort-id", "-e", help="Filter by Effort ID")] = None,
    scope: Annotated[str, typer.Option("--scope", help="ALL | DOCS_AND_DECISIONS | SIBLINGS_ONLY | CODE_ONLY")] = "ALL",
    top_k: Annotated[int, typer.Option("--top-k", "-k", help="Number of results")] = 5,
) -> None:
    """Run Hybrid RRF search across Specs, ADRs, Sibling Rollups, Conclusions, and AST snippets."""
    _emit(
        ws_rag_search(
            query=query,
            workstream_id=workstream_id,
            effort_id=effort_id,
            scope=scope,  # type: ignore[arg-type]
            top_k=top_k,
        )
    )


@app.command("check-file")
def cli_check_file(
    file_path: Annotated[str, typer.Argument(help="File path to check against SHUNT_MIN_LINES gate")],
    offset: Annotated[int | None, typer.Option("--offset", help="Line offset")] = None,
    limit: Annotated[int | None, typer.Option("--limit", help="Line limit")] = None,
    effort_id: Annotated[str | None, typer.Option("--effort-id", help="Optional Effort ID")] = None,
) -> None:
    """Check whether reading a file is allowed under the 350-line Shunt hard gate."""
    res = ws_check_file_gate(file_path=file_path, offset=offset, limit=limit, effort_id=effort_id)
    _emit(res)
    if not res.get("allowed", False):
        raise typer.Exit(code=2)


@app.command("shunt-read")
def cli_shunt_read(
    files: Annotated[list[str], typer.Argument(help="One or more file paths to summarize out-of-band")],
    question: Annotated[str, typer.Option("--question", "-q", help="Question to answer from target files")],
    effort_id: Annotated[str | None, typer.Option("--effort-id", "-e", help="Optional Effort ID")] = None,
) -> None:
    """Out-of-band bulk-reader: extract facts and AST citations (<250 tokens) from large files."""
    _emit(ws_shunt_read(files=files, question=question, effort_id=effort_id))


@app.command("shunt-write")
def cli_shunt_write(
    effort_id: Annotated[str, typer.Option("--effort-id", "-e", help="Effort or Sub-Effort ID")],
    lease_token: Annotated[str, typer.Option("--lease-token", "-k", help="Active fencing token")],
    target_file: Annotated[str, typer.Option("--target", "-t", help="Target output file path")],
    spec_prompt: Annotated[str, typer.Option("--prompt", "-p", help="Generation specification prompt")],
    reference_file: Annotated[str | None, typer.Option("--reference", "-r", help="Reference file for conventions")] = None,
) -> None:
    """Out-of-band code-writer: generate boilerplate/tests directly to target_file modeled after reference_file."""
    _emit(
        ws_shunt_write(
            effort_id=effort_id,
            lease_token=lease_token,
            target_file=target_file,
            spec_prompt=spec_prompt,
            reference_file=reference_file,
        )
    )


@app.command("delegate")
def cli_delegate(
    sub_id: Annotated[str, typer.Argument(help="Sub-Effort ID")],
    task_prompt: Annotated[str, typer.Option("--prompt", "-p", help="Task prompt for Databricks Omnigent")],
) -> None:
    """Delegate a Sub-Effort to a headless Databricks Omnigent (`omni`) CLI sub-agent."""
    _emit(ws_delegate_omni(sub_id=sub_id, task_prompt=task_prompt))


def run_mcp() -> None:
    """Entrypoint for `ws-mcp` stdio server."""
    mcp.run()


if __name__ == "__main__":
    app()

