"""Focused membership regression tests; business writes use isolated databases only."""
from __future__ import annotations

import json
import unittest

import manualdb
from services.port_routes import route_tokens, unique_common_route

LANE_A = "Test Lane A"
LANE_B = "Test Lane B"
PORT_A = "Test Port Route A"
PORT_B = "Test Port Route B"
POL = "Test Port Route Origin"
CARRIER = "TESTPR"


def seed(conn):
    for route in (LANE_A, LANE_B):
        manualdb.master_save(conn, "routes", {"name": route, "active": 1})
    for port, kind in ((PORT_A, "pod"), (PORT_B, "pod"), (POL, "both")):
        manualdb.master_save(conn, "ports", {"name": port, "kind": kind, "active": 1})
    manualdb.master_save(conn, "carriers", {"code": CARRIER, "active": 1})


def quote(route=""):
    return {"起运港": POL, "目的港": PORT_A, "船司": CARRIER, "航线": route,
            "柜型": "20GP", "海运费USD": "100", "有效期起": "2026-09-28",
            "有效期止": "2026-10-28", "消息原文": "isolated membership regression"}


def verify_port_route_flow(case, conn):
    seed(conn)
    result = manualdb.master_save(conn, "ports", {"name": PORT_A, "routes_list": [LANE_A, LANE_B, LANE_A]})
    case.assertEqual(set(result["data"]["routes_list"]), {LANE_A, LANE_B})
    master = manualdb.get_master(conn)
    case.assertEqual(master.derive_route(PORT_A), "")
    payload = manualdb.master_payload(conn, full=True)
    compact = next(p for p in payload["ports"] if p["n"] == PORT_A)
    case.assertEqual(set(compact["rs"]), {LANE_A, LANE_B})
    case.assertEqual(set(next(p for p in payload["raw"]["ports"] if p["name"] == PORT_A)["routes_list"]), {LANE_A, LANE_B})
    checked = manualdb.validate_record(conn, master, "rates", quote())
    case.assertTrue(checked["ok"], checked)
    case.assertEqual(checked["resolved"]["航线"], "")
    case.assertEqual(checked["warnings"][0]["field"], "航线归属")
    ids = manualdb.stage_pending(conn, master, "rates", [quote()], auto_suggest=False)
    staged = conn.execute("SELECT * FROM pending_records WHERE id=?", (ids[0],)).fetchone()
    case.assertEqual(staged["check_status"], "passed")
    case.assertEqual(json.loads(staged["resolved"])["航线"], "")
    case.assertEqual(json.loads(staged["warnings"])[0]["field"], "航线归属")
    coverage = {"field": "港口覆盖率", "message": "isolated coverage hint"}
    conn.execute("UPDATE pending_records SET warnings=? WHERE id=?",
                 (json.dumps([*json.loads(staged["warnings"]), coverage]), ids[0]))
    conn.commit()
    edited = manualdb.update_pending(conn, master, ids[0], {"航线": LANE_B})
    case.assertEqual(edited["warnings"], [coverage])
    case.assertEqual(json.loads(conn.execute("SELECT warnings FROM pending_records WHERE id=?", (ids[0],)).fetchone()[0]), [coverage])
    # An explicit quote classification is not expanded to all port memberships.
    ids = manualdb.stage_pending(conn, master, "rates", [quote(LANE_B)], auto_suggest=False)
    case.assertTrue(manualdb.approve_pending(conn, master, ids[0], reviewed_by="isolated-test")["ok"])
    case.assertEqual(manualdb.query(conn, master, "rates", {"route": LANE_B})["total"], 1)
    case.assertEqual(manualdb.query(conn, master, "rates", {"route": LANE_A})["total"], 0)
    with case.assertRaises(ValueError):
        manualdb.master_save(conn, "ports", {"name": PORT_A, "routes_list": [LANE_A, "unknown-lane"]})
    case.assertEqual(conn.execute("SELECT COUNT(*) FROM port_routes WHERE port_name=?", (PORT_A,)).fetchone()[0], 2)
    with case.assertRaises(ValueError):
        manualdb.master_delete(conn, "routes", LANE_A)
    with case.assertRaises(ValueError):
        manualdb.master_save(conn, "routes", {"name": LANE_A, "active": 0})
    with case.assertRaises(ValueError):
        manualdb.master_save(conn, "routes", {"name": LANE_A, "active": 0.0})
    snap = manualdb.export_all(conn)
    manualdb.master_save(conn, "ports", {"name": PORT_A, "routes_list": []})
    case.assertEqual(manualdb.get_master(conn).route_candidates(PORT_A), [])
    manualdb.load_snapshot(conn, snap)
    case.assertEqual(set(manualdb.get_master(conn).route_candidates(PORT_A)), {LANE_A, LANE_B})
    legacy = dict(snap)
    legacy.pop("port_routes")
    manualdb.load_snapshot(conn, legacy)
    case.assertEqual(set(manualdb.get_master(conn).route_candidates(PORT_A)), {LANE_A, LANE_B})
    broken = dict(snap, port_routes=[{"port_name": PORT_A, "route_name": "missing-route"}])
    with case.assertRaises(Exception):
        manualdb.load_snapshot(conn, broken)
    case.assertEqual(set(manualdb.get_master(conn).route_candidates(PORT_A)), {LANE_A, LANE_B})
    manualdb.master_save(conn, "ports", {"name": PORT_A, "route": LANE_A + "," + LANE_A})
    case.assertEqual(manualdb.get_master(conn).derive_route(PORT_A), LANE_A)
    manualdb.master_save(conn, "ports", {"name": PORT_A, "routes_list": []})
    case.assertEqual(conn.execute("SELECT route FROM ports WHERE name=?", (PORT_A,)).fetchone()[0], "")
    case.assertEqual(conn.execute("SELECT route FROM freight_items WHERE pod=?", (PORT_A,)).fetchone()[0], LANE_B)


class PortRoutesTests(unittest.TestCase):
    def setUp(self):
        self.conn = manualdb.connect(":memory:")
        manualdb.invalidate_master()

    def tearDown(self):
        self.conn.close()
        manualdb.invalidate_master()

    def test_cross_database_flow(self):
        verify_port_route_flow(self, self.conn)

    def test_tokens_and_common_membership(self):
        self.assertEqual(route_tokens(" A， B;A "), ["A", "B"])
        for invalid in ({}, True, 1, ["A", 1]):
            with self.assertRaises(ValueError):
                route_tokens(invalid)
        self.assertEqual(unique_common_route([["A", "B"], ["B"]]), "B")
        self.assertEqual(unique_common_route([["A", "B"]]), "")
        self.assertEqual(unique_common_route([["A"], []]), "")
        self.assertEqual(unique_common_route([]), "")

    def test_group_missing_membership_does_not_guess(self):
        seed(self.conn)
        manualdb.master_save(self.conn, "ports", {"name": PORT_A, "routes_list": [LANE_A, LANE_B]})
        manualdb.master_save(self.conn, "ports", {"name": PORT_B, "routes_list": [LANE_B]})
        manualdb.master_save(self.conn, "port_groups", {"name": "Test route group", "members": PORT_A + "," + PORT_B})
        self.assertEqual(manualdb.get_master(self.conn).derive_route("Test route group"), LANE_B)
        manualdb.master_save(self.conn, "ports", {"name": PORT_B, "routes_list": []})
        self.assertEqual(manualdb.get_master(self.conn).derive_route("Test route group"), "")

    def test_reject_invalid_and_conflicting_contracts(self):
        seed(self.conn)
        for fields in ({"routes_list": "Test Lane A"}, {"routes_list": [LANE_A, 1]},
                       {"routes_list": [LANE_A], "route": LANE_B}):
            with self.assertRaises(ValueError):
                manualdb.master_save(self.conn, "ports", dict(name=PORT_A, **fields))
        self.assertEqual(manualdb.get_master(self.conn).route_candidates(PORT_A), [])

    def test_legacy_backfill_is_idempotent(self):
        seed(self.conn)
        self.conn.execute("DROP TABLE port_routes")
        self.conn.execute("UPDATE ports SET route=? WHERE name=?", (LANE_B + "," + LANE_A, PORT_A))
        self.conn.commit()
        self.assertTrue(manualdb.ensure_port_route_schema(self.conn)["data"]["migrated"])
        self.assertEqual(set(manualdb.get_master(self.conn).route_candidates(PORT_A)), {LANE_A, LANE_B})
        manualdb.master_save(self.conn, "ports", {"name": PORT_A, "routes_list": []})
        self.assertFalse(manualdb.ensure_port_route_schema(self.conn)["data"]["migrated"])
        self.assertEqual(manualdb.get_master(self.conn).route_candidates(PORT_A), [])

    def test_unknown_legacy_rolls_back_schema(self):
        seed(self.conn)
        self.conn.execute("DROP TABLE port_routes")
        self.conn.execute("UPDATE ports SET route='unknown' WHERE name=?", (PORT_A,))
        self.conn.commit()
        with self.assertRaises(ValueError):
            manualdb.ensure_port_route_schema(self.conn)
        self.assertEqual(self.conn.execute("PRAGMA table_info(port_routes)").fetchall(), [])
        self.assertEqual(self.conn.execute("SELECT route FROM ports WHERE name=?", (PORT_A,)).fetchone()[0], "unknown")


if __name__ == "__main__":
    unittest.main()
