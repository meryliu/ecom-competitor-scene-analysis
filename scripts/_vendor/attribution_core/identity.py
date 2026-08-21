"""Version and source identity for embedded attribution runtimes."""
from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from .registry import REGISTRY_NAME, REGISTRY_VERSION

ENGINE_API_VERSION = "1.0.0"
CONTRACT_SCHEMA_VERSION = "1.3.0"
CORE_VERSION = "1.5.0"


def source_hash() -> str:
    package_root = Path(__file__).resolve().parent
    digest = hashlib.sha256()
    for path in sorted(package_root.glob("*.py"), key=lambda item: item.name):
        digest.update(path.name.encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def identity() -> dict[str, Any]:
    return {
        "name": REGISTRY_NAME,
        "engine_api_version": ENGINE_API_VERSION,
        "contract_schema_version": CONTRACT_SCHEMA_VERSION,
        "registry_version": REGISTRY_VERSION,
        "core_version": CORE_VERSION,
        "core_sha256": source_hash(),
    }
