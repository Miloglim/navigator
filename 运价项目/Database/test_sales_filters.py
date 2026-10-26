"""Sales query regressions using isolated rows on either database adapter."""
import sqlite3
import json
import unittest

import manualdb


def verify_sales_filters(test, connection, master):
    rows = [
        ("expired", "20GP", "2026-09-01", "2026-09-27", "南美东", "测试群"),
        ("boundary", "20GP", "2026-09-01", "2026-09-28", "南美东", "测试群"),
        ("future", "40HQ/HC", "2026-10-01", "2026-10-31", "南美东", "测试群"),
        ("nor", "40NOR", "2026-10-01", "2026-10-31", "南美东支线", "测试群二"),
        ("open", "40NOR", "2026-10-01", None, "南美东", "测试群"),
    ]
    columns = {r["name"] for r in connection.execute("PRAGMA table_info(freight_items)")}
    for key, ctype, start, end, route, group in rows:
        data = dict(content_key=key, record_type="rates", container_type=ctype,
                    valid_from=start, valid_to=end, route=route, source_group=group,
                    carrier="CMA", pol="Tianjin", pod="Santos",
                    message_time="2026-09-28 10:00:00", status="当前生效")
        if "entry_id" in columns:
            data["entry_id"] = "sales-test-" + key
        connection.execute(
            "INSERT INTO freight_items (" + ",".join(data) + ") VALUES ("
            + ",".join("?" for _ in data) + ")", tuple(data.values()))
    connection.execute(
        "UPDATE freight_items SET message_text=?, remark=? WHERE content_key=?",
        ("ASUNCION addon +1000/+1200", "RIO GRANDE addon +USD250/500", "future"))
    connection.execute("UPDATE freight_items SET pod=?,message_text=? WHERE content_key=?",
                       ("Paranagua", "Santos feeder transfer", "nor"))
    connection.commit()

    def keys(q):
        return {r["content_key"] for r in manualdb.query(connection, master, "rates", q)["rows"]}

    test.assertEqual(keys({"ctype": "20GP,40HQ/HC"}), {"expired", "boundary", "future"})
    test.assertEqual(keys({"ctype": "40HQ/HC"}), {"future"})
    test.assertEqual(keys({"ctype": "20GP,40HQ/HC,40NOR"}), {r[0] for r in rows})
    test.assertEqual(keys({"valid_since": "2026-09-28"}), {"boundary", "future", "nor", "open"})
    test.assertEqual(keys({"valid_since": "2026-11-01"}), {"open"})
    test.assertEqual(keys({"valid": "2026-09-28"}), {"boundary"})
    test.assertEqual(keys({"route": "南美东", "group": "测试群", "valid_since": "2026-09-28",
                           "ctype": "20GP,40HQ/HC"}), {"boundary", "future"})
    test.assertEqual(keys({"group": "测试"}), set())
    test.assertEqual(keys({"carrier": "CMA,MSC"}), {r[0] for r in rows})
    test.assertEqual(keys({"carrier": "MSC,MAERSK"}), set())
    test.assertEqual(keys({"route": "南美"}), set())
    test.assertEqual(keys({"q": "Santos"}), {r[0] for r in rows})
    for term in ("Asuncion", "asuncion", "Asunción", "ASUNCIÓN", "Rio Grande"):
        test.assertEqual(keys({"q": term}), {"future"}, term)
    test.assertEqual(keys({"pod": "Asuncion"}), set(), "port filters must not fall back to whole-message text")
    test.assertEqual(keys({"pol": "Asunción"}), set(), "port filters must not fall back to whole-message text")
    test.assertEqual(keys({"pod": "Asuncion", "global_query": "1"}), {"future"})
    test.assertEqual(keys({"pol": "Asunción", "global_query": "1"}), {"future"})
    test.assertEqual(keys({"pod": "Santos"}), {"expired", "boundary", "future", "open"},
                     "port filters must ignore sibling text in message_text")
    preferred = {"display_mode": "preferred", "display_routes": json.dumps(["南美东"]),
                 "display_groups": json.dumps(["测试群"], ensure_ascii=False)}
    test.assertEqual(keys(preferred), {"expired", "boundary", "future", "open"})
    test.assertEqual(keys({"route": "南美东支线", "group": "测试群二"}), {"nor"})
    test.assertEqual({row["name"]: row["rates"] for row in manualdb.sales_group_summary(connection, master, {})["rows"]},
                     {"测试群": 4, "测试群二": 1})
    test.assertEqual([row["name"] for row in manualdb.sales_group_summary(connection, master, preferred)["rows"]], ["测试群"])
    test.assertEqual(keys({"display_mode": "search", **{key: value for key, value in preferred.items() if key != "display_mode"}}),
                     {row[0] for row in rows})
    with test.assertRaises(ValueError):
        keys({"display_mode": "preferred", "display_groups": '"测试群"'})
    multi_ports = (("multi-pair-a", "Shanghai", "Santos"), ("multi-pair-b", "Ningbo", "Paranagua"))
    for key, pol, pod in multi_ports:
        data = dict(content_key=key, record_type="rates", container_type="20GP",
                    valid_from="2026-10-01", valid_to="2026-10-31", route="测试多港",
                    source_group="测试群", carrier="CMA", pol=pol, pod=pod,
                    message_time="2026-09-28 10:00:00", status="当前生效")
        if "entry_id" in columns:
            data["entry_id"] = "sales-test-" + key
        connection.execute(
            "INSERT INTO freight_items (" + ",".join(data) + ") VALUES ("
            + ",".join("?" for _ in data) + ")", tuple(data.values()))
    connection.commit()
    connection.execute("UPDATE freight_items SET message_text=? WHERE content_key=?",
                       ("Shanghai Santos alternate ports", "multi-pair-b"))
    connection.commit()
    test.assertEqual(keys({"pol": "Shanghai,Ningbo", "pod": "Santos,Paranagua"}),
                     {"multi-pair-a", "multi-pair-b"},
                     "multiple POL and POD values must OR within each side and AND across sides")
    test.assertEqual(keys({"pol": "Shanghai", "pod": "Santos"}), {"multi-pair-a"},
                     "sibling text in the same message must not leak through port filters")
    test.assertEqual(keys({"pol": "Shanghai", "pod": "Santos", "global_query": "1"}),
                     {"multi-pair-a", "multi-pair-b"},
                     "global query explicitly includes sibling message text")
    with test.assertRaisesRegex(ValueError, "起运港最多选择 3 个"):
        keys({"pol": "Shanghai,Ningbo,Santos,Paranagua"})
    with test.assertRaisesRegex(ValueError, "目的港最多选择 3 个"):
        keys({"pod": "Santos,Paranagua,Shanghai,Ningbo"})
    folded = manualdb.query_grouped(connection, master, "rates",
                                    {"ctype": "40HQ/HC", "valid_since": "2026-09-28"})
    test.assertEqual(folded["total"], 1)

    for key, route, service in (("space-route", "地中海", "MD1"),
                                ("space-service", "", "WSA")):
        data = dict(content_key=key, record_type="space", route=route,
                    service_name=service, carrier="YML", pol="Shanghai", pod="",
                    message_time="2026-10-08 10:00:00", status="当前有效")
        if "entry_id" in columns:
            data["entry_id"] = "sales-test-" + key
        connection.execute(
            "INSERT INTO freight_items (" + ",".join(data) + ") VALUES ("
            + ",".join("?" for _ in data) + ")", tuple(data.values()))
    connection.commit()

    def space_keys(q):
        return {r["content_key"] for r in manualdb.query(connection, master, "space", q)["rows"]}

    test.assertEqual(space_keys({"route": "地中海"}), {"space-route"})
    test.assertEqual(space_keys({"route": "WSA"}), {"space-service"})
    test.assertEqual(space_keys({"carrier": "YML,MSC"}), {"space-route", "space-service"})
    test.assertIn("WSA", manualdb.options(connection)["space"]["service_name"])
    with test.assertRaises(ValueError):
        keys({"valid_since": "2026-02-30"})


class SalesFilterTests(unittest.TestCase):
    def test_sqlite_sales_query_boundaries(self):
        connection = sqlite3.connect(":memory:")
        connection.row_factory = sqlite3.Row
        try:
            manualdb.init_schema(connection)
            verify_sales_filters(self, connection, manualdb.Master(connection))
        finally:
            connection.close()


if __name__ == "__main__":
    unittest.main()
