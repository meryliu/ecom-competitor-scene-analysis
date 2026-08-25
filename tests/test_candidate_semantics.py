#!/usr/bin/env python3
from __future__ import annotations

import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from candidate_semantics import (  # noqa: E402
    constrained_core_evidence,
    full_scope_evidence,
)
from resolution_policy import load_resolution_policy  # noqa: E402


class CandidateSemanticsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.policy = load_resolution_policy()

    def evidence(
        self,
        phrase: str,
        metric: str,
        candidate: str,
        aliases: list[str],
        constraints: list[dict],
    ) -> dict:
        metadata = {"aliases": aliases}
        core = constrained_core_evidence(
            metric, candidate, metadata, constraints, self.policy
        )
        return full_scope_evidence(
            phrase, candidate, metadata, constraints, core, 0.78, self.policy
        )

    def test_eq_scope_uses_live_value_without_business_specific_rule(self) -> None:
        evidence = self.evidence(
            "京东闭环电商佣金收入",
            "闭环电商佣金收入",
            "京东闭环电商佣金收入",
            [],
            [{"operator": "eq", "values": ["京东"]}],
        )
        self.assertTrue(evidence["full_scope"])
        self.assertEqual(evidence["score"], 1.0)

    def test_in_scope_requires_all_members_in_one_label(self) -> None:
        constraints = [{"operator": "in", "values": ["京东", "拼多多"]}]
        exact = self.evidence(
            "京东和拼多多支付GMV合计",
            "支付GMV",
            "京东拼多多支付GMV",
            [],
            constraints,
        )
        fragmented = self.evidence(
            "京东和拼多多支付GMV合计",
            "支付GMV",
            "支付GMV",
            ["京东支付GMV", "拼多多支付GMV"],
            constraints,
        )
        self.assertTrue(exact["full_scope"])
        self.assertFalse(fragmented["full_scope"])

    def test_multiple_dimensions_are_and_combined(self) -> None:
        constraints = [
            {"operator": "eq", "values": ["京东"]},
            {"operator": "eq", "values": ["自营"]},
            {"operator": "eq", "values": ["家电"]},
        ]
        exact = self.evidence(
            "京东自营家电支付GMV",
            "支付GMV",
            "京东自营家电支付GMV",
            [],
            constraints,
        )
        partial = self.evidence(
            "京东自营家电支付GMV",
            "支付GMV",
            "京东自营支付GMV",
            [],
            constraints,
        )
        self.assertTrue(exact["full_scope"])
        self.assertFalse(partial["full_scope"])

    def test_exclude_requires_negative_operator_on_both_sides(self) -> None:
        constraint = [{"operator": "exclude", "values": ["抖音"]}]
        exact = self.evidence(
            "剔除抖音的大盘快递量",
            "大盘快递量",
            "剔除抖音大盘快递量",
            ["剔抖音大盘快递"],
            constraint,
        )
        positive = self.evidence(
            "剔除抖音的大盘快递量",
            "大盘快递量",
            "抖音大盘快递量",
            [],
            constraint,
        )
        self.assertTrue(exact["full_scope"])
        self.assertFalse(positive["full_scope"])


if __name__ == "__main__":
    unittest.main()
