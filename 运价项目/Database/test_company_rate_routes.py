import unittest

from core.company_rates_contracts import (
    COMPANY_RATE_QUERY_ROUTE,
    COMPANY_RATE_TASK_ROUTE,
    COMPANY_REALTIME_QUERY_ROUTE,
)
from transport.access_rules import authorize
from transport.company_rate_routes import build_company_rate_routes


class FakeRuntime:
    def __init__(self):
        self.created = []

    def create(self, user_id, kind, query):
        self.created.append((user_id, kind, query))
        return {"task_id": "crt-1", "cached": None}

    def get(self, user_id, task_id):
        return {"task_id": task_id, "user_id": user_id, "status": "queued"} if task_id == "crt-1" else None


class CompanyRateRouteTests(unittest.TestCase):
    def test_sales_can_create_and_read_only_own_company_tasks(self):
        runtime = FakeRuntime()
        get_routes, post_routes = build_company_rate_routes(runtime)
        body = {"pol": ["CNSHA"], "pod": ["USLAX"], "_actor_id": "usr-1", "_role": "sales"}
        created = post_routes[COMPANY_RATE_QUERY_ROUTE](dict(body))
        self.assertEqual(created, {"ok": True, "data": {"task_id": "crt-1", "cached": None}})
        self.assertEqual(runtime.created[0][0:2], ("usr-1", "company"))
        self.assertNotIn("_actor_id", runtime.created[0][2])
        read = get_routes[COMPANY_RATE_TASK_ROUTE]({"id": "crt-1", "_actor_id": "usr-1"})
        self.assertEqual(read["data"]["status"], "queued")
        with self.assertRaisesRegex(LookupError, "不存在"):
            get_routes[COMPANY_RATE_TASK_ROUTE]({"id": "other", "_actor_id": "usr-1"})

    def test_routes_require_logged_in_account_and_realtime_is_explicit_post(self):
        for role in ("sales", "operator", "admin"):
            self.assertTrue(authorize("POST", COMPANY_REALTIME_QUERY_ROUTE, role)["ok"])
            self.assertTrue(authorize("GET", COMPANY_RATE_TASK_ROUTE, role)["ok"])
        self.assertFalse(authorize("POST", COMPANY_RATE_QUERY_ROUTE)["ok"])


if __name__ == "__main__":
    unittest.main()
