from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from fulfillment_candidates import (  # noqa: E402
    fulfillment_tier_for_path,
    select_fulfillment_candidate,
)


class FulfillmentCandidateTests(unittest.TestCase):
    def test_complete_direct_fact_precedes_registered_composition(self) -> None:
        selected, ranked = select_fulfillment_candidate([
            {"candidate_id": "composition:rate", "candidate_type": "registered_composition", "status": "viable"},
            {"candidate_id": "direct:rate", "candidate_type": "direct_fact", "status": "viable"},
        ])
        self.assertEqual(selected["candidate_type"], "direct_fact")
        self.assertEqual([item["candidate_type"] for item in ranked], [
            "direct_fact", "registered_composition"
        ])

    def test_registered_composition_is_selected_when_direct_is_infeasible(self) -> None:
        selected, _ = select_fulfillment_candidate([
            {"candidate_id": "direct:rate", "candidate_type": "direct_fact", "status": "infeasible"},
            {"candidate_id": "composition:rate", "candidate_type": "registered_composition", "status": "viable"},
        ])
        self.assertEqual(selected["candidate_type"], "registered_composition")

    def test_same_metric_set_paths_share_unified_candidate_tier(self) -> None:
        self.assertEqual(fulfillment_tier_for_path("additive_member_sum"), 2)
        self.assertEqual(fulfillment_tier_for_path("same_metric_total_minus_members"), 2)
