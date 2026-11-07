#!/usr/bin/env python3
"""Pre-read interceptor hook enforcing SHUNT_MIN_LINES (default 350 lines)."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# Allow running directly from scripts/ or when installed
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from workstream_hub.shunt import check_file_gate  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Enforce SHUNT_MIN_LINES gate before reading files into main agent context."
    )
    parser.add_argument("file_path", help="Path to the file being read")
    parser.add_argument("--offset", type=int, default=None, help="Starting line offset")
    parser.add_argument("--limit", type=int, default=None, help="Max lines to read")
    parser.add_argument("--effort-id", default=None, help="Optional Effort ID for telemetry")
    args = parser.parse_args(argv)

    result = check_file_gate(
        file_path=args.file_path,
        offset=args.offset,
        limit=args.limit,
        effort_id=args.effort_id,
    )
    print(json.dumps(result))
    return 0 if result.get("allowed", False) else 2


if __name__ == "__main__":
    raise SystemExit(main())

