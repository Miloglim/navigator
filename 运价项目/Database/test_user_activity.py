import json
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

import manualdb
from core.activity_contracts import (
    ACTIVITY_EVENTS_ROUTE,
    ACTIVITY_PAGE_OPEN_ROUTE,
    ACTIVITY_QUOTE_USAGE_ROUTE,
    ACTIVITY_SUMMARY_ROUTE,
    ACTIVITY_TIMELINE_ROUTE,
)
from ops import user_activity
from services.user_activity import page_open_payload, quote_usage_payload, search_properties
from transport.access_rules import authorize


class UserActivityTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.connection = manualdb.connect(Path(self.directory.name) / "activity.sqlite3")
        self.connection.execute(
            "INSERT INTO operators(user_id,name,display_name,role,active) VALUES(?,?,?,?,?)",
            ("usr_sales", "sales-test", "销售测试", "sales", 1),
        )
        self.connection.commit()

    def tearDown(self):
        self.connection.close()
        self.directory.cleanup()

    def test_page_open_is_idempotent_and_server_identity_is_used(self):
        body = {
            "page": "/",
            "client_event_id": "page-test-1",
            "_actor_id": "usr_sales",
            "_role": "sales",
            "_client_ip": "10.0.0.8",
        }
        first = user_activity.record_page_open(self.connection, body)
        second = user_activity.record_page_open(self.connection, body)

        self.assertTrue(first["data"]["recorded"])
        self.assertFalse(second["data"]["recorded"])
        row = self.connection.execute(
            "SELECT user_id,client_ip,properties_json FROM user_activity_events"
        ).fetchone()
        self.assertEqual("usr_sales", row["user_id"])
        self.assertEqual("10.0.0.8", row["client_ip"])
        self.assertEqual({"page": "/"}, json.loads(row["properties_json"]))

    def test_only_intentional_business_search_is_recorded(self):
        base = {
            "_actor_id": "usr_sales", "_role": "sales", "_client_ip": "10.0.0.8",
            "pod": "ECPOS", "limit": "50", "offset": "0", "token": "secret",
        }
        skipped = user_activity.record_sales_search(
            self.connection, base, "/api/rates_grouped", {"total": 4, "groups": []}
        )
        recorded = user_activity.record_sales_search(
            self.connection, {**base, "activity": "intentional"},
            "/api/rates_grouped", {"total": 4, "groups": []},
        )

        self.assertFalse(skipped["recorded"])
        self.assertTrue(recorded["recorded"])
        row = self.connection.execute(
            "SELECT properties_json,result_count FROM user_activity_events"
        ).fetchone()
        properties = json.loads(row["properties_json"])
        self.assertEqual("ECPOS", properties["pod"])
        self.assertEqual("intentional", properties["activity"])
        self.assertNotIn("token", properties)
        self.assertEqual(4, row["result_count"])

    def test_prospector_audit_keeps_bounded_query_fields_without_identity(self):
        properties=search_properties({'_client_app':'prospector','terms':json.dumps([' Santos ','Santos','Brazil']),
            'lanes':json.dumps(['ECSA']),'podExtra':json.dumps(['Brazil']),'polExtra':json.dumps(['South China']),
            'lane':'ECSA','container':'40HQ','_actor_id':'forged','password':'secret'},'/api/rates')
        self.assertEqual(properties['terms'],['Santos','Brazil'])
        self.assertEqual(properties['lanes'],['ECSA'])
        self.assertEqual(properties['podExtra'],['Brazil'])
        self.assertEqual(properties['polExtra'],['South China'])
        self.assertEqual(properties['container'],'40HQ')
        self.assertEqual(properties['lane'],'ECSA')
        self.assertNotIn('_actor_id',properties)
        self.assertNotIn('password',properties)
        self.assertNotIn('terms',search_properties({'terms':'["Santos"]'},'/api/rates'))
        for terms in ('invalid',json.dumps(['x'*513]),json.dumps(['x']*65)):
            with self.assertRaises(ValueError): search_properties({'_client_app':'prospector','terms':terms},'/api/rates')

    def test_admin_summary_and_event_details(self):
        user_activity.record_page_open(self.connection, {
            "page": "/", "client_event_id": "page-test-2",
            "_actor_id": "usr_sales", "_role": "sales", "_client_ip": "127.0.0.1",
        })
        user_activity.record_sales_search(self.connection, {
            "pod": "ECPOS", "route": "南美西", "activity": "intentional",
            "_actor_id": "usr_sales", "_role": "sales", "_client_ip": "127.0.0.1",
        }, "/api/space", {"total": 0, "rows": []})

        summary = user_activity.summary(self.connection, {"days": "7"})["data"]
        details = user_activity.events(self.connection, {"days": "7"})["data"]

        self.assertEqual(1, summary["dau"])
        self.assertEqual(1, summary["searches"])
        self.assertEqual(1, summary["zero_result_searches"])
        self.assertEqual([{"value": "ECPOS", "count": 1}], summary["top"]["pods"])
        self.assertEqual(2, details["total"])
        self.assertEqual("销售测试", details["rows"][0]["display_name"])

    def test_quote_usage_records_only_allowlisted_metadata_and_joins_timeline(self):
        result = user_activity.record_quote_usage(self.connection, {
            "action": "cell_edited", "quote_kind": "rates", "item_count": 2,
            "field_type": "price", "_actor_id": "usr_sales", "_role": "sales",
            "_client_ip": "127.0.0.1",
        })
        self.assertTrue(result["data"]["recorded"])
        row = self.connection.execute(
            "SELECT user_id,role,quote_kind,item_count,field_type FROM quote_usage_events"
        ).fetchone()
        self.assertEqual(("usr_sales", "sales", "rates", 2, "price"), tuple(row))

        timeline = user_activity.timeline(self.connection, {
            "days": "7", "category": "quote", "limit": "10",
        })["data"]
        self.assertEqual(1, timeline["total"])
        self.assertEqual("cell_edited", timeline["rows"][0]["action"])
        self.assertEqual({"quote_kind": "rates", "item_count": 2, "field_type": "price"},
                         timeline["rows"][0]["details"])
        self.assertNotIn("4500", json.dumps(timeline["rows"][0]))
        self.assertNotIn("secret", json.dumps(timeline["rows"][0]))
        summary = user_activity.summary(self.connection, {"days": "7"})["data"]
        self.assertEqual(1, summary["events"])
        self.assertEqual(0, summary["searches"], "quote editing is not a business search")

    def test_postgres_activity_queries_use_postgres_json_constructor(self):
        class QueryCapture:
            engine = "postgres"

            def __init__(self):
                self.sql = []

            def execute(self, sql, params=()):
                self.sql.append(sql)
                class Result:
                    def __iter__(self):
                        return iter(())

                    def fetchone(self):
                        return (0,)

                return Result()

        for query in (user_activity.summary, user_activity.timeline):
            connection = QueryCapture()
            query(connection, {"days": "7"})
            sql = " ".join(connection.sql)
            self.assertIn("json_build_object(", sql)
            self.assertIn(")::text", sql)
            self.assertNotIn("json_object(", sql)

    def test_quote_usage_contract_rejects_content_and_unknown_actions(self):
        with self.assertRaises(ValueError):
            quote_usage_payload({"action": "cell_edited", "quote_kind": "rates",
                                 "item_count": 1, "field_type": "price", "value": "4500"})
        with self.assertRaises(ValueError):
            quote_usage_payload({"action": "unknown", "quote_kind": "rates",
                                 "item_count": 1, "field_type": "price"})

    def test_active_user_windows_do_not_shrink_with_report_days(self):
        eight_days_ago = datetime.now(user_activity.BUSINESS_TIMEZONE) - timedelta(days=8)
        user_activity._insert(
            self.connection, "usr_sales", "sales", "page_open", {"page": "/"},
            None, "127.0.0.1", "page-eight-days", eight_days_ago.isoformat(timespec="seconds"),
        )

        summary = user_activity.summary(self.connection, {"days": "1"})["data"]

        self.assertEqual(0, summary["dau"])
        self.assertEqual(0, summary["wau"])
        self.assertEqual(1, summary["mau"])
        self.assertEqual(0, summary["events"])

    def test_contract_and_permissions_reject_client_identity(self):
        with self.assertRaises(ValueError):
            page_open_payload({"page": "/", "client_event_id": "page-1", "user_id": "fake"})
        self.assertNotIn("password", search_properties({"password": "secret"}, "/api/rates"))
        self.assertTrue(authorize("POST", ACTIVITY_PAGE_OPEN_ROUTE, "sales")["ok"])
        self.assertTrue(authorize("POST", ACTIVITY_QUOTE_USAGE_ROUTE, "sales")["ok"])
        self.assertFalse(authorize("POST", ACTIVITY_QUOTE_USAGE_ROUTE, None)["ok"])
        self.assertTrue(authorize("GET", ACTIVITY_SUMMARY_ROUTE, "admin")["ok"])
        self.assertTrue(authorize("GET", ACTIVITY_EVENTS_ROUTE, "admin")["ok"])
        self.assertTrue(authorize("GET", ACTIVITY_TIMELINE_ROUTE, "admin")["ok"])
        self.assertFalse(authorize("GET", ACTIVITY_SUMMARY_ROUTE, "sales")["ok"])
        self.assertFalse(authorize("GET", ACTIVITY_TIMELINE_ROUTE, "sales")["ok"])

    def test_timeline_merges_and_classifies_activity_and_account_events(self):
        user_activity.record_page_open(self.connection, {
            "page": "/", "client_event_id": "timeline-page",
            "_actor_id": "usr_sales", "_role": "sales", "_client_ip": "10.0.0.8",
        })
        user_activity.record_sales_search(self.connection, {
            "pod": "Posorja", "route": "南美西", "activity": "intentional",
            "_actor_id": "usr_sales", "_role": "sales", "_client_ip": "10.0.0.8",
        }, "/api/rates_grouped", {"total": 13, "groups": []})
        self.connection.execute(
            "INSERT INTO account_events(event_id,user_id,actor_id,actor_name,action,detail_json,created_at) "
            "VALUES(?,?,?,?,?,?,?)",
            ("evt_login", "usr_sales", "usr_sales", "销售测试", "login_success",
             json.dumps({"client_ip": "10.0.0.8"}), user_activity._stamp()),
        )
        self.connection.execute(
            "INSERT INTO account_events(event_id,user_id,actor_id,actor_name,action,detail_json,created_at) "
            "VALUES(?,?,?,?,?,?,?)",
            ("evt_profile", "usr_sales", "usr_admin", "管理员", "profile_updated",
             json.dumps({"department": {"before": "", "after": "销售部"}}, ensure_ascii=False),
             user_activity._stamp()[:10] + " 23:59:59"),
        )
        self.connection.commit()

        all_rows = user_activity.timeline(self.connection, {"days": "30", "limit": "20"})["data"]
        query_rows = user_activity.timeline(
            self.connection, {"days": "30", "category": "query", "q": "Posorja"}
        )["data"]
        security_rows = user_activity.timeline(
            self.connection, {"days": "30", "category": "security", "user_id": "usr_sales"}
        )["data"]

        self.assertEqual({"access", "query", "security", "management"},
                         {row["category"] for row in all_rows["rows"]})
        self.assertEqual(4, all_rows["total"])
        self.assertEqual("profile_updated", all_rows["rows"][0]["action"])
        self.assertEqual(1, query_rows["total"])
        self.assertEqual("sales_search", query_rows["rows"][0]["action"])
        self.assertEqual(13, query_rows["rows"][0]["result_count"])
        self.assertEqual("Posorja", query_rows["rows"][0]["details"]["pod"])
        self.assertEqual(1, security_rows["total"])
        self.assertEqual("login_success", security_rows["rows"][0]["action"])

    def test_timeline_validates_category_and_keyword(self):
        with self.assertRaises(ValueError):
            user_activity.timeline(self.connection, {"category": "unknown"})
        with self.assertRaises(ValueError):
            user_activity.timeline(self.connection, {"q": "x" * 513})

    def test_timeline_can_aggregate_all_matching_events_by_account(self):
        user_activity.record_page_open(self.connection, {
            "page": "/", "client_event_id": "aggregate-page-1",
            "_actor_id": "usr_sales", "_role": "sales", "_client_ip": "10.0.0.8",
        })
        user_activity.record_page_open(self.connection, {
            "page": "/monitor", "client_event_id": "aggregate-page-2",
            "_actor_id": "usr_sales", "_role": "sales", "_client_ip": "10.0.0.8",
        })

        result = user_activity.timeline(self.connection, {
            "days": "30", "display_mode": "account", "limit": "20",
        })["data"]

        self.assertEqual(1, result["total"])
        self.assertEqual(2, result["rows"][0]["event_count"])
        self.assertEqual("usr_sales", result["rows"][0]["user_id"])
        self.assertEqual("销售测试", result["rows"][0]["display_name"])

    def test_hide_admin_filters_subject_and_actor_before_count_and_paging(self):
        self.connection.execute(
            "INSERT INTO operators(user_id,name,display_name,role,active) VALUES(?,?,?,?,?)",
            ("usr_admin", "admin-test", "管理员测试", "admin", 1),
        )
        for user_id, role in (("usr_admin", "admin"), ("usr_sales", "sales")):
            user_activity.record_page_open(self.connection, {
                "page": "/", "client_event_id": "hide-" + user_id,
                "_actor_id": user_id, "_role": role,
            })
        self.connection.execute(
            "INSERT INTO account_events(event_id,user_id,actor_id,actor_name,action,detail_json,created_at) "
            "VALUES(?,?,?,?,?,?,?)",
            ("evt_admin_edit", "usr_sales", "usr_admin", "管理员测试", "profile_updated", "{}", user_activity._stamp()),
        )
        self.connection.commit()

        all_rows = user_activity.timeline(self.connection, {"days": "7"})["data"]
        visible = user_activity.timeline(self.connection, {"days": "7", "hide_admin": "1", "limit": "1"})["data"]
        grouped = user_activity.timeline(self.connection, {"days": "7", "hide_admin": "1", "display_mode": "account"})["data"]
        summary = user_activity.summary(self.connection, {"days": "7", "hide_admin": "1"})["data"]

        self.assertEqual(3, all_rows["total"])
        self.assertEqual(1, visible["total"])
        self.assertEqual("usr_sales", visible["rows"][0]["user_id"])
        self.assertEqual(1, grouped["total"])
        self.assertEqual(1, grouped["rows"][0]["event_count"])
        self.assertEqual(1, summary["dau"])
        self.assertEqual(1, summary["page_opens"])
        self.assertEqual(1, user_activity.events(self.connection, {"days": "7", "hide_admin": "1"})["data"]["total"])
        with self.assertRaises(ValueError):
            user_activity.timeline(self.connection, {"hide_admin": "yes"})


if __name__ == "__main__":
    unittest.main()
