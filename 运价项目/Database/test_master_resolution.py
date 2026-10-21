"""Contract, ambiguity, scope and read-only transport checks."""
import sqlite3
from pathlib import Path
import tempfile
import unittest
from types import SimpleNamespace

import manualdb
from core.master_resolution_contracts import MASTER_RESOLVE_ROUTE
from services.master_resolution import NameResolver, validate_request
from transport.access_rules import authorize
from transport.master_resolution_routes import build_master_resolution_routes


class ResolutionTests(unittest.TestCase):
    def setUp(self):
        self.ports = [{"name": "Pecém", "aliases": "佩森港"},
                      {"name": "Colón"}, {"name": "Cristóbal", "aliases": "Colon"}]
        self.groups = [{"name": "NEB", "label": "北巴", "kind": "pod"}]
        self.resolver = NameResolver(self.ports, [], [], self.groups, [])

    def test_example_and_canonical_priority(self):
        self.assertEqual(self.resolver.resolve("pod", "北巴")["canonical"], "NEB")
        self.assertEqual(self.resolver.resolve("pod", "佩森港")["canonical"], "Pecém")
        self.assertEqual(self.resolver.resolve("pod", "Colon")["canonical"], "Colón")
        self.assertEqual(self.resolver.resolve("pol", "北巴")["status"], "unknown")

    def test_conflicting_folded_canonical_and_alias(self):
        resolver = NameResolver([{"name":"Vitoria"}, {"name":"Vitória"}], [], [], [], [])
        self.assertEqual(resolver.resolve("pod", "Vitoria")["status"], "ambiguous")
        resolver = NameResolver([{"name":"A", "aliases":"same"}, {"name":"B", "aliases":"same"}], [], [], [], [])
        self.assertIsNone(resolver.resolve("pod", "same")["canonical"])
        groups = [{"name":"A", "label":"same", "kind":"pod"}, {"name":"B", "label":"same", "kind":"pod"}]
        self.assertEqual(NameResolver([],[],[],groups,[]).resolve("pod","same")["status"], "ambiguous")

    def test_scope_target_and_canonical_protection(self):
        scopes = [{"alias":"local", "kind":"pod", "scope_group":"G", "target":"NEB"},
                  {"alias":"bad", "kind":"pod", "scope_group":"G", "target":"missing"},
                  {"alias":"Colón", "kind":"pod", "scope_group":"G", "target":"Cristóbal"}]
        resolver = NameResolver(self.ports, [], [], self.groups, scopes)
        self.assertEqual(resolver.resolve("pod","local","G")["canonical"], "NEB")
        self.assertEqual(resolver.resolve("pod","local","Other")["status"], "unknown")
        self.assertEqual(resolver.resolve("pod","bad","G")["status"], "invalid_target")
        self.assertEqual(resolver.resolve("pod","Colón","G")["canonical"], "Colón")

    def test_request_and_permissions(self):
        for body in ({}, {"items":[]}, {"items":[{"type":[],"value":"a"}]},
                     {"items":[{"type":"pod","value":1}]}, {"items":[{"type":"pod","value":"x"}]*201}):
            with self.assertRaises(ValueError):
                validate_request(body)
        for role in ("sales", "operator", "admin"):
            self.assertTrue(authorize("POST", MASTER_RESOLVE_ROUTE, role)["ok"])
        self.assertTrue(authorize("POST", MASTER_RESOLVE_ROUTE, agent=True)["ok"])
        self.assertFalse(authorize("POST", MASTER_RESOLVE_ROUTE)["ok"])

    def test_document_audit_preserves_business_instructions(self):
        from ops.audit_master_resolution import audit
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        try:
            manualdb.init_schema(conn)
            conn.execute("INSERT INTO ports(name,active) VALUES('Ningbo',1)")
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                folder = root / "test"
                folder.mkdir()
                (folder / "rules.md").write_text(
                    "| 写法 | 归一目标 | 作用域 |\n|---|---|---|\n"
                    "| `NB` `SH` | 各自规范港名（`NB`=Ningbo） | 起运港 |\n"
                    "| `不经巴拿马` | 不是港口，进 `备注` | 目的港 |\n", encoding="utf-8")
                result = audit(manualdb.Master(conn), root)["data"]
                self.assertEqual(result["document_counts"], {"review":2})
                self.assertTrue(all("alias" not in row for row in result["document_inventory"]))
        finally:
            conn.close()

    def test_transport_closes_connection_without_write(self):
        events = []
        connection = SimpleNamespace(close=lambda:events.append("close"))
        routes = build_master_resolution_routes(lambda:connection, lambda _:SimpleNamespace(name_resolver=self.resolver))
        result = routes[MASTER_RESOLVE_ROUTE]({"items":[{"type":"pod","value":"佩森港"}]})
        self.assertEqual(result["data"][0]["canonical"], "Pecém")
        self.assertEqual(events, ["close"])

    def test_shadow_does_not_change_legacy_conclusions(self):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        try:
            manualdb.init_schema(conn)
            conn.execute("INSERT INTO ports(name,active) VALUES('Vitoria',1),('Vitória',1)")
            master = manualdb.Master(conn)
            record = {"目的港":"Vitoria"}
            resolved, errors = manualdb.resolve_core(master, "rates", record)
            actual = manualdb.validate_record(conn, master, "rates", record)
            self.assertEqual(actual["resolved"], resolved)
            self.assertEqual(actual["errors"], errors)
            self.assertTrue(actual["name_resolution"]["equal"])
            self.assertTrue(any(error['field'] == '目的港' for error in errors))
        finally:
            conn.close()
