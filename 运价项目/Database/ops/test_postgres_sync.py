import tempfile
import unittest
from pathlib import Path

from ops.postgres_migrate import POSTGRES_DSN_ENV
from ops.postgres_sync import load_dsn


class PostgresSyncTests(unittest.TestCase):
    def test_load_dsn_reads_only_expected_key(self):
        with tempfile.TemporaryDirectory() as directory:
            env_file = Path(directory) / "freight-postgres.env"
            env_file.write_text("OTHER=value\n%s=postgresql://user:password@127.0.0.1/freight\n" % POSTGRES_DSN_ENV,
                                encoding="ascii")
            self.assertEqual(load_dsn(env_file), "postgresql://user:password@127.0.0.1/freight")

    def test_load_dsn_rejects_missing_or_duplicate_key(self):
        with tempfile.TemporaryDirectory() as directory:
            env_file = Path(directory) / "freight-postgres.env"
            env_file.write_text("OTHER=value\n", encoding="ascii")
            with self.assertRaises(RuntimeError):
                load_dsn(env_file)
            env_file.write_text("%s=a\n%s=b\n" % (POSTGRES_DSN_ENV, POSTGRES_DSN_ENV), encoding="ascii")
            with self.assertRaises(RuntimeError):
                load_dsn(env_file)


if __name__ == "__main__":
    unittest.main()
