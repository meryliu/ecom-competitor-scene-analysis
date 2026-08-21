#!/usr/bin/env python3
"""Inspect one result from a reference-based scene-analysis execution manifest."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Any


class InspectionError(ValueError):
    pass


def reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    output: dict[str, Any] = {}
    for key, value in pairs:
        if key in output:
            raise InspectionError(f"duplicate JSON key: {key}")
        output[key] = value
    return output


def load_manifest(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=reject_duplicate_keys)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise InspectionError(f"cannot read manifest: {exc}") from exc
    if not isinstance(value, dict) or (value.get("storage") or {}).get("mode") != "reference":
        raise InspectionError("manifest does not use reference storage")
    return value


def artifact_path(base_dir: Path, metadata: dict[str, Any]) -> Path:
    raw_path = metadata.get("path")
    if not isinstance(raw_path, str) or not raw_path or Path(raw_path).is_absolute():
        raise InspectionError("artifact path must be a non-empty relative path")
    base = base_dir.resolve()
    path = (base / raw_path).resolve()
    try:
        path.relative_to(base)
    except ValueError as exc:
        raise InspectionError("artifact path escapes the manifest directory") from exc
    return path


def verify_artifact(base_dir: Path, metadata: dict[str, Any]) -> Path:
    path = artifact_path(base_dir, metadata)
    digest = hashlib.sha256()
    byte_count = 0
    try:
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                byte_count += len(chunk)
                digest.update(chunk)
    except OSError as exc:
        raise InspectionError(f"cannot read artifact {path}: {exc}") from exc
    if byte_count != metadata.get("bytes") or digest.hexdigest() != metadata.get("sha256"):
        raise InspectionError(f"artifact integrity check failed: {path}")
    return path


def artifact_metadata(manifest: dict[str, Any], artifact_id: str, expected_format: str) -> dict[str, Any]:
    artifacts = manifest.get("artifacts")
    metadata = artifacts.get(artifact_id) if isinstance(artifacts, dict) else None
    if not isinstance(metadata, dict) or metadata.get("format") != expected_format:
        raise InspectionError(f"missing {expected_format} artifact: {artifact_id}")
    return metadata


def load_index(manifest: dict[str, Any], base_dir: Path) -> dict[str, Any]:
    index_ref = manifest.get("result_index")
    artifact_id = index_ref.get("artifact_id") if isinstance(index_ref, dict) else None
    if not isinstance(artifact_id, str):
        raise InspectionError("manifest has no result_index artifact reference")
    metadata = artifact_metadata(manifest, artifact_id, "json")
    path = verify_artifact(base_dir, metadata)
    try:
        value = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=reject_duplicate_keys)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise InspectionError(f"cannot parse result index: {exc}") from exc
    if not isinstance(value, dict) or len(value) != metadata.get("records"):
        raise InspectionError("result index shape or record count is invalid")
    return value


def read_jsonl_line(path: Path, line_index: int, expected_records: int) -> dict[str, Any]:
    if not isinstance(line_index, int) or isinstance(line_index, bool) or line_index < 0:
        raise InspectionError("result reference line must be a non-negative integer")
    selected: Any = None
    records = 0
    try:
        with path.open("r", encoding="utf-8") as stream:
            for line in stream:
                if not line.strip():
                    continue
                if records == line_index:
                    selected = json.loads(line, object_pairs_hook=reject_duplicate_keys)
                records += 1
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise InspectionError(f"cannot parse result artifact: {exc}") from exc
    if records != expected_records:
        raise InspectionError("result artifact record count is invalid")
    if not isinstance(selected, dict):
        raise InspectionError(f"result reference line is absent: {line_index}")
    return selected


def inspect_key(manifest: dict[str, Any], base_dir: Path, index: dict[str, Any], key: str) -> dict[str, Any]:
    entry = index.get(key)
    if not isinstance(entry, dict):
        raise InspectionError(f"result key not found: {key}")
    result_ref = entry.get("result_ref")
    artifact_id = result_ref.get("artifact_id") if isinstance(result_ref, dict) else None
    line = result_ref.get("line") if isinstance(result_ref, dict) else None
    if not isinstance(artifact_id, str):
        raise InspectionError(f"result key has no artifact reference: {key}")
    metadata = artifact_metadata(manifest, artifact_id, "jsonl")
    path = verify_artifact(base_dir, metadata)
    record = read_jsonl_line(path, line, metadata.get("records"))
    return {"key": key, "index": entry, "record": record}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", required=True, type=Path)
    selection = parser.add_mutually_exclusive_group(required=True)
    selection.add_argument("--list", action="store_true", help="list result keys and lightweight index entries")
    selection.add_argument("--key", help="read one exact result key")
    selection.add_argument("--node-id", help="read a node key and list its fanout child keys")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        manifest = load_manifest(args.manifest)
        index = load_index(manifest, args.manifest.parent)
        if args.list:
            output: Any = {"records": len(index), "results": index}
        elif args.key:
            output = inspect_key(manifest, args.manifest.parent, index, args.key)
        else:
            matching = sorted(key for key in index if key == args.node_id or key.startswith(f"{args.node_id}::"))
            if not matching:
                raise InspectionError(f"node ID not found: {args.node_id}")
            output = {
                "node_id": args.node_id,
                "keys": matching,
                "node_result": inspect_key(manifest, args.manifest.parent, index, args.node_id)
                if args.node_id in index
                else None,
            }
        print(json.dumps(output, ensure_ascii=False, indent=2))
        return 0
    except InspectionError as exc:
        print(json.dumps({"status": "blocked", "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
