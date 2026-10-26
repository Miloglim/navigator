"""Rate currency defaults, validation, persistence and grouped output."""
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

import manualdb
from test_port_routes import PORT_B, seed, quote


class RateCurrencyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="rate-currency-")
        self.conn = manualdb.connect(Path(self.temp.name) / "currency.db")
        seed(self.conn)
        manualdb.invalidate_master()
        self.master = manualdb.get_master(self.conn)

    def tearDown(self):
        self.conn.close()
        self.temp.cleanup()
        manualdb.invalidate_master()

    def test_missing_currency_defaults_to_usd_and_eur_is_preserved(self):
        usd = dict(quote(""), **{"消息ID": "currency-usd", "消息原文": "USD default rate"})
        eur = dict(quote(""), **{"目的港": PORT_B, "消息ID": "currency-eur", "海运费USD": "150",
                                 "币种": "eur", "消息原文": "EUR explicit rate"})
        ids = manualdb.stage_pending(self.conn, self.master, "rates", [usd, eur], auto_suggest=False)
        payloads = [json.loads(self.conn.execute("SELECT payload FROM pending_records WHERE id=?", (pid,)).fetchone()[0])
                    for pid in ids]
        self.assertEqual([row["币种"] for row in payloads], ["USD", "EUR"])
        self.assertTrue(all(self.conn.execute("SELECT check_status FROM pending_records WHERE id=?", (pid,)).fetchone()[0] == "passed"
                            for pid in ids))

        for pid in ids:
            self.assertTrue(manualdb.approve_pending(self.conn, self.master, pid, reviewed_by="currency-test")["ok"])
        rows = manualdb.query(self.conn, self.master, "rates", {"limit": "20"})["rows"]
        self.assertEqual({row["currency"] for row in rows}, {"USD", "EUR"})
        groups = manualdb.query_grouped(self.conn, self.master, "rates", {"limit": "20"})["groups"]
        self.assertEqual({group["current"]["currency"] for group in groups}, {"USD", "EUR"})

    def test_invalid_currency_code_is_rejected(self):
        invalid = dict(quote(""), **{"消息ID": "currency-invalid", "币种": "EURO"})
        pid = manualdb.stage_pending(self.conn, self.master, "rates", [invalid], auto_suggest=False)[0]
        row = self.conn.execute("SELECT check_status,errors FROM pending_records WHERE id=?", (pid,)).fetchone()
        self.assertEqual(row["check_status"], "failed")
        self.assertIn("币种", row["errors"])

    def test_existing_sqlite_schema_gets_usd_default(self):
        path = Path(self.temp.name) / "legacy-currency.db"
        legacy = sqlite3.connect(path)
        legacy.row_factory = sqlite3.Row
        old_ddl = manualdb.DDL["freight_items"].replace(" currency TEXT NOT NULL DEFAULT 'USD',", "")
        legacy.execute(old_ddl)
        legacy.commit()
        manualdb.init_schema(legacy)
        columns = {row["name"] for row in legacy.execute("PRAGMA table_info(freight_items)")}
        self.assertIn("currency", columns)
        legacy.execute("INSERT INTO freight_items(entry_id,content_key,record_type,pol,pod,carrier,container_type,price_usd) VALUES('frt_legacy','legacy-key','rates','Ningbo','Santos','MSC','20GP',100)")
        self.assertEqual(legacy.execute("SELECT currency FROM freight_items WHERE entry_id='frt_legacy'").fetchone()[0], "USD")
        legacy.close()

    def test_usd_remains_legacy_fingerprint_and_other_currency_changes_it(self):
        base = dict(quote(""), **{"消息ID": "fingerprint-currency"})
        now = "2026-10-23 10:00:00"
        legacy = manualdb.normalize(base, "rates", now, "manual")
        usd = manualdb.normalize({**base, "币种": "USD"}, "rates", now, "manual")
        eur = manualdb.normalize({**base, "币种": "EUR"}, "rates", now, "manual")
        self.assertEqual(legacy["content_key"], usd["content_key"])
        self.assertNotEqual(usd["content_key"], eur["content_key"])


if __name__ == "__main__":
    unittest.main()
