from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from set_materialization import (  # noqa: E402
    materialize_set_spec,
    set_aggregate_expression,
)


class SetMaterializationTests(unittest.TestCase):
    def test_complement_is_generic_and_revision_scoped(self) -> None:
        index = {
            "source": {"revision": 1301},
            "dimensions": {"渠道": {"values": ["A", "B", "C"]}},
        }
        spec = materialize_set_spec([{
            "operator": "exclude", "values": ["B"], "source_dimension": "渠道"
        }], index)
        self.assertEqual(spec["membership_kind"], "complement")
        self.assertEqual(spec["members"], ["A", "C"])
        self.assertEqual(spec["source_revision"], 1301)
        changed = materialize_set_spec([{
            "operator": "exclude", "values": ["B"], "source_dimension": "渠道"
        }], {**index, "source": {"revision": 1302}})
        self.assertNotEqual(spec["set_fingerprint"], changed["set_fingerprint"])

    def test_complement_compiles_to_existing_sum_and_subtract_ast(self) -> None:
        spec = {
            "dimension_ref": "渠道", "members": ["A", "C"],
            "domain_members": ["A", "B", "C"], "excluded_members": ["B"],
            "has_positive_filter": False,
        }
        expression = set_aggregate_expression(
            spec, "orders", "analysis", "same_metric_total_minus_members"
        )
        self.assertEqual(expression["op"], "subtract")
        self.assertEqual(expression["args"][0]["op"], "sum")

    def test_high_cardinality_domain_allows_small_explicit_subset(self) -> None:
        index = {
            "source": {"revision": 1},
            "dimensions": {"商品": {"values": [f"sku-{number}" for number in range(300)]}},
        }
        spec = materialize_set_spec([{
            "operator": "in", "values": ["sku-1", "sku-2"], "source_dimension": "商品"
        }], index)
        self.assertEqual(spec["members"], ["sku-1", "sku-2"])
