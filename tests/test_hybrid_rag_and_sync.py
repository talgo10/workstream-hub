"""AC-3 Verification: Heading-Aware Chunker, FTS5 + sqlite-vec RRF Search, and Two-Way mtime Disk Sync."""

from __future__ import annotations

import os
import time
from pathlib import Path

from workstream_hub.cli_mcp import (
    ws_create_effort,
    ws_create_workstream,
    ws_hydrate,
    ws_log_decision,
    ws_rag_search,
    ws_save_spec,
)
from workstream_hub.config import get_docs_dir
from workstream_hub.db import get_connection, get_effort


def test_ac3_heading_aware_hybrid_rag_and_two_way_mtime_sync(temp_git_repo: Path) -> None:
    """
    AC-3:
    Calling ws_save_spec and ws_log_decision writes SPEC.md and DECISIONS.md to ~/.workstreams/docs/...
    and indexes each ## section into doc_chunks_fts and doc_chunks_vec.
    Modifying SPEC.md directly on disk and calling ws_rag_search automatically detects the newer mtime,
    re-indexes the updated section, and retrieves the newly edited text via both semantic query and
    exact BM25 keyword match.
    """
    ws_create_workstream("ws-rag", title="Hybrid RAG Epic", goal="Test RRF and mtime sync")
    ws_create_effort("eff-rag", ws_id="ws-rag", title="Vector Indexer Effort", repo_path=str(temp_git_repo))

    hyd = ws_hydrate("eff-rag", client_id="win-rag")
    token = hyd["lease_token"]

    initial_spec = (
        "# Vector Indexer Spec\n\n"
        "## Storage Engine Architecture\n"
        "We use single-file SQLite in WAL mode with sqlite-vec for 384-dim embeddings.\n\n"
        "## Token Shunting Strategy\n"
        "Files over 350 lines are gated and routed to out-of-band bulk readers.\n"
    )
    save_res = ws_save_spec("eff-rag", lease_token=token, spec_md=initial_spec)
    assert save_res["status"] == "SAVED"
    assert save_res["chunks_indexed"] >= 2

    adr_res = ws_log_decision(
        "eff-rag",
        lease_token=token,
        title="Adopt Reciprocal Rank Fusion",
        rationale_md="Combines FTS5 BM25 lexical precision with bge-small-en-v1.5 semantic recall using k=60.",
        scope="EFFORT",
    )
    assert adr_res["status"] == "LOGGED"

    spec_path = get_docs_dir() / "ws-rag" / "eff-rag" / "SPEC.md"
    decisions_path = get_docs_dir() / "ws-rag" / "eff-rag" / "DECISIONS.md"
    assert spec_path.exists()
    assert decisions_path.exists()

    # Verify chunks exist in doc_chunks, doc_chunks_fts, and doc_chunks_vec
    conn = get_connection()
    try:
        fts_count = conn.execute("SELECT COUNT(*) AS c FROM doc_chunks_fts").fetchone()["c"]
        vec_count = conn.execute("SELECT COUNT(*) AS c FROM doc_chunks_vec").fetchone()["c"]
        assert fts_count >= 3
        assert vec_count >= 3
    finally:
        conn.close()

    # Human edits SPEC.md directly on disk with a unique keyword & new section content
    updated_spec = (
        "# Vector Indexer Spec\n\n"
        "## Storage Engine Architecture\n"
        "We upgraded the storage layer to use QUANTUM_HYPERGRAPH_SHARDING_V9 for zero-copy reads.\n\n"
        "## Token Shunting Strategy\n"
        "Files over 350 lines are gated and routed to out-of-band bulk readers.\n"
    )
    spec_path.write_text(updated_spec, encoding="utf-8")
    # Bump mtime explicitly by +2 seconds to simulate human edit in VS Code
    new_mtime = time.time() + 2.0
    os.utime(spec_path, (new_mtime, new_mtime))

    # Calling ws_rag_search must auto-detect newer mtime, sync to DB, re-index, and retrieve the edit
    bm25_res = ws_rag_search("QUANTUM_HYPERGRAPH_SHARDING_V9", workstream_id="ws-rag")
    assert bm25_res["count"] >= 1
    top_hit = bm25_res["results"][0]
    assert "QUANTUM_HYPERGRAPH_SHARDING_V9" in top_hit["content_md"]
    assert top_hit["section_header"] == "Storage Engine Architecture"

    semantic_res = ws_rag_search("zero copy storage layer sharding upgrade", workstream_id="ws-rag")
    assert semantic_res["count"] >= 1
    assert any("QUANTUM_HYPERGRAPH_SHARDING_V9" in r["content_md"] for r in semantic_res["results"])

    # Verify state.db efforts.spec_md was updated by the 2-way sync
    eff_row = get_effort("eff-rag")
    assert eff_row is not None
    assert "QUANTUM_HYPERGRAPH_SHARDING_V9" in eff_row["spec_md"]

