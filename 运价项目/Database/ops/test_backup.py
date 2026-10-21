from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from ops.backup import CHECK_TABLES, backup, check


class BackupTests(unittest.TestCase):
    def test_backup_restores_and_copies_to_existing_offhost_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            db_path, backup_dir, offhost_dir = root / "ledger.db", root / "backup", root / "offhost"
            offhost_dir.mkdir()
            conn = sqlite3.connect(db_path)
            try:
                for table in CHECK_TABLES:
                    conn.execute(f"CREATE TABLE {table} (id INTEGER PRIMARY KEY)")
                    conn.execute(f"INSERT INTO {table} VALUES (1)")
                conn.commit()
            finally:
                conn.close()
            result = backup(db_path, keep=3, backup_dir=backup_dir, target_dir=offhost_dir,
                            status_path=root / "backup-status.json")
            self.assertTrue(result["verification"]["ok"])
            self.assertTrue(Path(result["offhost"]["path"]).is_file())
            self.assertTrue(check(db_path, backup_dir)["ok"])


if __name__ == "__main__":
    unittest.main()
