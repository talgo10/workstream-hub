"""FastAPI + HTMX + Tailwind + Mermaid.js + Diff2Html Unified Web Cockpit (:8765)."""

from __future__ import annotations

import html
import json
from typing import Any

import uvicorn
from fastapi import FastAPI, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse

from workstream_hub.config import (
    OMNI_UI_URL,
    PAPARATS_URL,
    UI_PORT,
    get_docs_dir,
)
from workstream_hub.db import (
    get_connection,
    get_effort,
    get_shunt_stats,
    init_db,
    list_efforts,
    list_workstreams,
)
from workstream_hub.indexer import hybrid_search
from workstream_hub.omni_bridge import check_omni_health
from workstream_hub.paparats_client import check_paparats_health
from workstream_hub.worktree_mgr import (
    check_sibling_conflicts,
    get_branch_diff,
    reopen_worktree,
)

app = FastAPI(
    title="Workstream Hub Cockpit",
    description="Unified Back-to-Work Web Cockpit for Local Workstream Orchestration Hub",
    version="0.1.0",
)

PHASES = ["RESEARCH", "POC", "SPEC_GRILLING", "EXECUTING", "DONE"]


def _escape(val: Any) -> str:
    return html.escape(str(val or ""))


def _get_delta_brief() -> dict[str, Any]:
    conn = get_connection()
    try:
        recent_rows = conn.execute(
            """
            SELECT id, title, tier, phase_tag, updated_at, lease_owner, worktree_pruned
            FROM efforts
            WHERE updated_at >= datetime('now', '-12 hours')
            ORDER BY updated_at DESC
            """
        ).fetchall()
        active_leases = conn.execute(
            """
            SELECT id, title, lease_owner, lease_expires_at
            FROM efforts
            WHERE lease_owner IS NOT NULL
              AND lease_expires_at > datetime('now')
            """
        ).fetchall()
        return {
            "recent_efforts": [dict(r) for r in recent_rows],
            "active_leases": [dict(r) for r in active_leases],
        }
    finally:
        conn.close()


def _render_sub_card(sub: dict[str, Any], conflict_info: dict[str, Any]) -> str:
    sub_id = sub["id"]
    title = _escape(sub["title"])
    branch = _escape(sub["git_branch"])
    wt_path = sub.get("worktree_path") or ""

    if sub.get("worktree_pruned"):
        lease_badge = (
            f'<span class="px-2 py-0.5 text-xs rounded bg-emerald-900/60 text-emerald-200">'
            f"✅ Pruned (Branch: {branch})</span>"
        )
    elif sub.get("lease_owner"):
        owner = _escape(sub["lease_owner"])
        lease_badge = (
            f'<span class="px-2 py-0.5 text-xs rounded bg-green-900/70 text-green-200">'
            f"🟢 Active ({owner})</span>"
        )
    else:
        lease_badge = (
            '<span class="px-2 py-0.5 text-xs rounded bg-slate-700 text-slate-200">'
            "⏸️ Parked (Ready to Hydrate)</span>"
        )

    # Check if this sub is part of any conflict pair
    conflicting_files: list[str] = []
    for pair in conflict_info.get("conflict_pairs", []):
        if sub_id in (pair.get("sub_a"), pair.get("sub_b")):
            conflicting_files.extend(pair.get("files", []))
    conflicting_files = sorted(set(conflicting_files))

    if conflicting_files:
        files_str = _escape(", ".join(conflicting_files))
        conflict_badge = (
            f'<span class="px-2 py-0.5 text-xs rounded bg-red-900/70 text-red-200">'
            f"🔴 Conflict ({files_str})</span>"
        )
    else:
        conflict_badge = (
            '<span class="px-2 py-0.5 text-xs rounded bg-teal-900/50 text-teal-200">'
            "🟢 Merge Clean</span>"
        )

    if sub.get("worktree_pruned") or not wt_path:
        open_btn = (
            f'<button hx-post="/ui/reopen/{_escape(sub_id)}" hx-swap="outerHTML" '
            f'class="px-2 py-1 text-xs bg-amber-600 hover:bg-amber-500 rounded text-white font-medium">'
            f"[Re-open Worktree]</button>"
        )
    else:
        vscode_url = f"vscode://file/{_escape(wt_path)}"
        open_btn = (
            f'<a href="{vscode_url}" '
            f'class="px-2 py-1 text-xs bg-blue-600 hover:bg-blue-500 rounded text-white font-medium">'
            f"[Open in VS Code]</a>"
        )

    return f"""
    <div class="bg-slate-800/90 border border-slate-700 rounded-lg p-3 shadow-sm space-y-2">
        <div class="flex items-center justify-between">
            <span class="font-mono text-xs text-indigo-300 font-semibold">{_escape(sub_id)}</span>
            <span class="font-mono text-[11px] text-slate-400">{_escape((sub.get("latest_git_sha") or "")[:8])}</span>
        </div>
        <div class="text-sm font-medium text-slate-100">{title}</div>
        <div class="flex flex-wrap gap-1.5">
            {lease_badge}
            {conflict_badge}
        </div>
        <div class="flex flex-wrap gap-1.5 pt-1">
            {open_btn}
            <button onclick="navigator.clipboard.writeText('/ws-hydrate {_escape(sub_id)}')"
                    class="px-2 py-1 text-xs bg-slate-700 hover:bg-slate-600 rounded text-slate-200">
                [Copy /ws-hydrate]
            </button>
            <button hx-get="/ui/drawer/{_escape(sub_id)}" hx-target="#inspector-drawer" hx-swap="innerHTML"
                    class="px-2 py-1 text-xs bg-indigo-600 hover:bg-indigo-500 rounded text-white">
                [Inspect Diff &amp; Spec]
            </button>
        </div>
    </div>
    """


@app.get("/", response_class=HTMLResponse)
def main_cockpit(request: Request, ws_id: str | None = Query(default=None)) -> HTMLResponse:
    """Render the Unified Back-to-Work Web Cockpit."""
    init_db()
    workstreams = list_workstreams()
    active_ws_id = ws_id or (workstreams[0]["id"] if workstreams else None)

    shunt_stats = get_shunt_stats()
    tokens_saved = shunt_stats.get("total_tokens_saved", 0)
    paparats_up = check_paparats_health()
    omni_up = check_omni_health()
    delta = _get_delta_brief()

    efforts_tier2 = list_efforts(workstream_id=active_ws_id, tier=2) if active_ws_id else list_efforts(tier=2)

    swimlanes_html: list[str] = []
    conflict_warnings: list[str] = []

    for eff in efforts_tier2:
        eid = eff["id"]
        conflicts = check_sibling_conflicts(eid)
        if conflicts["has_conflict"]:
            conflict_warnings.append(
                f"Effort `{eid}` has sibling conflicts in: {', '.join(conflicts['conflicting_files'])}"
            )

        subs = list_efforts(parent_effort_id=eid, tier=3)
        cols_html: list[str] = []
        for phase in PHASES:
            phase_subs = [s for s in subs if s["phase_tag"] == phase]
            cards_str = "\n".join(_render_sub_card(s, conflicts) for s in phase_subs)
            if not cards_str:
                cards_str = '<div class="text-xs text-slate-500 italic py-2">No sub-efforts</div>'
            cols_html.append(
                f"""
                <div class="bg-slate-900/60 border border-slate-800 rounded-lg p-2.5 min-h-[130px]">
                    <div class="text-xs font-semibold tracking-wider text-slate-400 uppercase mb-2">{phase}</div>
                    <div class="space-y-2">{cards_str}</div>
                </div>
                """
            )

        eff_wt = eff.get("worktree_path") or ""
        vscode_eff_link = f"vscode://file/{_escape(eff_wt)}" if eff_wt else "#"
        swimlanes_html.append(
            f"""
            <div class="bg-slate-900/90 border border-slate-800 rounded-xl p-4 space-y-3">
                <div class="flex flex-wrap items-center justify-between gap-2 border-b border-slate-800 pb-3">
                    <div>
                        <span class="text-xs font-mono px-2 py-0.5 rounded bg-indigo-950 text-indigo-300 border border-indigo-800">
                            Tier-2 Effort: {_escape(eid)}
                        </span>
                        <h3 class="text-base font-semibold text-white inline ml-2">{_escape(eff["title"])}</h3>
                        <span class="text-xs text-slate-400 font-mono ml-2">branch: {_escape(eff["git_branch"])}</span>
                    </div>
                    <div class="flex gap-2">
                        <a href="{vscode_eff_link}" class="px-2.5 py-1 text-xs bg-blue-600 hover:bg-blue-500 rounded text-white">
                            [Open Effort Worktree]
                        </a>
                        <button onclick="navigator.clipboard.writeText('/ws-hydrate {_escape(eid)}')"
                                class="px-2.5 py-1 text-xs bg-slate-800 hover:bg-slate-700 rounded text-slate-200">
                            [Copy /ws-hydrate]
                        </button>
                        <button hx-get="/ui/drawer/{_escape(eid)}" hx-target="#inspector-drawer" hx-swap="innerHTML"
                                class="px-2.5 py-1 text-xs bg-indigo-600 hover:bg-indigo-500 rounded text-white">
                            [Inspect Diff &amp; Spec]
                        </button>
                    </div>
                </div>
                <div class="grid grid-cols-1 md:grid-cols-5 gap-3">
                    {"".join(cols_html)}
                </div>
            </div>
            """
        )

    if not swimlanes_html:
        swimlanes_html.append(
            '<div class="bg-slate-900 border border-slate-800 rounded-xl p-6 text-slate-400 text-sm">'
            "No active Tier-2 Efforts yet. Run <code>ws create-ws</code> and <code>ws create-effort</code> to start."
            "</div>"
        )

    recent_summary = (
        ", ".join(f"{r['id']} ({r['phase_tag']})" for r in delta["recent_efforts"][:6])
        or "None in last 12h"
    )
    active_leases_summary = (
        ", ".join(f"{r['id']} -> {r['lease_owner']}" for r in delta["active_leases"])
        or "No active leases"
    )
    conflicts_banner = (
        " | ".join(conflict_warnings) if conflict_warnings else "🟢 All sibling branches merge clean"
    )

    ws_options = "".join(
        f'<option value="{_escape(w["id"])}" {"selected" if w["id"] == active_ws_id else ""}>'
        f'{_escape(w["title"])} ({_escape(w["id"])})</option>'
        for w in workstreams
    )

    html_doc = f"""<!DOCTYPE html>
<html lang="en" class="dark">
<head>
    <meta charset="utf-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1.0" />
    <title>Workstream Hub — Back to Work Cockpit</title>
    <script src="https://cdn.tailwindcss.com"></script>
    <script src="https://unpkg.com/htmx.org@1.9.12"></script>
    <script src="https://cdn.jsdelivr.net/npm/mermaid@10/dist/mermaid.min.js"></script>
    <link rel="stylesheet" type="text/css" href="https://cdn.jsdelivr.net/npm/diff2html/bundles/css/diff2html.min.css" />
    <script type="text/javascript" src="https://cdn.jsdelivr.net/npm/diff2html/bundles/js/diff2html-ui.min.js"></script>
</head>
<body class="bg-slate-950 text-slate-100 min-h-screen font-sans">
    <!-- Top Bar -->
    <header class="border-b border-slate-800 bg-slate-900/95 sticky top-0 z-20 px-6 py-3 flex flex-wrap items-center justify-between gap-4">
        <div class="flex items-center gap-3">
            <span class="text-lg font-bold tracking-tight text-white">⚡ Workstream Hub</span>
            <span class="text-xs px-2.5 py-1 rounded-full bg-indigo-950 text-indigo-300 border border-indigo-800 font-mono">
                🛡️ Shunt Saved: {tokens_saved:,} tokens
            </span>
        </div>

        <div class="flex-1 max-w-xl">
            <input type="search"
                   name="q"
                   placeholder="Global Hybrid RAG Search (Specs, ADRs, Rollups, AST)..."
                   hx-get="/ui/search"
                   hx-trigger="keyup changed delay:250ms, search"
                   hx-target="#rag-search-results"
                   class="w-full bg-slate-950 border border-slate-700 rounded-lg px-3 py-1.5 text-sm text-slate-100 focus:outline-none focus:border-indigo-500" />
        </div>

        <div class="flex items-center gap-3 text-xs">
            <span class="px-2 py-1 rounded bg-emerald-950 text-emerald-300 border border-emerald-800">
                🟢 SQLite WAL
            </span>
            <span class="px-2 py-1 rounded bg-slate-800 text-slate-300">
                {"🟢" if paparats_up else "⚪"} Paparats (:9876)
            </span>
            <span class="px-2 py-1 rounded bg-slate-800 text-slate-300">
                {"🟢" if omni_up else "⚪"} Omnigent (:6767)
            </span>
        </div>
    </header>

    <main class="max-w-7xl mx-auto px-6 py-5 space-y-5">
        <!-- Search Results Container -->
        <div id="rag-search-results"></div>

        <!-- Delta Brief Banner -->
        <section class="bg-slate-900/90 border border-slate-800 rounded-xl p-4 grid grid-cols-1 md:grid-cols-3 gap-4 text-xs">
            <div>
                <div class="text-slate-400 font-semibold uppercase mb-1">Delta Brief (Last 12h)</div>
                <div class="text-slate-200 font-mono">{_escape(recent_summary)}</div>
            </div>
            <div>
                <div class="text-slate-400 font-semibold uppercase mb-1">Active Session Leases</div>
                <div class="text-slate-200 font-mono">{_escape(active_leases_summary)}</div>
            </div>
            <div>
                <div class="text-slate-400 font-semibold uppercase mb-1">Sibling Conflict Radar (git merge-tree)</div>
                <div class="text-slate-200 font-mono">{_escape(conflicts_banner)}</div>
            </div>
        </section>

        <!-- Workstream Selector -->
        <section class="flex items-center justify-between">
            <div class="flex items-center gap-3">
                <label class="text-xs uppercase tracking-wider text-slate-400 font-semibold">Workstream:</label>
                <select onchange="window.location.href='/?ws_id=' + this.value"
                        class="bg-slate-900 border border-slate-700 rounded px-3 py-1 text-sm text-white">
                    {ws_options or '<option value="">Default</option>'}
                </select>
            </div>
        </section>

        <!-- 3-Tier Kanban Swimlanes -->
        <section class="space-y-4">
            {"".join(swimlanes_html)}
        </section>

        <!-- Slide-Over Inspector Drawer Target -->
        <section id="inspector-drawer"></section>
    </main>
</body>
</html>"""
    return HTMLResponse(content=html_doc)


@app.get("/ui/search", response_class=HTMLResponse)
def ui_search(q: str = Query(default="")) -> HTMLResponse:
    """HTMX endpoint returning Hybrid RRF search hits."""
    if not q.strip():
        return HTMLResponse(content="")

    hits = hybrid_search(query=q, scope="ALL", top_k=6)
    if not hits:
        return HTMLResponse(
            content=(
                f'<div class="bg-slate-900 border border-slate-800 rounded-xl p-4 text-xs text-slate-400">'
                f'No Hybrid RAG matches found for <code>{_escape(q)}</code>.'
                f"</div>"
            )
        )

    rows_html: list[str] = []
    for h in hits:
        rows_html.append(
            f"""
            <div class="border border-slate-800 bg-slate-950/80 rounded-lg p-3 space-y-1">
                <div class="flex items-center justify-between text-xs">
                    <span class="font-mono text-indigo-300 font-semibold">
                        [{_escape(h["doc_type"])}] {_escape(h["effort_id"])} :: {_escape(h["section_header"])}
                    </span>
                    <span class="font-mono text-emerald-400">RRF: {h["rrf_score"]:.4f}</span>
                </div>
                <pre class="text-xs text-slate-300 whitespace-pre-wrap font-mono overflow-x-auto">{_escape(h["content_md"][:500])}</pre>
            </div>
            """
        )

    return HTMLResponse(
        content=f"""
        <div class="bg-slate-900 border border-indigo-900/60 rounded-xl p-4 space-y-2">
            <div class="text-xs font-semibold text-indigo-300 uppercase">
                Hybrid RAG RRF Results for "{_escape(q)}" ({len(hits)} hits)
            </div>
            <div class="grid grid-cols-1 md:grid-cols-2 gap-2.5">
                {"".join(rows_html)}
            </div>
        </div>
        """
    )


@app.get("/ui/drawer/{effort_id}", response_class=HTMLResponse)
def ui_drawer(effort_id: str) -> HTMLResponse:
    """
    Slide-Over Inspector Drawer:
    - Tab 1: Side-by-Side Git Diff (Diff2HtmlUI) — works even after Tier-3 worktree is auto-pruned.
    - Tab 2: Spec, Decisions & Sibling Rollup.
    - Tab 3: Live Harnesses (Omnigent :6767 & Paparats Architecture Map :9876/ui/map).
    """
    effort = get_effort(effort_id)
    if effort is None:
        return HTMLResponse(
            content=f'<div class="p-4 text-red-400">Effort {_escape(effort_id)} not found.</div>',
            status_code=404,
        )

    raw_diff = get_branch_diff(effort_id)
    diff_json = json.dumps(raw_diff)

    ws_id = effort["workstream_id"]
    parent_id = effort["parent_effort_id"] or effort_id
    parent_effort = get_effort(parent_id) or effort

    spec_md = effort.get("spec_md") or parent_effort.get("spec_md") or ""
    rollup_md = effort.get("children_rollup_md") or parent_effort.get("children_rollup_md") or ""
    conclusion_md = effort.get("conclusion_md") or ""

    decisions_file = get_docs_dir() / ws_id / parent_id / "DECISIONS.md"
    decisions_md = decisions_file.read_text(encoding="utf-8") if decisions_file.exists() else ""

    omni_url = effort.get("omni_session_url") or f"{OMNI_UI_URL.rstrip('/')}/sessions/{effort_id}"
    paparats_map_url = f"{PAPARATS_URL.rstrip('/')}/ui/map"

    drawer_html = f"""
    <div class="bg-slate-900 border border-indigo-800/80 rounded-xl p-5 shadow-2xl space-y-4 mt-4">
        <div class="flex items-center justify-between border-b border-slate-800 pb-3">
            <div>
                <span class="text-xs font-mono px-2 py-0.5 rounded bg-indigo-950 text-indigo-300">
                    Inspector Drawer: {_escape(effort_id)}
                </span>
                <h2 class="text-lg font-bold text-white inline ml-2">{_escape(effort["title"])}</h2>
                <span class="text-xs text-slate-400 font-mono ml-2">
                    ({_escape(effort["base_branch"])}..{_escape(effort["git_branch"])})
                </span>
            </div>
            <button onclick="document.getElementById('inspector-drawer').innerHTML=''"
                    class="text-xs px-2.5 py-1 rounded bg-slate-800 hover:bg-slate-700 text-slate-300">
                ✕ Close
            </button>
        </div>

        <!-- Tab 1: Side-by-Side Git Diff (Diff2Html) -->
        <div class="space-y-2">
            <h3 class="text-xs font-semibold uppercase tracking-wider text-indigo-300">
                Tab 1 — Side-by-Side Branch Diff (Diff2Html)
            </h3>
            <div id="d2h-container-{_escape(effort_id)}" class="bg-white text-slate-900 rounded-lg p-2 overflow-x-auto"></div>
            <pre class="hidden raw-diff-fallback">{_escape(raw_diff)}</pre>
        </div>

        <!-- Tab 2: Spec, Decisions & Sibling Rollup -->
        <div class="grid grid-cols-1 md:grid-cols-3 gap-4 pt-2">
            <div class="bg-slate-950 border border-slate-800 rounded-lg p-3">
                <h4 class="text-xs font-semibold uppercase text-slate-400 mb-2">SPEC.md</h4>
                <pre class="text-xs text-slate-200 whitespace-pre-wrap font-mono">{_escape(spec_md or "No spec saved yet.")}</pre>
            </div>
            <div class="bg-slate-950 border border-slate-800 rounded-lg p-3">
                <h4 class="text-xs font-semibold uppercase text-slate-400 mb-2">DECISIONS.md</h4>
                <pre class="text-xs text-slate-200 whitespace-pre-wrap font-mono">{_escape(decisions_md or "No ADRs logged yet.")}</pre>
            </div>
            <div class="bg-slate-950 border border-slate-800 rounded-lg p-3">
                <h4 class="text-xs font-semibold uppercase text-slate-400 mb-2">SIBLING_ROLLUP.md &amp; Conclusion</h4>
                <pre class="text-xs text-slate-200 whitespace-pre-wrap font-mono">{_escape(rollup_md or conclusion_md or "No rollups yet.")}</pre>
            </div>
        </div>

        <!-- Tab 3: Live Harnesses -->
        <div class="border-t border-slate-800 pt-3 flex flex-wrap items-center gap-4 text-xs">
            <span class="text-slate-400 font-semibold uppercase">Tab 3 — Live Harnesses:</span>
            <a href="{_escape(omni_url)}" target="_blank" class="text-indigo-400 hover:underline font-mono">
                🔗 Omnigent Session ({_escape(omni_url)})
            </a>
            <a href="{_escape(paparats_map_url)}" target="_blank" class="text-emerald-400 hover:underline font-mono">
                🗺️ Paparats Architecture Map ({_escape(paparats_map_url)})
            </a>
        </div>

        <script>
        (function() {{
            var diffString = {diff_json};
            var targetEl = document.getElementById("d2h-container-{_escape(effort_id)}");
            if (typeof Diff2HtmlUI !== "undefined" && targetEl) {{
                var ui = new Diff2HtmlUI(targetEl, diffString, {{
                    drawFileList: true,
                    matching: "lines",
                    outputFormat: "side-by-side"
                }});
                ui.draw();
            }} else if (targetEl) {{
                targetEl.innerHTML = "<pre>" + (diffString || "No diff changes.") + "</pre>";
            }}
        }})();
        </script>
    </div>
    """
    return HTMLResponse(content=drawer_html)


@app.post("/ui/reopen/{sub_id}", response_class=HTMLResponse)
def ui_reopen(sub_id: str) -> HTMLResponse:
    """Re-open a previously auto-pruned Tier-3 Sub-Effort worktree."""
    res = reopen_worktree(sub_id=sub_id, client_id="vscode-ui")
    wt = _escape(res["worktree_path"])
    return HTMLResponse(
        content=(
            f'<a href="vscode://file/{wt}" '
            f'class="px-2 py-1 text-xs bg-blue-600 hover:bg-blue-500 rounded text-white font-medium">'
            f"[Open in VS Code]</a>"
        )
    )


@app.get("/api/status", response_class=JSONResponse)
def api_status() -> JSONResponse:
    """Return JSON status and Shunt telemetry."""
    return JSONResponse(
        content={
            "status": "ok",
            "shunt_stats": get_shunt_stats(),
            "paparats_online": check_paparats_health(),
            "omni_online": check_omni_health(),
        }
    )


def run_ui() -> None:
    """Entrypoint for `ws-ui` server on localhost:8765."""
    init_db()
    uvicorn.run(app, host="127.0.0.1", port=UI_PORT, log_level="info")


if __name__ == "__main__":
    run_ui()

