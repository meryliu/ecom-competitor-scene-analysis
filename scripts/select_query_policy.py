#!/usr/bin/env python3
"""Select a fail-open, context-bounded Query Policy packet."""
from __future__ import annotations

import argparse
import json
import os
import tempfile
from pathlib import Path

from query_policy_runtime import POLICY_ROOT, select_query_policy_fail_open


def _write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True, help="JSON object containing raw_query")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--policy-root", type=Path, default=POLICY_ROOT)
    args = parser.parse_args()
    try:
        value = json.loads(args.input.read_text(encoding="utf-8"))
        query = value.get("raw_query") if isinstance(value, dict) else ""
    except Exception:
        query = ""
    result = select_query_policy_fail_open(query, root=args.policy_root)
    _write_json(args.output, result)
    print(json.dumps({"status": result["status"], "output": str(args.output)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
