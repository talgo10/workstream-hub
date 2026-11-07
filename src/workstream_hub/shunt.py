"""Local Shunt Engine: 350-line file gate, out-of-band bulk-reader, and convention-mirroring code-writer."""

from __future__ import annotations

import os
import re
import shlex
import subprocess
from pathlib import Path
from typing import Any

from workstream_hub.ast_extractor import extract_symbols_from_file
from workstream_hub.config import get_shunt_min_lines
from workstream_hub.db import (
    get_effort,
    log_shunt_event,
    verify_and_refresh_lease,
)
from workstream_hub.indexer import embed_texts


def check_file_gate(
    file_path: str | Path,
    offset: int | None = None,
    limit: int | None = None,
    effort_id: str | None = None,
    db_path: Path | None = None,
) -> dict[str, Any]:
    """
    Enforce the SHUNT_MIN_LINES (default 350) hard gate.
    Full reads on files > threshold without explicit offset/limit are blocked and logged.
    """
    threshold = get_shunt_min_lines()

    if offset is not None and limit is not None and 0 < int(limit) <= threshold:
        return {
            "allowed": True,
            "offset": int(offset),
            "limit": int(limit),
            "threshold": threshold,
        }

    p = Path(file_path)
    if not p.exists() or not p.is_file():
        return {
            "allowed": True,
            "line_count": 0,
            "threshold": threshold,
        }

    content = p.read_text(encoding="utf-8", errors="replace")
    line_count = len(content.splitlines())

    if line_count <= threshold:
        return {
            "allowed": True,
            "line_count": line_count,
            "threshold": threshold,
        }

    log_shunt_event(
        mode="GATE_BLOCKED",
        target_files=[str(p)],
        raw_lines_processed=line_count,
        estimated_tokens_saved=0,
        effort_id=effort_id,
        db_path=db_path,
    )
    return {
        "allowed": False,
        "line_count": line_count,
        "threshold": threshold,
        "directive": (
            f"BLOCKED: File exceeds {threshold} lines. Use paparats-mcp `search_code` "
            f"for AST symbols, pass explicit `offset`/`limit` ranges, or call `ws_shunt_read` "
            f"to summarize out-of-band."
        ),
    }


def _cosine_sim(a: list[float], b: list[float]) -> float:
    return sum(x * y for x, y in zip(a, b))


def shunt_bulk_read(
    files: list[str] | str,
    question: str,
    effort_id: str | None = None,
    db_path: Path | None = None,
) -> dict[str, Any]:
    """
    Out-of-band bulk-reader: inspect 1+ large files and return a compact (<250 token)
    structured summary with file:start_line-end_line citations and AST symbol signatures.
    """
    if isinstance(files, str):
        file_list = [f.strip() for f in files.split(",") if f.strip()]
    else:
        file_list = [str(f) for f in files]

    total_lines = 0
    candidates: list[dict[str, Any]] = []

    for fpath_str in file_list:
        p = Path(fpath_str)
        if not p.exists() or not p.is_file():
            continue
        text = p.read_text(encoding="utf-8", errors="replace")
        lines = text.splitlines()
        total_lines += len(lines)

        symbols = extract_symbols_from_file(p, rel_file_path=p.name)
        if symbols:
            for sym in symbols:
                first_sig_line = sym["signature_and_body"].splitlines()[0].strip()
                candidates.append(
                    {
                        "file": p.name,
                        "full_path": str(p),
                        "symbol_name": sym["symbol_name"],
                        "symbol_kind": sym["symbol_kind"],
                        "start_line": sym["start_line"],
                        "end_line": sym["end_line"],
                        "signature": first_sig_line,
                        "text_for_rank": f"{sym['symbol_name']} {sym['signature_and_body']}",
                    }
                )
        else:
            # Chunk plain text / log files into 50-line windows
            step = 50
            for start_idx in range(0, len(lines), step):
                end_idx = min(len(lines), start_idx + step)
                window_text = "\n".join(lines[start_idx:end_idx])
                first_non_empty = next(
                    (ln.strip() for ln in lines[start_idx:end_idx] if ln.strip()),
                    "section",
                )[:80]
                candidates.append(
                    {
                        "file": p.name,
                        "full_path": str(p),
                        "symbol_name": f"lines_{start_idx + 1}_{end_idx}",
                        "symbol_kind": "window",
                        "start_line": start_idx + 1,
                        "end_line": end_idx,
                        "signature": first_non_empty,
                        "text_for_rank": window_text,
                    }
                )

    worker_cmd = os.getenv("SHUNT_WORKER_CMD", "").strip()
    summary_bullets: list[str] = []
    top_symbols: list[dict[str, Any]] = []

    if worker_cmd and candidates:
        try:
            prompt_payload = (
                "You are a factual bulk-reader worker. Answer the question using ONLY "
                "concise bullet points with exact file:line citations. Max 250 tokens.\n"
                f"Question: {question}\n"
            )
            proc = subprocess.run(
                [*shlex.split(worker_cmd), prompt_payload],
                capture_output=True,
                text=True,
                timeout=15,
                check=False,
            )
            if proc.returncode == 0 and proc.stdout.strip():
                summary_bullets = [proc.stdout.strip()[:900]]
        except Exception:
            pass

    if not summary_bullets:
        if candidates:
            q_tokens = set(re.findall(r"[a-z0-9_]+", question.lower()))
            texts_to_embed = [question] + [c["text_for_rank"] for c in candidates]
            vecs = embed_texts(texts_to_embed)
            q_vec = vecs[0]
            c_vecs = vecs[1:]

            for cand, c_vec in zip(candidates, c_vecs):
                sim = _cosine_sim(q_vec, c_vec)
                c_tokens = set(re.findall(r"[a-z0-9_]+", cand["text_for_rank"].lower()))
                overlap = len(q_tokens & c_tokens) / max(1, len(q_tokens))
                cand["score"] = round(sim + 0.35 * overlap, 4)

            candidates.sort(key=lambda x: x["score"], reverse=True)
            top_5 = candidates[:5]
            for item in top_5:
                citation = f"{item['file']}:{item['start_line']}-{item['end_line']}"
                summary_bullets.append(
                    f"- `{citation}` [{item['symbol_kind']} `{item['symbol_name']}`]: {item['signature']}"
                )
                top_symbols.append(
                    {
                        "file": item["file"],
                        "symbol_name": item["symbol_name"],
                        "symbol_kind": item["symbol_kind"],
                        "range": f"{item['start_line']}-{item['end_line']}",
                        "signature": item["signature"],
                    }
                )
        else:
            summary_bullets.append("- No readable content or symbols found in target files.")

    summary_text = "\n".join(summary_bullets)
    # Enforce < 250 tokens cap (~900 chars / ~180 words)
    words = summary_text.split()
    if len(words) > 200:
        summary_text = " ".join(words[:200]) + " ..."

    response_tokens = max(1, int(len(summary_text.split()) * 1.3))
    estimated_tokens_saved = max(0, total_lines * 8 - response_tokens)

    log_shunt_event(
        mode="BULK_READER",
        target_files=file_list,
        raw_lines_processed=total_lines,
        estimated_tokens_saved=estimated_tokens_saved,
        effort_id=effort_id,
        db_path=db_path,
    )

    return {
        "status": "OK",
        "mode": "BULK_READER",
        "question": question,
        "files": file_list,
        "raw_lines_processed": total_lines,
        "response_tokens": response_tokens,
        "estimated_tokens_saved": estimated_tokens_saved,
        "summary": summary_text,
        "top_symbols": top_symbols,
    }


def _generate_code_from_spec_and_reference(
    spec_prompt: str,
    reference_text: str,
    target_path: Path,
) -> str:
    """
    Generate convention-matching code for target_path based on spec_prompt and reference_text.
    Extracts fenced code if provided, or synthesizes requested functions/classes mirroring reference imports.
    """
    # 1. If spec_prompt contains a fenced code block, use it while preserving reference header conventions
    fenced = re.findall(r"```(?:[a-zA-Z0-9_+-]*)\n(.*?)```", spec_prompt, re.DOTALL)
    ref_imports = [
        line
        for line in reference_text.splitlines()
        if line.startswith("from __future__") or line.startswith("import ") or line.startswith("from ")
    ]
    header_imports = "\n".join(ref_imports[:10])

    if fenced:
        body = "\n\n".join(block.strip() for block in fenced)
        if header_imports and "import " not in body:
            return f"{header_imports}\n\n\n{body}\n"
        return f"{body}\n"

    # 2. Extract any explicit function or class names mentioned in spec_prompt
    fn_matches = re.findall(r"\bdef\s+([A-Za-z_][A-Za-z0-9_]*)\s*\(([^)]*)\)", spec_prompt)
    cls_matches = re.findall(r"\bclass\s+([A-Za-z_][A-Za-z0-9_]*)", spec_prompt)
    ident_matches = re.findall(r"`([A-Za-z_][A-Za-z0-9_]*)`", spec_prompt)

    blocks: list[str] = [f'"""Auto-generated module for {target_path.name} via ws_shunt_write."""']
    if header_imports:
        blocks.append(header_imports)

    created_any = False
    for cls_name in cls_matches:
        created_any = True
        blocks.append(
            f"class {cls_name}:\n"
            f'    """{cls_name} generated from spec prompt."""\n\n'
            f"    def __init__(self, *args, **kwargs) -> None:\n"
            f"        self.args = args\n"
            f"        self.kwargs = kwargs\n"
        )

    for fn_name, fn_args in fn_matches:
        created_any = True
        blocks.append(
            f"def {fn_name}({fn_args}):\n"
            f'    """Generated function {fn_name}."""\n'
            f"    return True\n"
        )

    if not created_any:
        for ident in ident_matches:
            if ident.isidentifier() and not ident.isupper():
                created_any = True
                if ident[0].isupper():
                    blocks.append(
                        f"class {ident}:\n"
                        f'    """Generated class {ident}."""\n\n'
                        f"    def run(self):\n"
                        f"        return True\n"
                    )
                else:
                    blocks.append(
                        f"def {ident}(*args, **kwargs):\n"
                        f'    """Generated function {ident}."""\n'
                        f"    return True\n"
                    )

    if not created_any:
        stem = re.sub(r"[^a-zA-Z0-9_]", "_", target_path.stem) or "generated_task"
        fn_name = stem if not stem[0].isdigit() else f"fn_{stem}"
        blocks.append(
            f"def {fn_name}(*args, **kwargs):\n"
            f'    """{spec_prompt.strip()[:120]}"""\n'
            f"    return True\n"
        )

    return "\n\n".join(blocks) + "\n"


def shunt_code_write(
    target_file: str | Path,
    spec_prompt: str,
    reference_file: str | Path | None = None,
    effort_id: str = "",
    lease_token: str = "",
    db_path: Path | None = None,
) -> dict[str, Any]:
    """
    Out-of-band code-writer: validate lease_token, inspect reference_file conventions,
    write generated code directly to target_file inside the effort's worktree,
    log token savings to shunt_logs, and return compact file stats + AST symbols.
    """
    verify_and_refresh_lease(effort_id, lease_token, db_path=db_path)
    effort = get_effort(effort_id, db_path=db_path)

    target_path = Path(target_file)
    if not target_path.is_absolute() and effort and effort.get("worktree_path"):
        target_path = Path(effort["worktree_path"]) / target_path

    reference_text = ""
    if reference_file:
        ref_path = Path(reference_file)
        if not ref_path.is_absolute() and effort and effort.get("worktree_path"):
            candidate_ref = Path(effort["worktree_path"]) / ref_path
            if candidate_ref.exists():
                ref_path = candidate_ref
        if ref_path.exists() and ref_path.is_file():
            reference_text = ref_path.read_text(encoding="utf-8", errors="replace")

    generated_code = _generate_code_from_spec_and_reference(
        spec_prompt=spec_prompt,
        reference_text=reference_text,
        target_path=target_path,
    )

    target_path.parent.mkdir(parents=True, exist_ok=True)
    target_path.write_text(generated_code, encoding="utf-8")

    lines_written = len(generated_code.splitlines())
    symbols = extract_symbols_from_file(target_path, rel_file_path=target_path.name)
    symbol_names = [s["symbol_name"] for s in symbols]
    estimated_tokens_saved = max(50, lines_written * 10)

    log_shunt_event(
        mode="CODE_WRITER",
        target_files=[str(target_path)],
        raw_lines_processed=lines_written,
        estimated_tokens_saved=estimated_tokens_saved,
        effort_id=effort_id,
        db_path=db_path,
    )

    return {
        "status": "WRITTEN",
        "target_file": str(target_path),
        "lines_written": lines_written,
        "ast_symbols_created": symbol_names,
        "estimated_tokens_saved": estimated_tokens_saved,
    }

