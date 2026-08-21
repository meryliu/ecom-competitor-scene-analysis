#!/usr/bin/env python3
"""Persistent checkpoint helpers for the unified analysis runner."""
from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path
from typing import Any

from fact_contract import canonical_json


STATE_VERSION = "analysis_run_state/1.0"


def value_hash(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def atomic_write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{time.time_ns()}.tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def load_state(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    if not isinstance(value, dict) or value.get("schema_version") != STATE_VERSION:
        raise ValueError(f"unsupported run state: {path}")
    return value


def new_state(input_hash: str) -> dict[str, Any]:
    return {
        "schema_version": STATE_VERSION,
        "input_hash": input_hash,
        "status": "running",
        "stages": {},
        "artifacts": {},
        "fetch_attempts": [],
        "resume_decisions": [],
        "timings_ms": {},
    }


def artifact_record(path: Path) -> dict[str, Any]:
    return {"path": str(path), "sha256": file_hash(path), "bytes": path.stat().st_size}


def reusable_fetch(
    state: dict[str, Any] | None,
    *,
    request_hash: str,
    dimension_set_registry_hash: str,
) -> tuple[bool, str, Path | None]:
    if not state:
        return False, "missing_state", None
    if state.get("status") == "success":
        return False, "completed_run_requires_revision_check", None
    checkpoint = (state.get("stages") or {}).get("fetch")
    if not isinstance(checkpoint, dict) or checkpoint.get("status") != "success":
        return False, "fetch_checkpoint_missing", None
    if checkpoint.get("request_hash") != request_hash:
        return False, "request_hash_mismatch", None
    if checkpoint.get("dimension_set_registry_hash") != dimension_set_registry_hash:
        return False, "dimension_set_registry_hash_mismatch", None
    record = (state.get("artifacts") or {}).get("facts")
    if not isinstance(record, dict) or not record.get("path") or not record.get("sha256"):
        return False, "facts_artifact_missing", None
    path = Path(record["path"])
    if not path.exists() or file_hash(path) != record["sha256"]:
        return False, "facts_artifact_hash_mismatch", path
    if checkpoint.get("source_revision") is None:
        return False, "source_revision_missing", path
    return True, "successful_fetch_checkpoint", path
