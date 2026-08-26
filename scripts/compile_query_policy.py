#!/usr/bin/env python3
"""Compile an approved Query Policy review bundle into an isolated build directory."""
from __future__ import annotations

import argparse
import json
import os
import tempfile
from copy import deepcopy
from pathlib import Path
from typing import Any

from query_policy_runtime import (
    POLICY_INDEX_SCHEMA,
    POLICY_MANIFEST_SCHEMA,
    POLICY_RULE_SCHEMA,
    load_policy,
    validate_policy,
    value_hash,
)


class QueryPolicyCompileError(ValueError):
    pass


def _load(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise QueryPolicyCompileError(f"cannot read {path}") from exc
    if not isinstance(value, dict):
        raise QueryPolicyCompileError(f"{path} must contain an object")
    return value


def _write(path: Path, value: dict[str, Any]) -> None:
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


def _runtime_rule(source: dict[str, Any], policy_version: str) -> dict[str, Any]:
    rule = {
        "schema_version": POLICY_RULE_SCHEMA,
        "policy_version": policy_version,
        "rule_id": source.get("rule_id"),
        "name": source.get("name"),
        "version": source.get("version"),
        "priority": source.get("priority"),
        "routing": deepcopy(source.get("routing") or {}),
        "applicability": deepcopy(source.get("applicability") or {}),
        "actions": [],
        "boundaries": deepcopy(source.get("boundaries") or []),
        "source_ref": deepcopy(source.get("source_ref") or {}),
    }
    for key in ("relations", "user_explicit_protection"):
        if key in source:
            rule[key] = deepcopy(source[key])
    for source_action in source.get("actions") or []:
        if not isinstance(source_action, dict):
            raise QueryPolicyCompileError(
                f"{source.get('rule_id')} contains a non-object action"
            )
        action = deepcopy(source_action)
        if not isinstance(action.get("action_id"), str) or not action["action_id"]:
            raise QueryPolicyCompileError(
                f"{source.get('rule_id')} action_id is required for stable idempotent deduplication"
            )
        action.setdefault("idempotency", "application_key")
        rule["actions"].append(action)
    return rule


def compile_review_bundle(
    review_dir: Path, output_dir: Path, policy_version: str,
) -> dict[str, Any]:
    if output_dir.exists() and any(output_dir.iterdir()):
        raise QueryPolicyCompileError("output directory must not exist or must be empty")
    proposed = review_dir / "proposed"
    source_bundle = _load(proposed / "compiled-rules.json")
    source_index = _load(proposed / "policy-index.json")
    source_manifest = _load(proposed / "source-manifest.json")
    active_sources = [
        item for item in source_bundle.get("rules") or []
        if isinstance(item, dict) and item.get("status") == "active"
    ]
    rules = {
        str(item.get("rule_id")): _runtime_rule(item, policy_version)
        for item in active_sources
    }
    index = {
        "schema_version": POLICY_INDEX_SCHEMA,
        "policy_version": policy_version,
        "source_revision": source_manifest.get("revision_id"),
        "global_invariants": deepcopy(source_index.get("global_invariants") or []),
        "always_rule_ids": ["user-explicit-priority"],
        "active_rule_ids": list(rules),
        "routing": deepcopy(source_index.get("routing") or {}),
        "limits": {
            "max_selected_rules": 8,
            "max_dependency_depth": 4,
            "max_application_rounds": 8,
            "max_expanded_requirements": 20,
            "max_packet_bytes": 8192,
        },
    }
    manifest = {
        "schema_version": POLICY_MANIFEST_SCHEMA,
        "policy_version": policy_version,
        "source_revision": source_manifest.get("revision_id"),
        "source_url": source_manifest.get("source_url"),
        "document_id": source_manifest.get("document_id"),
        "document_sha256": source_manifest.get("document_sha256"),
        "index_sha256": value_hash(index),
        "rule_hashes": {rule_id: value_hash(rule) for rule_id, rule in rules.items()},
    }
    manifest["policy_sha256"] = value_hash({"index": index, "rules": rules})
    validate_policy(index, manifest, rules)
    output_dir.mkdir(parents=True, exist_ok=True)
    _write(output_dir / "policy-index.json", index)
    _write(output_dir / "policy-manifest.json", manifest)
    for rule_id, rule in rules.items():
        _write(output_dir / "rules" / f"{rule_id}.json", rule)
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--review-dir", type=Path)
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--policy-version", default="query-policy/1.0.0")
    parser.add_argument("--check", type=Path)
    args = parser.parse_args()
    if args.check:
        _, manifest, rules, _ = load_policy(args.check)
        print(json.dumps({"status": "valid", "rules": len(rules), "policy_sha256": manifest["policy_sha256"]}))
        return 0
    if not args.review_dir or not args.output_dir:
        parser.error("--review-dir and --output-dir are required unless --check is used")
    manifest = compile_review_bundle(args.review_dir, args.output_dir, args.policy_version)
    print(json.dumps({"status": "compiled", "output": str(args.output_dir), "policy_sha256": manifest["policy_sha256"]}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
