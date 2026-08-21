#!/usr/bin/env python3
"""Shared cache, singleflight and bounded source-call retry policy."""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import random
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from _vendor.ecom_competitor_source import (
    DEFAULT_SOURCE_URL,
    LarkClient,
    SkillError,
    ensure_fresh_index,
)


def source_key(source_url: str, identity: str) -> str:
    value = json.dumps([source_url, identity], ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:24]


def shared_cache_root() -> Path:
    configured = os.environ.get("ECOM_COMPETITOR_SCENE_CACHE_DIR")
    if configured:
        return Path(configured).expanduser().resolve()
    base = os.environ.get("XDG_CACHE_HOME")
    root = Path(base).expanduser() if base else Path.home() / ".cache"
    return root / "ecom-competitor-scene-analysis"


def shared_index_path(
    source_url: str = DEFAULT_SOURCE_URL,
    identity: str = "user",
    config_hash: str | None = None,
) -> Path:
    cache_identity = identity if not config_hash else f"{identity}:{config_hash}"
    return shared_cache_root() / source_key(source_url, cache_identity) / "index.json"


@contextmanager
def file_lock(path: Path) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _retryable(exc: SkillError) -> bool:
    if exc.code == "source_timeout":
        return True
    if exc.code != "source_read_failed":
        return False
    detail = json.dumps(exc.details or {}, ensure_ascii=False).lower()
    message = str(exc).lower()
    markers = ("frequency", "rate limit", "too many", "429", "500", "502", "503", "504", "timeout", "限频", "频率")
    return any(marker in detail or marker in message for marker in markers)


class ManagedLarkClient(LarkClient):
    """Lark client with a source-level cross-process pace and bounded retry."""

    def __init__(
        self,
        identity: str = "user",
        timeout: int = 40,
        *,
        runtime_dir: Path | None = None,
        min_interval: float = 0.35,
        max_attempts: int = 3,
    ) -> None:
        super().__init__(identity=identity, timeout=timeout)
        self.runtime_dir = runtime_dir or shared_cache_root() / "calls"
        self.min_interval = max(0.0, min_interval)
        self.max_attempts = max(1, max_attempts)
        self.call_attempts: list[dict[str, Any]] = []

    def _paced_run(self, args: list[str]) -> dict[str, Any]:
        lock_path = self.runtime_dir / f"{self.identity}.rate.lock"
        stamp_path = self.runtime_dir / f"{self.identity}.last-call"
        with file_lock(lock_path):
            previous = 0.0
            try:
                previous = float(stamp_path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                pass
            wait = self.min_interval - max(0.0, time.time() - previous)
            if wait > 0:
                time.sleep(wait)
            try:
                return super()._run(args)
            finally:
                stamp_path.parent.mkdir(parents=True, exist_ok=True)
                stamp_path.write_text(str(time.time()), encoding="utf-8")

    def _run(self, args: list[str]) -> dict[str, Any]:
        operation = " ".join(args[:2])
        for attempt in range(1, self.max_attempts + 1):
            started = time.perf_counter()
            record: dict[str, Any] = {"operation": operation, "attempt": attempt}
            try:
                result = self._paced_run(args)
                record.update({"status": "success", "duration_ms": round((time.perf_counter() - started) * 1000, 3)})
                self.call_attempts.append(record)
                return result
            except SkillError as exc:
                retry = attempt < self.max_attempts and _retryable(exc)
                record.update({
                    "status": "retrying" if retry else "failed",
                    "error_code": exc.code,
                    "duration_ms": round((time.perf_counter() - started) * 1000, 3),
                })
                self.call_attempts.append(record)
                if not retry:
                    raise
                time.sleep(min(2.0, 0.25 * (2 ** (attempt - 1))) + random.uniform(0, 0.1))
        raise AssertionError("unreachable")


def ensure_shared_index(
    client: LarkClient,
    source_url: str = DEFAULT_SOURCE_URL,
    *,
    identity: str = "user",
    index_path: Path | None = None,
    allow_stale: bool = False,
    sheet_roles: dict[str, list[str]] | None = None,
    config_hash: str | None = None,
) -> tuple[dict[str, Any], str, Path]:
    path = index_path or shared_index_path(source_url, identity, config_hash)
    with file_lock(path.with_suffix(".build.lock")):
        kwargs: dict[str, Any] = {"allow_stale": allow_stale}
        if sheet_roles is not None:
            kwargs["role_names"] = sheet_roles
        index, status = ensure_fresh_index(path, client, source_url, **kwargs)
    return index, status, path
