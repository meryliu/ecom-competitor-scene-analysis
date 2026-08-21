from __future__ import annotations

import json
import sys
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import source_runtime  # noqa: E402
from _vendor.ecom_competitor_source import (  # noqa: E402
    SkillError,
    parse_metric_metadata,
    select_standard_sheets,
)
import csv  # noqa: E402
import io  # noqa: E402


class SourceRuntimeTests(unittest.TestCase):
    def test_metric_metadata_parses_supported_grain_and_aggregation_mode(self) -> None:
        rows = list(csv.reader(io.StringIO(
            "指标名称,指标别名,数值单位,可支持时间粒度,可支持拆解维度,聚合方式,口径备注\n"
            "支付GMV,,,月度,TOP6平台,可聚合，可做加减乘除,\n"
            "MAC,,,月度,TOP6平台,不可聚合,\n"
        )))
        metrics = parse_metric_metadata(rows)
        self.assertEqual(metrics["支付GMV"]["supported_grains"], ["month"])
        self.assertEqual(metrics["支付GMV"]["aggregation_mode"], "additive")
        self.assertEqual(metrics["MAC"]["aggregation_mode"], "non_additive")
    def test_only_standard_sheets_are_selected(self) -> None:
        sheets = [
            {"sheet_id": "m", "sheet_name": "指标元信息"},
            {"sheet_id": "d", "sheet_name": " 维度元信息 "},
            {"sheet_id": "month", "sheet_name": "月度表"},
            {"sheet_id": "quarter", "sheet_name": "季度表"},
            {"sheet_id": "other", "sheet_name": "收入_引用"},
        ]
        selected = select_standard_sheets(sheets)
        self.assertEqual(
            {item["sheet_id"] for item in selected.values()},
            {"m", "d", "month", "quarter"},
        )
        self.assertNotIn("other", {item["sheet_id"] for item in selected.values()})

    def test_duplicate_standard_sheet_is_rejected(self) -> None:
        sheets = [
            {"sheet_id": "m1", "sheet_name": "指标元信息"},
            {"sheet_id": "m2", "sheet_name": " 指标元信息 "},
            {"sheet_id": "d", "sheet_name": "维度元信息"},
        ]
        with self.assertRaises(SkillError) as raised:
            select_standard_sheets(sheets)
        self.assertEqual(raised.exception.code, "duplicate_standard_sheet")

    def test_configured_sheet_role_names_are_used(self) -> None:
        sheets = [
            {"sheet_id": "m", "sheet_name": "指标字典"},
            {"sheet_id": "d", "sheet_name": "维度字典"},
            {"sheet_id": "month", "sheet_name": "月事实"},
        ]
        selected = select_standard_sheets(sheets, {
            "metric_metadata": ["指标字典"],
            "dimension_metadata": ["维度字典"],
            "month": ["月事实"],
        })
        self.assertEqual(selected["month"]["sheet_id"], "month")

    def test_retry_is_bounded_and_only_for_transient_errors(self) -> None:
        client = source_runtime.ManagedLarkClient(min_interval=0, max_attempts=3)
        retryable = SkillError("source_read_failed", "frequency limit", {"status": 429})
        with patch.object(client, "_paced_run", side_effect=[retryable, {"ok": True}]), patch.object(
            source_runtime.time, "sleep", return_value=None
        ):
            self.assertEqual(client._run(["sheets", "+revision-get"]), {"ok": True})
        self.assertEqual([item["status"] for item in client.call_attempts], ["retrying", "success"])

        semantic = source_runtime.ManagedLarkClient(min_interval=0, max_attempts=3)
        with patch.object(semantic, "_paced_run", side_effect=SkillError("unknown_metric", "ambiguous")):
            with self.assertRaises(SkillError):
                semantic._run(["sheets", "+csv-get"])
        self.assertEqual(len(semantic.call_attempts), 1)

    def test_shared_index_rebuild_is_singleflight(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "index.json"
            build_count = 0
            count_lock = threading.Lock()

            def fake_ensure(target: Path, client: object, source_url: str, allow_stale: bool = False):
                nonlocal build_count
                if target.exists():
                    return json.loads(target.read_text(encoding="utf-8")), "hit"
                with count_lock:
                    build_count += 1
                time.sleep(0.02)
                value = {"source": {"revision": 1}}
                target.write_text(json.dumps(value), encoding="utf-8")
                return value, "rebuilt"

            with patch.object(source_runtime, "ensure_fresh_index", side_effect=fake_ensure):
                with ThreadPoolExecutor(max_workers=2) as pool:
                    results = list(pool.map(
                        lambda _: source_runtime.ensure_shared_index(object(), index_path=path),
                        range(2),
                    ))
            self.assertEqual(build_count, 1)
            self.assertEqual({result[1] for result in results}, {"rebuilt", "hit"})


if __name__ == "__main__":
    unittest.main()
