import json
import sqlite3
import unittest
import manualdb as db
from services.agent_repair import payload_signature
from test_agent_repair import EVIDENCE
from test_port_routes import seed, quote


def verify_sailing_identity(test, conn):
    seed(conn)
    conn.execute("INSERT INTO ports(name,active) VALUES('Sailing Fixture Istanbul',1)")
    conn.execute("INSERT INTO alias_scope(alias,kind,target,scope_group) VALUES('Sailing Fixture Ambarli','pod','Sailing Fixture ISTanbul','fixture')")
    conn.commit()
    master = db.Master(conn)
    test.assertEqual(master.lookup_port('Sailing Fixture Ambarli','pod','fixture'), ('Sailing Fixture Istanbul',True))
    rec = dict(quote(), **{"消息ID": "two-images", "来源群": "fixture", "船期ETD": "2026-10-12"})
    for kind in ("rates", "space"):
        base = dict(rec)
        if kind == "space":
            base.update({"价格USD": 100, "船名航次": "VESSEL V001", "舱位类型": "现舱"})
        batch = [dict(base, 柜型=ct, 船期ETD=etd) for ct in ("20GP", "40HQ/HC") for etd in ("2026-10-12", "2026-10-26")]
        ids = db.stage_pending(conn, master, kind, batch, auto_suggest=False, preserve_other_sources=True)
        test.assertEqual(len(ids), 4, "different equipment and explicit ETDs must coexist")
        for pid in ids:
            test.assertTrue(db.approve_pending(conn, master, pid)["ok"])
        test.assertEqual(conn.execute("SELECT count(*) FROM freight_items WHERE record_type=? AND status=?", (kind, db.DEFAULT_STATUS[kind])).fetchone()[0], 4)
    # Correct classification without replacing the row or its source snapshot.
    pid = db.stage_pending(conn, master, "rates", [dict(rec, 消息ID="repair-source")], auto_suggest=False)[0]
    before = dict(conn.execute("SELECT * FROM pending_records WHERE id=?", (pid,)).fetchone())
    req = {"id": pid, "kind": "space", "expected_signature": payload_signature(json.loads(before["payload"])),
           "fields": {"船名航次": "VESSEL V002", "舱位类型": "现舱", "价格USD": 100, "海运费USD": ""}, "evidence": EVIDENCE}
    test.assertTrue(db.agent_pending_repair(conn, req)["ok"])
    after = dict(conn.execute("SELECT * FROM pending_records WHERE id=?", (pid,)).fetchone())
    for key in ("entry_id", "orig_payload", "review_status"):
        test.assertEqual(before[key], after[key])
    test.assertEqual(after["kind"], "space")
    test.assertTrue(any(e["field"] == "kind" and e["old"] == "rates" for e in json.loads(after["edit_log"])))
    test.assertEqual(conn.execute("SELECT count(*) FROM freight_items WHERE mid='repair-source'").fetchone()[0], 0)

class SailingIdentityTests(unittest.TestCase):
    def test_sqlite_sailings_and_audited_classification(self):
        conn = sqlite3.connect(":memory:"); conn.row_factory = sqlite3.Row
        try:
            db.init_schema(conn); verify_sailing_identity(self, conn)
        finally:
            conn.close(); db.invalidate_master()

if __name__ == "__main__": unittest.main()
