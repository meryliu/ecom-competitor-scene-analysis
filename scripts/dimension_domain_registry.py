#!/usr/bin/env python3
"""Resolve business-maintained named dimension sets into concrete source values."""
from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from pathlib import Path
from typing import Any


SCHEMA_VERSION = "dimension_set_registry/1.0"
DEFAULT_REGISTRY_PATH = (
    Path(__file__).resolve().parents[1] / "references" / "dimension-set-registry.json"
)


class DimensionDomainError(ValueError):
    def __init__(self, code: str, message: str, details: Any | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details


def normalize_token(value: Any) -> str:
    text = unicodedata.normalize("NFKC", str(value or ""))
    return re.sub(r"\s+", "", text).strip().lower()


def registry_hash(registry: dict[str, Any]) -> str:
    canonical = json.dumps(registry, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def load_dimension_set_registry(path: Path | None = None) -> dict[str, Any]:
    registry_path = path or DEFAULT_REGISTRY_PATH
    value = json.loads(registry_path.read_text(encoding="utf-8"))
    if not isinstance(value, dict) or value.get("schema_version") != SCHEMA_VERSION:
        raise DimensionDomainError(
            "invalid_dimension_set_registry",
            f"集合注册表必须使用 {SCHEMA_VERSION}",
        )
    sets = value.get("sets")
    if not isinstance(sets, dict):
        raise DimensionDomainError("invalid_dimension_set_registry", "集合注册表 sets 必须是对象")
    aliases: dict[tuple[str, str], str] = {}
    for set_id, definition in sets.items():
        if not isinstance(set_id, str) or not set_id or not isinstance(definition, dict):
            raise DimensionDomainError("invalid_dimension_set_registry", "集合定义必须有非空 ID 和对象定义")
        dimension = definition.get("dimension")
        members = definition.get("members")
        raw_aliases = definition.get("aliases", [])
        if not isinstance(dimension, str) or not dimension:
            raise DimensionDomainError("invalid_dimension_set_registry", f"集合 {set_id} 缺少 dimension")
        if not isinstance(members, list) or not members or not all(isinstance(item, str) and item for item in members):
            raise DimensionDomainError("invalid_dimension_set_registry", f"集合 {set_id} members 必须是非空字符串数组")
        if len({normalize_token(item) for item in members}) != len(members):
            raise DimensionDomainError("invalid_dimension_set_registry", f"集合 {set_id} 包含重复成员")
        if not isinstance(raw_aliases, list) or not all(isinstance(item, str) and item for item in raw_aliases):
            raise DimensionDomainError("invalid_dimension_set_registry", f"集合 {set_id} aliases 必须是字符串数组")
        for alias in [set_id, *raw_aliases]:
            key = (normalize_token(dimension), normalize_token(alias))
            previous = aliases.get(key)
            if previous is not None and previous != set_id:
                raise DimensionDomainError(
                    "duplicate_dimension_set_alias",
                    f"集合别名 {alias} 同时指向 {previous} 和 {set_id}",
                )
            aliases[key] = set_id
    return value


def _set_aliases(registry: dict[str, Any], dimension: str) -> dict[str, str]:
    aliases: dict[str, str] = {}
    for set_id, definition in registry.get("sets", {}).items():
        if normalize_token(definition.get("dimension")) != normalize_token(dimension):
            continue
        for alias in [set_id, *(definition.get("aliases") or [])]:
            aliases[normalize_token(alias)] = set_id
    return aliases


def is_dimension_domain(
    dimension: str,
    requested: Any,
    registry: dict[str, Any],
) -> bool:
    if isinstance(requested, list):
        return True
    return normalize_token(requested) in _set_aliases(registry, dimension)


def dimension_domain_ref(
    dimension: str,
    requested: Any,
    registry: dict[str, Any],
) -> str:
    raw_values = requested if isinstance(requested, list) else [requested]
    aliases = _set_aliases(registry, dimension)
    canonical_tokens = []
    for value in raw_values:
        token = normalize_token(value)
        set_id = aliases.get(token)
        canonical_tokens.append({"set": set_id} if set_id else {"value": token})
    identity = {
        "dimension": normalize_token(dimension),
        "tokens": sorted(canonical_tokens, key=lambda item: json.dumps(item, sort_keys=True)),
    }
    digest = hashlib.sha256(
        json.dumps(identity, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()[:20]
    return f"domain_{digest}"


def source_dimension_domain_ref(dimension: str) -> str:
    """Return the stable identity for all current members of a physical dimension."""
    identity = {
        "kind": "source_dimension_all",
        "dimension": normalize_token(dimension),
    }
    digest = hashlib.sha256(
        json.dumps(identity, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()[:20]
    return f"domain_{digest}"


def resolve_dimension_domain(
    dimension: str,
    requested: Any,
    available_values: list[str],
    registry: dict[str, Any],
    *,
    value_aliases: dict[str, str] | None = None,
) -> dict[str, Any]:
    raw_values = requested if isinstance(requested, list) else [requested]
    if not raw_values:
        raise DimensionDomainError("empty_dimension_domain", f"维度 {dimension} 的选择域不能为空")
    if not all(isinstance(item, str) and item for item in raw_values):
        raise DimensionDomainError("invalid_dimension_domain", f"维度 {dimension} 的选择域必须是字符串")

    available_by_token = {normalize_token(value): value for value in available_values}
    aliases = _set_aliases(registry, dimension)
    normalized_value_aliases = {
        normalize_token(alias): target for alias, target in (value_aliases or {}).items()
    }
    members: list[str] = []
    set_ids: list[str] = []

    def append_member(value: str, source: str) -> None:
        token = normalize_token(value)
        resolved = available_by_token.get(token)
        if resolved is None:
            alias_target = normalized_value_aliases.get(token)
            if alias_target is not None:
                resolved = available_by_token.get(normalize_token(alias_target))
        if resolved is None:
            raise DimensionDomainError(
                "unavailable_dimension_domain_member",
                f"维度集合成员不在当前源表元信息中：{value}",
                {"dimension": dimension, "member": value, "source": source, "available": available_values},
            )
        if resolved not in members:
            members.append(resolved)

    for value in raw_values:
        token = normalize_token(value)
        set_id = aliases.get(token)
        if set_id is not None:
            if token in available_by_token:
                raise DimensionDomainError(
                    "ambiguous_dimension_domain",
                    f"{value} 同时是维度值和集合别名",
                    {"dimension": dimension, "value": value, "set_id": set_id},
                )
            definition = registry["sets"][set_id]
            set_ids.append(set_id)
            for member in definition["members"]:
                append_member(member, set_id)
        else:
            append_member(value, "explicit")

    result = {
        "dimension": dimension,
        "requested_values": list(raw_values),
        "members": members,
        "set_ids": list(dict.fromkeys(set_ids)),
        "registry_hash": registry_hash(registry),
    }
    if is_dimension_domain(dimension, requested, registry):
        result["domain_id"] = dimension_domain_ref(dimension, requested, registry)
    return result
