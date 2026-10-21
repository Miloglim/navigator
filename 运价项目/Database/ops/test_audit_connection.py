"""Regression cases for audit identity and database-enforced read-only access."""
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import audit_connection as audit


class AuditTests(unittest.TestCase):
    def test_formal_services_and_terminals_remain_independent(self):
        import manualdb
        with tempfile.TemporaryDirectory() as directory:
            c = manualdb.connect(Path(directory) / "ledger.db")
            try:
                base = {"起运港": "Dalian", "目的港": "Santos", "船司": "MSC", "柜型": "20GP",
                        "海运费USD": "5000", "来源群": "test", "消息ID": "m1", "有效期止": "2099-01-01"}
                records = [dict(base, **{"航线服务": service, "目的码头": terminal})
                           for service, terminal in (("Santana", ""), ("Ipanema", ""),
                                                     ("Carioca", "A"), ("Carioca", "B"))]
                manualdb.upsert_rows(c, "rates", [manualdb.normalize(r, "rates", manualdb.now_cn(), "agent") for r in records])
                result = manualdb.audit_all(c, manualdb.Master(c))
                self.assertFalse([i for i in result['integrity'] if i['type'] == '同身份多条当前生效'])
            finally:
                c.close()

    def test_variants_are_not_duplicates(self):
        row = {"kind": "rates"}
        base = {"来源群": "test", "消息ID": "m1", "船司": "MSC", "航线服务": "Ipanema"}
        key = audit.duplicate_key(row, base)
        for field, value in (("航线服务", "Santana"), ("目的码头", "TERPORT"),
                             ("海运费USD", "100"), ("来源群", "other"), ("消息ID", "m2")):
            self.assertNotEqual(key, audit.duplicate_key(row, dict(base, **{field: value})))
        self.assertEqual(key, audit.duplicate_key(row, dict(reversed(list(base.items())))))
        self.assertIsNone(audit.duplicate_key(row, {"来源群": "test"}))

    def test_sqlite_readonly_and_missing_database(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "test.db"
            db = sqlite3.connect(path)
            db.execute("CREATE TABLE evidence(value TEXT)")
            db.close()
            with patch.dict(audit.os.environ, {"FRT_DB": str(path)}):
                db = audit.connect("sqlite")
                try:
                    with self.assertRaises(sqlite3.OperationalError):
                        db.execute("INSERT INTO evidence VALUES ('not allowed')")
                finally:
                    db.close()
            with patch.dict(audit.os.environ, {"FRT_DB": str(path.parent / 'missing.db')}):
                with self.assertRaises(sqlite3.OperationalError):
                    audit.connect("sqlite")


if __name__ == '__main__':
    unittest.main()
