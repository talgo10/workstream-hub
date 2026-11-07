"""Heading-aware Markdown chunker, two-way disk mtime sync, fastembed vectors, and Hybrid RRF search."""

from __future__ import annotations

import hashlib
import math
import os
import re
import uuid
from pathlib import Path
from typing import Any, Literal

import sqlite_vec

from workstream_hub.config import get_docs_dir
from workstream_hub.db import (
    get_connection,
    get_effort,
    verify_and_refresh_lease,
)

_EMBEDDER: Any = None
_HEADER_RE = re.compile(r"^(#{1,3})\s+(.+?)\s*$", re.MULTILINE)


def get_embedder() -> Any:
    """Lazy singleton for fastembed TextEmbedding('BAAI/bge-small-en-v1.5')."""
    global _EMBEDDER
    if _EMBEDDER is None:
        try:
            from fastembed import TextEmbedding

            _EMBEDDER = TextEmbedding(model_name="BAAI/bge-small-en-v1.5")
        except Exception:
            _EMBEDDER = False
    return _EMBEDDER


def _fallback_embed(text: str, dim: int = 384) -> list[float]:
    """Deterministic 384-dim feature-hashing fallback if offline without cached ONNX model."""
    vec = [0.0] * dim
    tokens = re.findall(r"[a-z0-9_]+", text.lower())
    if not tokens:
        vec[0] = 1.0
        return vec
    for tok in tokens:
        h = int(hashlib.sha256(tok.encode("utf-8")).hexdigest(), 16)
        idx = h % dim
        sign = 1.0 if ((h >> 16) & 1) == 0 else -1.0
        vec[idx] += sign
    norm = math.sqrt(sum(v * v for v in vec)) or 1.0
    return [v / norm for v in vec]


def embed_texts(texts: list[str]) -> list[list[float]]:
    """Generate 384-dimensional embeddings for a list of strings."""
    if not texts:
        return []
    embedder = get_embedder()
    if embedder:
        try:
            embeddings = list(embedder.embed(texts))
            return [[float(x) for x in emb] for emb in embeddings]
        except Exception:
            pass
    return [_fallback_embed(t) for t in texts]


def chunk_markdown_by_headers(
    markdown_text: str,
    ws_id: str,
    effort_id: str,
    doc_type: str,
) -> list[dict[str, str]]:
    """
    Split a Markdown document on `^#{1,3}\\s+(.+)$` and prepend the deterministic lineage prefix:
    `[Workstream: {ws_id} | Effort: {effort_id} | Type: {doc_type} | Section: {header}]\\n{body}`
    """
    text = (markdown_text or "").strip()
    if not text:
        return []

    matches = list(_HEADER_RE.finditer(text))
    raw_sections: list[tuple[str, str]] = []

    if not matches:
        raw_sections.append(("Overview", text))
    else:
        if matches[0].start() > 0:
            preamble = text[: matches[0].start()].strip()
            if preamble:
                raw_sections.append(("Overview", preamble))

        for i, match in enumerate(matches):
            header_title = match.group(2).strip()
            body_start = match.end()
            body_end = matches[i + 1].start() if (i + 1) < len(matches) else len(text)
            body = text[body_start:body_end].strip()
            if body or len(matches) == 1:
                raw_sections.append((header_title, body))

    chunks: list[dict[str, str]] = []
    for idx, (header, body) in enumerate(raw_sections):
        prefix = (
            f"[Workstream: {ws_id} | Effort: {effort_id} | Type: {doc_type} | Section: {header}]"
        )
        full_content = f"{prefix}\n{body}" if body else prefix
        digest = hashlib.sha1(
            f"{ws_id}:{effort_id}:{doc_type}:{idx}:{header}".encode("utf-8")
        ).hexdigest()[:12]
        chunk_id = f"chk_{effort_id}_{doc_type.lower()}_{idx}_{digest}"
        chunks.append(
            {
                "chunk_id": chunk_id,
                "section_header": header,
                "body": body,
                "content_md": full_content,
            }
        )
    return chunks


def index_document_chunks(
    ws_id: str,
    effort_id: str,
    doc_type: Literal[
        "SPEC", "DECISION_ADR", "POC_CONCLUSION", "SIBLING_ROLLUP", "CODE_AST"
    ],
    file_path: str,
    markdown_text: str,
    git_sha: str | None = None,
    replace_existing_for_type: bool = True,
    db_path: Path | None = None,
) -> list[dict[str, Any]]:
    """Chunk markdown by headers, embed via fastembed, and index into doc_chunks, FTS5, and sqlite-vec."""
    chunks = chunk_markdown_by_headers(markdown_text, ws_id, effort_id, doc_type)
    embeddings = embed_texts([c["content_md"] for c in chunks]) if chunks else []

    conn = get_connection(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        if replace_existing_for_type:
            old_rows = conn.execute(
                "SELECT chunk_id FROM doc_chunks WHERE effort_id = ? AND doc_type = ?",
                (effort_id, doc_type),
            ).fetchall()
            for r in old_rows:
                cid = r["chunk_id"]
                conn.execute("DELETE FROM doc_chunks_fts WHERE chunk_id = ?", (cid,))
                conn.execute("DELETE FROM doc_chunks_vec WHERE chunk_id = ?", (cid,))
            conn.execute(
                "DELETE FROM doc_chunks WHERE effort_id = ? AND doc_type = ?",
                (effort_id, doc_type),
            )

        for chunk, vec in zip(chunks, embeddings):
            cid = chunk["chunk_id"]
            # Clean up if same chunk_id already exists
            conn.execute("DELETE FROM doc_chunks_fts WHERE chunk_id = ?", (cid,))
            conn.execute("DELETE FROM doc_chunks_vec WHERE chunk_id = ?", (cid,))
            conn.execute("DELETE FROM doc_chunks WHERE chunk_id = ?", (cid,))

            conn.execute(
                """
                INSERT INTO doc_chunks (
                    chunk_id, workstream_id, effort_id, doc_type,
                    file_path, section_header, content_md, git_sha, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))
                """,
                (
                    cid,
                    ws_id,
                    effort_id,
                    doc_type,
                    str(file_path),
                    chunk["section_header"],
                    chunk["content_md"],
                    git_sha,
                ),
            )
            conn.execute(
                """
                INSERT INTO doc_chunks_fts (chunk_id, section_header, content_md)
                VALUES (?, ?, ?)
                """,
                (cid, chunk["section_header"], chunk["content_md"]),
            )
            conn.execute(
                """
                INSERT INTO doc_chunks_vec (chunk_id, embedding)
                VALUES (?, ?)
                """,
                (cid, sqlite_vec.serialize_float32(vec)),
            )
        conn.execute("COMMIT")
    except Exception:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()

    return chunks


def get_spec_file_path(ws_id: str, effort_id: str) -> Path:
    return get_docs_dir() / ws_id / effort_id / "SPEC.md"


def get_decisions_file_path(ws_id: str, effort_id: str) -> Path:
    return get_docs_dir() / ws_id / effort_id / "DECISIONS.md"


def get_rollup_file_path(ws_id: str, effort_id: str) -> Path:
    return get_docs_dir() / ws_id / effort_id / "SIBLING_ROLLUP.md"


def save_spec(
    effort_id: str,
    lease_token: str,
    spec_md: str,
    phase_tag: str = "SPEC_GRILLING",
    db_path: Path | None = None,
) -> dict[str, Any]:
    """
    Validate lease_token, persist spec_md to state.db and mirror to SPEC.md,
    update spec_file_mtime, and index heading chunks into FTS5 + sqlite-vec.
    """
    verify_and_refresh_lease(effort_id, lease_token, db_path=db_path)
    effort = get_effort(effort_id, db_path=db_path)
    if effort is None:
        raise KeyError(f"Effort '{effort_id}' not found.")

    ws_id = effort["workstream_id"]
    spec_path = get_spec_file_path(ws_id, effort_id)
    spec_path.parent.mkdir(parents=True, exist_ok=True)
    spec_path.write_text(spec_md, encoding="utf-8")
    mtime = os.path.getmtime(spec_path)

    conn = get_connection(db_path)
    try:
        conn.execute(
            """
            UPDATE efforts
            SET spec_md = ?,
                phase_tag = ?,
                spec_file_mtime = ?,
                updated_at = datetime('now')
            WHERE id = ?
            """,
            (spec_md, phase_tag, mtime, effort_id),
        )
    finally:
        conn.close()

    chunks = index_document_chunks(
        ws_id=ws_id,
        effort_id=effort_id,
        doc_type="SPEC",
        file_path=str(spec_path),
        markdown_text=spec_md,
        git_sha=effort.get("latest_git_sha"),
        replace_existing_for_type=True,
        db_path=db_path,
    )

    return {
        "status": "SAVED",
        "effort_id": effort_id,
        "workstream_id": ws_id,
        "phase_tag": phase_tag,
        "spec_path": str(spec_path),
        "spec_file_mtime": mtime,
        "chunks_indexed": len(chunks),
    }


def log_decision(
    effort_id: str,
    lease_token: str,
    title: str,
    rationale_md: str,
    scope: Literal["EFFORT", "WORKSTREAM", "CODEBASE"] = "EFFORT",
    git_sha: str | None = None,
    db_path: Path | None = None,
) -> dict[str, Any]:
    """
    Validate lease_token, append ADR to decisions table + DECISIONS.md mirror,
    index in Hybrid RAG, and optionally sync to Paparats if scope == 'CODEBASE'.
    """
    verify_and_refresh_lease(effort_id, lease_token, db_path=db_path)
    effort = get_effort(effort_id, db_path=db_path)
    if effort is None:
        raise KeyError(f"Effort '{effort_id}' not found.")

    ws_id = effort["workstream_id"]
    decision_id = f"adr_{uuid.uuid4().hex[:10]}"
    sha = git_sha or effort.get("latest_git_sha")

    synced_to_paparats = 0
    if scope == "CODEBASE":
        try:
            from workstream_hub.paparats_client import sync_decision_to_paparats

            if sync_decision_to_paparats(
                decision_id=decision_id,
                ws_id=ws_id,
                effort_id=effort_id,
                title=title,
                rationale_md=rationale_md,
            ):
                synced_to_paparats = 1
        except Exception:
            synced_to_paparats = 0

    conn = get_connection(db_path)
    try:
        conn.execute(
            """
            INSERT INTO decisions (
                id, workstream_id, effort_id, title, rationale_md,
                scope, git_sha, synced_to_paparats
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                decision_id,
                ws_id,
                effort_id,
                title,
                rationale_md,
                scope,
                sha,
                synced_to_paparats,
            ),
        )
        all_decisions = conn.execute(
            "SELECT * FROM decisions WHERE effort_id = ? ORDER BY created_at ASC, rowid ASC",
            (effort_id,),
        ).fetchall()
    finally:
        conn.close()

    decisions_path = get_decisions_file_path(ws_id, effort_id)
    decisions_path.parent.mkdir(parents=True, exist_ok=True)

    sections = [f"# Architectural & Effort Decisions (`{effort_id}`)\n"]
    for d in all_decisions:
        sections.append(
            f"## {d['title']} (`{d['id']}`)\n"
            f"- **Scope:** `{d['scope']}`\n"
            f"- **Git SHA:** `{d['git_sha'] or 'N/A'}`\n"
            f"- **Created At:** `{d['created_at']}`\n\n"
            f"{d['rationale_md'].strip()}\n"
        )
    full_decisions_md = "\n".join(sections)
    decisions_path.write_text(full_decisions_md, encoding="utf-8")

    chunks = index_document_chunks(
        ws_id=ws_id,
        effort_id=effort_id,
        doc_type="DECISION_ADR",
        file_path=str(decisions_path),
        markdown_text=full_decisions_md,
        git_sha=sha,
        replace_existing_for_type=True,
        db_path=db_path,
    )

    return {
        "status": "LOGGED",
        "decision_id": decision_id,
        "effort_id": effort_id,
        "workstream_id": ws_id,
        "title": title,
        "scope": scope,
        "decisions_path": str(decisions_path),
        "synced_to_paparats": bool(synced_to_paparats),
        "chunks_indexed": len(chunks),
    }


def sync_docs_from_disk(
    effort_id: str | None = None,
    workstream_id: str | None = None,
    db_path: Path | None = None,
) -> list[str]:
    """
    Two-way mtime sync: check if mirrored SPEC.md on disk was modified by a human
    (mtime > efforts.spec_file_mtime or content changed) and re-index into state.db.
    Returns list of effort_ids that were synced from disk.
    """
    conn = get_connection(db_path)
    try:
        clauses: list[str] = []
        params: list[Any] = []
        if effort_id:
            clauses.append("id = ?")
            params.append(effort_id)
        if workstream_id:
            clauses.append("workstream_id = ?")
            params.append(workstream_id)
        where_sql = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = conn.execute(f"SELECT * FROM efforts {where_sql}", tuple(params)).fetchall()
    finally:
        conn.close()

    synced_efforts: list[str] = []
    for row in rows:
        eid = row["id"]
        ws_id = row["workstream_id"]
        spec_path = get_spec_file_path(ws_id, eid)
        if not spec_path.exists():
            continue

        disk_mtime = os.path.getmtime(spec_path)
        db_mtime = float(row["spec_file_mtime"] or 0.0)
        disk_text = spec_path.read_text(encoding="utf-8")
        db_text = row["spec_md"] or ""

        # Sync if disk file is newer OR if disk file has non-empty edits differing from DB
        if disk_mtime > db_mtime or (disk_text.strip() and disk_text != db_text):
            if not disk_text.strip() and not db_text.strip():
                continue
            conn = get_connection(db_path)
            try:
                conn.execute(
                    """
                    UPDATE efforts
                    SET spec_md = ?,
                        spec_file_mtime = ?,
                        updated_at = datetime('now')
                    WHERE id = ?
                    """,
                    (disk_text, disk_mtime, eid),
                )
            finally:
                conn.close()

            index_document_chunks(
                ws_id=ws_id,
                effort_id=eid,
                doc_type="SPEC",
                file_path=str(spec_path),
                markdown_text=disk_text,
                git_sha=row["latest_git_sha"],
                replace_existing_for_type=True,
                db_path=db_path,
            )
            synced_efforts.append(eid)

    return synced_efforts


def _build_scope_doc_types(
    scope: Literal["ALL", "DOCS_AND_DECISIONS", "SIBLINGS_ONLY", "CODE_ONLY"]
) -> tuple[str, ...]:
    if scope == "DOCS_AND_DECISIONS":
        return ("SPEC", "DECISION_ADR")
    if scope == "SIBLINGS_ONLY":
        return ("POC_CONCLUSION", "SIBLING_ROLLUP")
    if scope == "CODE_ONLY":
        return ("CODE_AST",)
    return ("SPEC", "DECISION_ADR", "POC_CONCLUSION", "SIBLING_ROLLUP", "CODE_AST")


def hybrid_search(
    query: str,
    workstream_id: str | None = None,
    effort_id: str | None = None,
    scope: Literal["ALL", "DOCS_AND_DECISIONS", "SIBLINGS_ONLY", "CODE_ONLY"] = "ALL",
    top_k: int = 5,
    db_path: Path | None = None,
) -> list[dict[str, Any]]:
    """
    Execute Reciprocal Rank Fusion (RRF) combining sqlite-vec (384-dim semantic search)
    and SQLite FTS5 (BM25 keyword search):
    RRF_Score(d) = 1 / (60 + rank_vec(d)) + 1 / (60 + rank_fts(d))
    """
    sync_docs_from_disk(effort_id=effort_id, workstream_id=workstream_id, db_path=db_path)

    if not query or not query.strip():
        return []

    allowed_doc_types = _build_scope_doc_types(scope)

    conn = get_connection(db_path)
    try:
        # Resolve effort family so searching on a Tier-2 or Tier-3 effort finds sibling rollups & children
        effort_filter_ids: list[str] | None = None
        if effort_id:
            target_effort = conn.execute(
                "SELECT id, parent_effort_id, tier FROM efforts WHERE id = ?",
                (effort_id,),
            ).fetchone()
            if target_effort:
                family_ids = {effort_id}
                if target_effort["parent_effort_id"]:
                    family_ids.add(target_effort["parent_effort_id"])
                children = conn.execute(
                    "SELECT id FROM efforts WHERE parent_effort_id = ?",
                    (target_effort["parent_effort_id"] or effort_id,),
                ).fetchall()
                for c in children:
                    family_ids.add(c["id"])
                effort_filter_ids = list(family_ids)
            else:
                effort_filter_ids = [effort_id]

        # Build filter query for eligible chunks
        clauses = [f"doc_type IN ({','.join('?' for _ in allowed_doc_types)})"]
        params: list[Any] = list(allowed_doc_types)
        if workstream_id:
            clauses.append("workstream_id = ?")
            params.append(workstream_id)
        if effort_filter_ids:
            placeholders = ",".join("?" for _ in effort_filter_ids)
            clauses.append(f"effort_id IN ({placeholders})")
            params.extend(effort_filter_ids)

        where_sql = " AND ".join(clauses)
        eligible_rows = conn.execute(
            f"SELECT * FROM doc_chunks WHERE {where_sql}",
            tuple(params),
        ).fetchall()

        if not eligible_rows:
            return []

        eligible_map: dict[str, dict[str, Any]] = {
            r["chunk_id"]: dict(r) for r in eligible_rows
        }

        # 1. Vector Search (top 20 among eligible)
        query_vec = embed_texts([query])[0]
        total_vec_rows = conn.execute("SELECT COUNT(*) AS cnt FROM doc_chunks_vec").fetchone()["cnt"]
        vec_ranks: dict[str, int] = {}
        if total_vec_rows > 0:
            k_fetch = min(int(total_vec_rows), max(50, top_k * 10))
            vec_hits = conn.execute(
                """
                SELECT chunk_id, distance
                FROM doc_chunks_vec
                WHERE embedding MATCH ?
                  AND k = ?
                ORDER BY distance ASC
                """,
                (sqlite_vec.serialize_float32(query_vec), k_fetch),
            ).fetchall()
            rank_idx = 1
            for vh in vec_hits:
                cid = vh["chunk_id"]
                if cid in eligible_map:
                    vec_ranks[cid] = rank_idx
                    rank_idx += 1
                    if rank_idx > 20:
                        break

        # 2. FTS5 BM25 Search (top 20 among eligible)
        fts_ranks: dict[str, int] = {}
        tokens = re.findall(r"[A-Za-z0-9_]+", query)
        if tokens:
            fts_query = " OR ".join(f'"{t}"' for t in tokens)
            try:
                fts_hits = conn.execute(
                    """
                    SELECT chunk_id, bm25(doc_chunks_fts) AS score
                    FROM doc_chunks_fts
                    WHERE doc_chunks_fts MATCH ?
                    ORDER BY score ASC
                    LIMIT 100
                    """,
                    (fts_query,),
                ).fetchall()
                rank_idx = 1
                for fh in fts_hits:
                    cid = fh["chunk_id"]
                    if cid in eligible_map and cid not in fts_ranks:
                        fts_ranks[cid] = rank_idx
                        rank_idx += 1
                        if rank_idx > 20:
                            break
            except Exception:
                pass

        # 3. Compute Reciprocal Rank Fusion (RRF) score
        candidate_ids = set(vec_ranks.keys()) | set(fts_ranks.keys())
        scored_results: list[dict[str, Any]] = []
        for cid in candidate_ids:
            v_rank = vec_ranks.get(cid)
            f_rank = fts_ranks.get(cid)
            rrf_score = 0.0
            if v_rank is not None:
                rrf_score += 1.0 / (60.0 + v_rank)
            if f_rank is not None:
                rrf_score += 1.0 / (60.0 + f_rank)

            item = dict(eligible_map[cid])
            item["rrf_score"] = round(rrf_score, 6)
            item["vec_rank"] = v_rank
            item["fts_rank"] = f_rank
            scored_results.append(item)

        scored_results.sort(
            key=lambda x: (
                x["rrf_score"],
                1 if x["fts_rank"] is not None else 0,
                -(x["fts_rank"] or 999),
                -(x["vec_rank"] or 999),
            ),
            reverse=True,
        )
        return scored_results[:top_k]
    finally:
        conn.close()

