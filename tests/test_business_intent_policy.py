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

    def test_growth_hypothesis_is_declarative_and_bounded(self) -> None:
        hypotheses = generate_metric_hypotheses(
            {"query": "线上社零表现怎么样，涨幅较上月如何"},
            {
                "name": "线上社零",
                "metric_object": "volume",
                "metric_object_provenance": "model_inferred",
                "consumers": [{"requirement_type": "fact_observations"}],
            },
            self.policy,
        )
        self.assertLessEqual(len(hypotheses), 3)
        growth = next(item for item in hypotheses if item["intent_id"] == "growth_ratio_metric")
        self.assertEqual(growth["metric_object"], "ratio")
        self.assertIn("线上社零同比增速", growth["requested_terms"])

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
