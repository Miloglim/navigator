import unittest
from unittest.mock import patch

import pipeline


class CompletedCycleTests(unittest.TestCase):
    def test_done_with_uncommitted_watermark_refreshes_existing_cycle(self):
        state = {
            "date": "fixture",
            "stage": "DONE",
            "new_ids": ["previous-message"],
            "pending_watermark": {"group": "2026-10-13 10:00"},
            "submit_done_records": ["submitted-signature"],
        }
        with patch.object(pipeline, "load_state", return_value=state), \
                patch.object(pipeline, "refresh_incomplete_pull") as refresh, \
                patch.object(pipeline, "step_pull") as start_new:
            pipeline.next_step("fixture")

        refresh.assert_called_once_with("fixture", state)
        start_new.assert_not_called()

    def test_done_with_committed_watermarks_starts_new_cycle(self):
        state = {
            "date": "fixture",
            "stage": "DONE",
            "new_ids": ["previous-message"],
            "pending_watermark": {},
        }
        with patch.object(pipeline, "load_state", return_value=state), \
                patch.object(pipeline, "save_state") as save, \
                patch.object(pipeline, "step_pull") as start_new:
            pipeline.next_step("fixture")

        next_state = save.call_args.args[1]
        self.assertEqual(next_state, {
            "date": "fixture", "stage": "PULL", "retries": {}, "submit_done": []
        })
        start_new.assert_called_once_with("fixture", next_state)


if __name__ == "__main__":
    unittest.main()
