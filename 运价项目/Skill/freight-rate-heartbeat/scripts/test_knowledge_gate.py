import json
import tempfile
import unittest
from pathlib import Path

import knowledge_gate


class KnowledgeGateTests(unittest.TestCase):
    def test_receipt_covers_global_and_group_files_and_reuses_hash(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            first = knowledge_gate.ensure(str(run_dir), ["europe_price"], ["m1"], "PARSE")
            self.assertTrue(first["ok"])
            self.assertIn("europe_price", first["groups"])
            self.assertGreaterEqual(len(first["files"]), 6)
            second = knowledge_gate.ensure(str(run_dir), ["europe_price"], ["m1"], "BUILD")
            self.assertEqual(second["phase"], "BUILD")
            self.assertTrue(all(item["reused"] for item in second["files"].values()))
            saved = json.loads((run_dir / knowledge_gate.MANIFEST_NAME).read_text(encoding="utf-8"))
            self.assertEqual(saved["message_ids"], ["m1"])

    def test_unknown_group_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(knowledge_gate.KnowledgeGateError):
                knowledge_gate.ensure(tmp, ["not_a_group"], ["m1"], "PARSE")


if __name__ == "__main__":
    unittest.main()
