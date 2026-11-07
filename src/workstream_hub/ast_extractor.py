"""Zero-token AST symbol & signature extractor using tree-sitter-language-pack."""

from __future__ import annotations

import re
import subprocess
import uuid
from pathlib import Path
from typing import Any

from workstream_hub.db import get_connection
from workstream_hub.indexer import index_document_chunks

EXT_TO_LANGUAGE: dict[str, str] = {
    ".py": "python",
    ".rs": "rust",
    ".scala": "scala",
    ".sc": "scala",
    ".sql": "sql",
    ".ts": "typescript",
    ".tsx": "tsx",
    ".js": "javascript",
    ".jsx": "javascript",
    ".go": "go",
    ".java": "java",
}

NODE_TYPE_TO_KIND: dict[str, str] = {
    "function_definition": "function",
    "function_item": "function",
    "function_declaration": "function",
    "method_declaration": "function",
    "method_definition": "function",
    "class_definition": "class",
    "class_declaration": "class",
    "object_definition": "class",
    "struct_item": "struct",
    "type_declaration": "struct",
    "trait_item": "trait",
    "interface_declaration": "trait",
    "impl_item": "impl",
}

WRAPPER_NODE_TYPES = {
    "decorated_definition",
    "export_statement",
    "program",
    "module",
    "source_file",
    "compilation_unit",
}


def _truncate_body(text: str, max_lines: int = 50) -> str:
    """Truncate bodies > 50 lines while preserving signatures and docstrings."""
    lines = text.splitlines()
    if len(lines) <= max_lines:
        return text
    kept = lines[:max_lines]
    omitted = len(lines) - max_lines
    kept.append(f"    # ... [truncated {omitted} lines by AST extractor] ...")
    return "\n".join(kept)


def _extract_node_name(node: Any, source_bytes: bytes) -> str:
    """Extract the symbol identifier from a tree-sitter AST node."""
    for field in ("name", "type", "declarator"):
        child = node.child_by_field_name(field)
        if child is not None:
            raw = source_bytes[child.start_byte : child.end_byte].decode("utf-8", errors="replace").strip()
            if raw:
                return raw.split("(")[0].strip()

    for child in getattr(node, "children", []):
        if child.type in ("identifier", "type_identifier", "property_identifier"):
            raw = source_bytes[child.start_byte : child.end_byte].decode("utf-8", errors="replace").strip()
            if raw:
                return raw
    return "anonymous"


def extract_symbols_from_source(
    source_text: str,
    file_path: str,
    language: str | None = None,
) -> list[dict[str, Any]]:
    """Parse source code with tree-sitter and return top-level function/class/struct/trait/impl symbols."""
    ext = Path(file_path).suffix.lower()
    lang = language or EXT_TO_LANGUAGE.get(ext)
    source_bytes = source_text.encode("utf-8")
    extracted: list[dict[str, Any]] = []

    if lang:
        try:
            from tree_sitter_language_pack import get_parser

            parser = get_parser(lang)
            tree = parser.parse(source_bytes)
            root = tree.root_node

            def _visit(node: Any, outer_node: Any | None = None) -> None:
                if node.type in NODE_TYPE_TO_KIND:
                    target = outer_node or node
                    kind = NODE_TYPE_TO_KIND[node.type]
                    name = _extract_node_name(node, source_bytes)
                    raw_snippet = source_bytes[target.start_byte : target.end_byte].decode(
                        "utf-8", errors="replace"
                    )
                    extracted.append(
                        {
                            "file_path": str(file_path),
                            "symbol_name": name,
                            "symbol_kind": kind,
                            "start_line": int(target.start_point[0]) + 1,
                            "end_line": int(target.end_point[0]) + 1,
                            "signature_and_body": _truncate_body(raw_snippet, max_lines=50),
                        }
                    )
                    return

                if node.type in WRAPPER_NODE_TYPES:
                    wrapper = node if node.type == "decorated_definition" else outer_node
                    for child in node.children:
                        _visit(child, outer_node=wrapper)

            _visit(root)
        except Exception:
            pass

    if not extracted:
        # Deterministic regex fallback if language is unsupported or has no tree-sitter grammar
        pattern = re.compile(
            r"^(?:pub\s+)?(?:async\s+)?(def|class|fn|struct|trait|impl|function|interface)\s+([A-Za-z_][A-Za-z0-9_]*)",
            re.MULTILINE,
        )
        lines = source_text.splitlines()
        matches = list(pattern.finditer(source_text))
        for i, m in enumerate(matches):
            kw = m.group(1)
            name = m.group(2)
            kind_map = {
                "def": "function",
                "fn": "function",
                "function": "function",
                "class": "class",
                "struct": "struct",
                "trait": "trait",
                "interface": "trait",
                "impl": "impl",
            }
            start_line = source_text[: m.start()].count("\n") + 1
            end_line = (
                source_text[: matches[i + 1].start()].count("\n")
                if (i + 1) < len(matches)
                else len(lines)
            )
            snippet = "\n".join(lines[start_line - 1 : min(end_line, start_line - 1 + 50)])
            extracted.append(
                {
                    "file_path": str(file_path),
                    "symbol_name": name,
                    "symbol_kind": kind_map.get(kw, "function"),
                    "start_line": start_line,
                    "end_line": end_line,
                    "signature_and_body": _truncate_body(snippet, max_lines=50),
                }
            )

    return extracted


def extract_symbols_from_file(
    abs_file_path: str | Path,
    rel_file_path: str | None = None,
) -> list[dict[str, Any]]:
    """Read a file from disk and extract its top-level AST symbols."""
    p = Path(abs_file_path)
    if not p.exists() or not p.is_file():
        return []
    try:
        content = p.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    display_path = rel_file_path or str(p)
    return extract_symbols_from_source(content, file_path=display_path)


def extract_and_index_git_diff_ast(
    workstream_id: str,
    effort_id: str,
    repo_path: str | Path,
    worktree_path: str | Path,
    base_ref: str,
    head_sha: str,
    db_path: Path | None = None,
) -> list[dict[str, Any]]:
    """
    Run `git -C <worktree_path> diff --name-only <base_ref>..HEAD`,
    extract modified AST symbols via tree-sitter, store in `code_artifacts`,
    and index into `doc_chunks` (`doc_type='CODE_AST'`).
    """
    wt = Path(worktree_path)
    if not wt.exists():
        return []

    cmd = ["git", "-C", str(wt), "diff", "--name-only", f"{base_ref}..HEAD"]
    proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
    changed_files = [
        line.strip()
        for line in proc.stdout.splitlines()
        if line.strip() and not line.strip().endswith(".ws_lock.json")
    ]

    all_symbols: list[dict[str, Any]] = []
    for rel_file in changed_files:
        abs_file = wt / rel_file
        if abs_file.exists() and abs_file.is_file():
            file_symbols = extract_symbols_from_file(abs_file, rel_file_path=rel_file)
            all_symbols.extend(file_symbols)

    conn = get_connection(db_path)
    try:
        conn.execute("DELETE FROM code_artifacts WHERE effort_id = ?", (effort_id,))
        for sym in all_symbols:
            art_id = f"ast_{uuid.uuid4().hex[:10]}"
            sym["id"] = art_id
            sym["git_sha"] = head_sha
            conn.execute(
                """
                INSERT INTO code_artifacts (
                    id, workstream_id, effort_id, repo_path, git_sha,
                    file_path, symbol_name, symbol_kind, signature_and_body
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    art_id,
                    workstream_id,
                    effort_id,
                    str(repo_path),
                    head_sha,
                    sym["file_path"],
                    sym["symbol_name"],
                    sym["symbol_kind"],
                    sym["signature_and_body"],
                ),
            )
    finally:
        conn.close()

    if all_symbols:
        md_sections = [
            f"## {s['symbol_kind']} `{s['symbol_name']}` ({s['file_path']})\n"
            f"```\n{s['signature_and_body']}\n```"
            for s in all_symbols
        ]
        combined_md = "\n\n".join(md_sections)
        index_document_chunks(
            ws_id=workstream_id,
            effort_id=effort_id,
            doc_type="CODE_AST",
            file_path=str(wt),
            markdown_text=combined_md,
            git_sha=head_sha,
            replace_existing_for_type=True,
            db_path=db_path,
        )

    return all_symbols

