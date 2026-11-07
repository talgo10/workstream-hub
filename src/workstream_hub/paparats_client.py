"""Paparats MCP client: manages projects.yml, triggers re-indexing, and proxies AST queries."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx
import yaml

from workstream_hub.config import (
    PAPARATS_URL,
    ensure_workstreams_dirs,
    get_paparats_projects_yml,
)


def register_effort_in_projects_yml(
    ws_id: str,
    effort_id: str,
    worktree_path: str | Path,
    repo_path: str | Path | None = None,
) -> Path:
    """
    Register the Tier-2 Effort integration worktree (and canonical repo) in
    ~/.workstreams/paparats/projects.yml under group `{ws_id}`.
    """
    ensure_workstreams_dirs()
    yml_path = get_paparats_projects_yml()
    data: dict[str, Any] = {"groups": {}, "projects": []}

    if yml_path.exists():
        try:
            loaded = yaml.safe_load(yml_path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                data = loaded
        except Exception:
            pass

    groups = data.setdefault("groups", {})
    ws_group = groups.setdefault(ws_id, {"efforts": {}, "repositories": []})
    if not isinstance(ws_group, dict):
        ws_group = {"efforts": {}, "repositories": []}
        groups[ws_id] = ws_group

    efforts_map = ws_group.setdefault("efforts", {})
    wt_str = str(Path(worktree_path).resolve())
    efforts_map[effort_id] = {
        "name": effort_id,
        "workstream_id": ws_id,
        "path": wt_str,
    }

    if repo_path:
        repo_str = str(Path(repo_path).resolve())
        repos = ws_group.setdefault("repositories", [])
        if repo_str not in repos:
            repos.append(repo_str)

    projects_list = data.setdefault("projects", [])
    if not any(isinstance(p, dict) and p.get("name") == effort_id for p in projects_list):
        projects_list.append(
            {
                "name": effort_id,
                "group": ws_id,
                "path": wt_str,
            }
        )
    else:
        for p in projects_list:
            if isinstance(p, dict) and p.get("name") == effort_id:
                p["group"] = ws_id
                p["path"] = wt_str

    yml_path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    return yml_path


def trigger_paparats_index(
    effort_id: str | None = None,
    workstream_id: str | None = None,
    timeout: float = 0.5,
) -> dict[str, Any]:
    """
    Fire POST http://localhost:9876/trigger to update Paparats live call graph.
    Gracefully degrades if :9876 is offline.
    """
    url = f"{PAPARATS_URL.rstrip('/')}/trigger"
    payload: dict[str, Any] = {}
    if effort_id:
        payload["project"] = effort_id
    if workstream_id:
        payload["group"] = workstream_id

    try:
        with httpx.Client(timeout=timeout) as client:
            resp = client.post(url, json=payload)
            return {
                "triggered": resp.status_code < 400,
                "status_code": resp.status_code,
            }
    except Exception as exc:
        return {
            "triggered": False,
            "offline": True,
            "reason": str(exc),
        }


def check_paparats_health(timeout: float = 0.3) -> bool:
    """Return True if paparats-mcp is reachable on :9876."""
    try:
        with httpx.Client(timeout=timeout) as client:
            resp = client.get(f"{PAPARATS_URL.rstrip('/')}/health")
            return resp.status_code < 500
    except Exception:
        return False


def sync_decision_to_paparats(
    decision_id: str,
    ws_id: str,
    effort_id: str,
    title: str,
    rationale_md: str,
    timeout: float = 0.5,
) -> bool:
    """Push a CODEBASE-scoped ADR decision to Paparats architectural memory."""
    url = f"{PAPARATS_URL.rstrip('/')}/decisions"
    try:
        with httpx.Client(timeout=timeout) as client:
            resp = client.post(
                url,
                json={
                    "id": decision_id,
                    "group": ws_id,
                    "project": effort_id,
                    "title": title,
                    "rationale": rationale_md,
                },
            )
            return resp.status_code < 400
    except Exception:
        return False


def query_paparats_search(
    query: str,
    workstream_id: str | None = None,
    effort_id: str | None = None,
    top_k: int = 5,
    timeout: float = 0.5,
) -> list[dict[str, Any]]:
    """Query Paparats AST search_code if :9876 is reachable; returns [] if offline."""
    url = f"{PAPARATS_URL.rstrip('/')}/search_code"
    try:
        with httpx.Client(timeout=timeout) as client:
            resp = client.post(
                url,
                json={
                    "query": query,
                    "group": workstream_id,
                    "project": effort_id,
                    "limit": top_k,
                },
            )
            if resp.status_code == 200:
                body = resp.json()
                if isinstance(body, list):
                    return body
                if isinstance(body, dict) and isinstance(body.get("results"), list):
                    return body["results"]
    except Exception:
        pass
    return []

