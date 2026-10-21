from __future__ import annotations

import unittest

from ops.postgres_migrate import CONFIGURATION_TABLES, SESSION_TABLE, digest_rows, postgres_schema, selected_tables


class PostgresMigrationTests(unittest.TestCase):
    def test_schema_has_no_sqlite_only_autoincrement_or_glob(self) -> None:
        schema = "\n".join(postgres_schema())
        self.assertNotIn("AUTOINCREMENT", schema)
        self.assertNotIn(" GLOB ", schema)
        self.assertIn("BIGSERIAL PRIMARY KEY", schema)
        self.assertIn("orig_payload", schema)

    def test_default_migration_preserves_only_configuration(self) -> None:
        self.assertEqual(CONFIGURATION_TABLES, tuple(selected_tables(False)))
        self.assertNotIn(SESSION_TABLE, selected_tables(False))

    def test_digest_is_order_independent(self) -> None:
        rows = [{"name": "A", "active": 1}, {"name": "B", "active": 0}]
        self.assertEqual(digest_rows(rows), digest_rows(list(reversed(rows))))


if __name__ == "__main__":
    unittest.main()
