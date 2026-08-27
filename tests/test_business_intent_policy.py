from __future__ import annotations

import sys
import unittest
from copy import deepcopy
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from business_intent_policy import (  # noqa: E402
    BusinessIntentPolicyError,
    generate_metric_hypotheses,
    load_business_intent_policy,
    validate_business_intent_policy,
)


class BusinessIntentPolicyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.policy = load_business_intent_policy()

    def test_policy_rejects_name_mappings_and_unknown_fields(self) -> None:
        invalid = deepcopy(self.policy)
        invalid["mappings"] = {"线上社零": "实物商品网上零售额"}
        with self.assertRaises(BusinessIntentPolicyError):
            validate_business_intent_policy(invalid)

    def test_registered_derived_consumer_generates_bounded_growth_hypothesis(self) -> None:
        hypotheses = generate_metric_hypotheses(
            {"query": "线上社零表现怎么样，涨幅较上月如何"},
            {
                "name": "线上社零",
                "metric_object": "volume",
                "metric_object_provenance": "model_inferred",
                "consumers": [{
                    "requirement_type": "derived_requirements",
                    "derived_metric_id": "yoy_growth",
                }],
            },
            self.policy,
        )
        self.assertLessEqual(len(hypotheses), 3)
        growth = next(item for item in hypotheses if item["intent_id"] == "growth_ratio_metric")
        self.assertEqual(growth["metric_object"], "ratio")
        self.assertIn("线上社零同比增速", growth["requested_terms"])

    def test_query_wide_yoy_does_not_rewrite_unrelated_fact_consumer(self) -> None:
        hypotheses = generate_metric_hypotheses(
            {"query": "线上社零同比是多少"},
            {
                "name": "线上社零",
                "metric_object": "volume",
                "metric_object_provenance": "model_inferred",
                "consumers": [{"requirement_type": "fact_observations"}],
            },
            self.policy,
        )
        self.assertEqual([item["intent_id"] for item in hypotheses], ["declared_metric"])

    def test_requirement_fragment_can_trigger_legacy_fact_intent(self) -> None:
        hypotheses = generate_metric_hypotheses(
            {"query": "指标A水平，指标B同比是多少"},
            {
                "name": "指标B",
                "metric_object": "volume",
                "metric_object_provenance": "model_inferred",
                "consumers": [{
                    "requirement_type": "fact_observations",
                    "semantic_text": "指标B同比是多少",
                }],
            },
            self.policy,
        )
        self.assertIn("growth_ratio_metric", [item["intent_id"] for item in hypotheses])

    def test_vague_performance_keeps_a_lower_ratio_alternative(self) -> None:
        hypotheses = generate_metric_hypotheses(
            {"query": "社零大盘中哪些类目表现较好"},
            {
                "name": "社零大盘",
                "metric_object": "volume",
                "metric_object_provenance": "model_inferred",
                "consumers": [{
                    "requirement_type": "fact_observations",
                    "breakdown_dimensions": ["类目"],
                    "semantic_text": "社零大盘中各类目表现",
                }],
            },
            self.policy,
        )
        declared = next(item for item in hypotheses if item["intent_id"] == "declared_metric")
        alternative = next(
            item for item in hypotheses
            if item["intent_id"] == "performance_growth_alternative"
        )
        self.assertEqual(declared["semantic_role"], "primary")
        self.assertEqual(alternative["semantic_role"], "compatible_alternative")
        self.assertEqual(alternative["metric_object"], "ratio")
        self.assertIn("社零大盘同比增速", alternative["requested_terms"])

    def test_vague_performance_alternative_does_not_require_breakdown(self) -> None:
        hypotheses = generate_metric_hypotheses(
            {"query": "线上社零表现怎么样"},
            {
                "name": "线上社零",
                "metric_object": "volume",
                "metric_object_provenance": "model_inferred",
                "consumers": [{
                    "requirement_type": "fact_observations",
                    "semantic_text": "线上社零表现",
                }],
            },
            self.policy,
        )
        self.assertIn(
            "performance_growth_alternative",
            [item["intent_id"] for item in hypotheses],
        )

    def test_explicit_volume_does_not_expand_performance_to_growth(self) -> None:
        hypotheses = generate_metric_hypotheses(
            {"query": "线上社零规模表现"},
            {
                "name": "线上社零",
                "metric_object": "volume",
                "metric_object_provenance": "user_explicit",
                "consumers": [{
                    "requirement_type": "fact_observations",
                    "semantic_text": "线上社零规模表现",
                }],
            },
            self.policy,
        )
        self.assertEqual([item["intent_id"] for item in hypotheses], ["declared_metric"])

    def test_attribution_consumer_does_not_expand_alternative_intents(self) -> None:
        hypotheses = generate_metric_hypotheses(
            {"query": "支付GMV涨幅贡献"},
            {
                "name": "支付GMV",
                "metric_object": "volume",
                "metric_object_provenance": "model_inferred",
                "consumers": [{"requirement_type": "attribution_targets"}],
            },
            self.policy,
        )
        self.assertEqual([item["intent_id"] for item in hypotheses], ["declared_metric"])


if __name__ == "__main__":
    unittest.main()
