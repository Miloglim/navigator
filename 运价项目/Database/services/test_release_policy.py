from __future__ import annotations

import unittest

from core.settings import RELEASE_MODE_GUARDED_AUTO, RELEASE_MODE_REVIEW
from services.release_policy import decide_release


class ReleasePolicyTests(unittest.TestCase):
    def test_review_mode_never_auto_publishes(self) -> None:
        result = decide_release({
            "mechanical_check_passed": True, "source_known": True, "sender_known": True,
            "validity_hours": 48, "price_change_percent": 2, "group_clean_rate_percent": 100,
        }, RELEASE_MODE_REVIEW)
        self.assertEqual("review_required", result["decision"])

    def test_guarded_auto_requires_all_facts(self) -> None:
        result = decide_release({
            "mechanical_check_passed": True, "source_known": True, "sender_known": True,
            "validity_hours": 48, "price_change_percent": 2, "group_clean_rate_percent": 100,
        }, RELEASE_MODE_GUARDED_AUTO)
        self.assertEqual("auto_publish", result["decision"])

    def test_ai_inference_stays_in_review(self) -> None:
        result = decide_release({
            "mechanical_check_passed": True, "source_known": True, "sender_known": True,
            "ai_inferred_fields": True, "validity_hours": 48,
            "price_change_percent": 2, "group_clean_rate_percent": 100,
        }, RELEASE_MODE_GUARDED_AUTO)
        self.assertEqual("review_required", result["decision"])
        self.assertIn("ai_inferred_fields", result["reasons"])

    def test_invalid_numeric_fact_stays_in_review(self) -> None:
        result = decide_release({
            "mechanical_check_passed": True, "source_known": True, "sender_known": True,
            "validity_hours": "tomorrow", "price_change_percent": 2,
            "group_clean_rate_percent": 100,
        }, RELEASE_MODE_GUARDED_AUTO)
        self.assertEqual("review_required", result["decision"])
        self.assertIn("validity_hours_invalid", result["reasons"])


if __name__ == "__main__":
    unittest.main()
