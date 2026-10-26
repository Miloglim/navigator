import unittest
from pathlib import Path

import spec_index


class SpecKnowledgeTests(unittest.TestCase):
    def test_numbered_heading_rules_are_valid_audit_anchors(self):
        for rule in (33, 34, 35, 36, 37, 38, 39, 40):
            with self.subTest(rule=rule):
                self.assertTrue(spec_index.resolve_basis('huanan_latam',
                    'extraction-rules.md#' + str(rule))[0])

    def test_existing_knowledge_is_exposed_and_scoped(self):
        index = spec_index.index(refresh=True)
        for slug in ("europe_price", "turkey_price"):
            with self.subTest(slug=slug):
                path = spec_index.brief(slug)["spec_knowledge_path"]
                self.assertTrue(Path(path).is_file())
                self.assertEqual(Path(path).parent, Path(index[slug]["path"]).parent)
                for heading in index[slug]["knowledge_headers"]:
                    basis = spec_index.KNOWLEDGE_FILE + "#" + heading
                    self.assertTrue(spec_index.resolve_basis(slug, basis)[0])
        european_basis = spec_index.KNOWLEDGE_FILE + "#" + index["europe_price"]["knowledge_headers"][0]
        self.assertFalse(spec_index.resolve_basis("turkey_price", european_basis)[0])
        self.assertFalse(spec_index.resolve_basis("europe_price", spec_index.KNOWLEDGE_FILE + "#不存在")[0])

    def test_absent_knowledge_and_legacy_basis(self):
        self.assertEqual(spec_index.brief("not_a_group")["spec_knowledge_path"], "")
        self.assertEqual(spec_index.brief("tianjin_mesa")["spec_knowledge_path"], "")
        self.assertTrue(spec_index.resolve_basis("europe_price", "规范.md#解析要求")[0])


if __name__ == "__main__":
    unittest.main()
