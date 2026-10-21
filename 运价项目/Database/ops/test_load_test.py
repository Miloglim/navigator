from __future__ import annotations

import unittest

from ops.load_test import percentile, summarize


class LoadTestSummaryTests(unittest.TestCase):
    def test_percentile_empty_and_ordered_values(self) -> None:
        self.assertEqual(0.0, percentile([], 95))
        self.assertEqual(40, percentile([10, 20, 30, 40], 100))

    def test_summary_groups_successes_and_failures(self) -> None:
        result = summarize([
            ("/api/stats", 200, 10.0, ""),
            ("/api/stats", 503, 20.0, "HTTP 503"),
            ("/api/rates", 200, 30.0, ""),
        ])
        self.assertEqual(1, result["/api/stats"]["success"])
        self.assertEqual(1, result["/api/stats"]["failed"])
        self.assertEqual(["HTTP 503"], result["/api/stats"]["errors"])
