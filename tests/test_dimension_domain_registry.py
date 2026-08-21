from __future__ import annotations

import sys
import json
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from dimension_domain_registry import (  # noqa: E402
    load_dimension_set_registry,
    resolve_dimension_domain,
    source_dimension_domain_ref,
)


class DimensionDomainRegistryTests(unittest.TestCase):
    def test_empty_registry_is_valid_and_contains_no_top6_alias(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "registry.json"
            path.write_text(json.dumps({
                "schema_version": "dimension_set_registry/1.0",
                "sets": {},
            }), encoding="utf-8")
            registry = load_dimension_set_registry(path)
        self.assertEqual(registry["sets"], {})

    def test_source_dimension_domain_identity_does_not_encode_members(self) -> None:
        first = source_dimension_domain_ref("TOP6平台")
        second = source_dimension_domain_ref(" TOP6平台 ")
        self.assertEqual(first, second)
        self.assertTrue(first.startswith("domain_"))

    def test_new_fixed_set_requires_configuration_only(self) -> None:
        registry = {
            "schema_version": "dimension_set_registry/1.0",
            "sets": {
                "content_ecommerce": {
                    "dimension": "平台",
                    "aliases": ["内容电商平台"],
                    "members": ["抖音", "快手", "视频号"],
                }
            },
        }
        resolved = resolve_dimension_domain(
            "平台",
            ["内容电商平台"],
            ["淘系", "抖音", "拼多多", "京东", "快手", "视频号"],
            registry,
        )
        self.assertEqual(resolved["members"], ["抖音", "快手", "视频号"])
        self.assertEqual(resolved["set_ids"], ["content_ecommerce"])
        self.assertTrue(resolved["domain_id"].startswith("domain_"))


if __name__ == "__main__":
    unittest.main()
