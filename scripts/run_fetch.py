#!/usr/bin/env python3
"""Fetch competitor Feishu cells and persist canonical scene facts."""
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from competitor_fact_provider import fetch_facts
from dimension_domain_registry import DEFAULT_REGISTRY_PATH as DEFAULT_DIMENSION_SET_REGISTRY
from _vendor.ecom_competitor_source import DEFAULT_SOURCE_URL, error_json


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def atomic_write(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{time.time_ns()}.tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--request-file", required=True, type=Path)
    parser.add_argument("--facts", required=True, type=Path)
    parser.add_argument("--result", required=True, type=Path)
    parser.add_argument("--index", type=Path, help="optional index override; shared source cache is the default")
    parser.add_argument("--source-url", default=DEFAULT_SOURCE_URL)
    parser.add_argument("--identity", default="user", choices=["user", "bot"])
    parser.add_argument("--allow-stale", action="store_true")
    parser.add_argument("--dimension-set-registry", type=Path, default=DEFAULT_DIMENSION_SET_REGISTRY)
    args = parser.parse_args()
    started_at = utc_now()
    started = time.perf_counter()
    request_id = None
    try:
        request = json.loads(args.request_file.read_text(encoding="utf-8"))
        request_id = request.get("request_id")
        payload = fetch_facts(
            request,
            args.index,
            source_url=args.source_url,
            identity=args.identity,
            allow_stale=args.allow_stale,
            dimension_set_registry_path=args.dimension_set_registry,
        )
        atomic_write(args.facts, payload)
        result = {
            "request_id": request.get("request_id"),
            "status": "success",
            "started_at": started_at,
            "ended_at": utc_now(),
            "duration_ms": round((time.perf_counter() - started) * 1000, 3),
            "raw_bytes": len(json.dumps(payload, ensure_ascii=False).encode("utf-8")),
            "response_format": payload.get("schema_version"),
            "facts_artifact": str(args.facts),
            "source": payload.get("source", {}),
        }
        atomic_write(args.result, result)
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except Exception as exc:
        result = {
            "request_id": request_id,
            "status": "failed",
            "started_at": started_at,
            "ended_at": utc_now(),
            "duration_ms": round((time.perf_counter() - started) * 1000, 3),
            "error": json.loads(error_json(exc)),
        }
        atomic_write(args.result, result)
        print(json.dumps(result, ensure_ascii=False), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
