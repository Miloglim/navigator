import sqlite3
import unittest

import manualdb
from ops.company_rate_store import create_task, find_fresh, get_task, save_result, update_task
from services.account_forms import profile_update


class CompanyRateStoreTests(unittest.TestCase):
    def setUp(self):
        self.connection = sqlite3.connect(":memory:")
        self.connection.row_factory = sqlite3.Row
        manualdb.init_schema(self.connection)

    def tearDown(self):
        self.connection.close()

    def test_saved_history_remains_but_only_unexpired_result_is_reusable(self):
        create_task(self.connection, "task-old", "usr-1", "company", "sig-1", {"pol_ids": [1]}, 100)
        save_result(self.connection, "task-old", "1:2", [{"product_id": 7}], 100, 110)
        update_task(self.connection, "task-old", "complete", 100)
        self.assertIsNone(find_fresh(self.connection, "usr-1", "company", "sig-1", 111))
        old = get_task(self.connection, "task-old", "usr-1")
        self.assertEqual(old["results"][0]["items"], [{"product_id": 7}])
        self.assertFalse(old["results"][0]["fresh"])

        create_task(self.connection, "task-new", "usr-1", "company", "sig-1", {"pol_ids": [1]}, 120)
        save_result(self.connection, "task-new", "1:2", [{"product_id": 8}], 120, 180)
        update_task(self.connection, "task-new", "complete", 120)
        fresh = find_fresh(self.connection, "usr-1", "company", "sig-1", 150)
        self.assertEqual(fresh["task_id"], "task-new")
        self.assertEqual(fresh["results"][0]["items"], [{"product_id": 8}])

    def test_task_owner_isolation(self):
        create_task(self.connection, "task-1", "usr-1", "company", "sig", {}, 100)
        self.assertIsNone(get_task(self.connection, "task-1", "usr-2"))

    def test_company_source_preferences_are_boolean_and_old_clients_preserve_them(self):
        base = {"routes": [], "groups": [], "ctypes": [], "start_tab": "rates"}
        _, saved = profile_update(
            {"display_name": "Sales", "preferences": {
                **base, "company_rates_enabled": True, "carrier_realtime_enabled": True,
            }}, [], [], existing_preferences={},
        )
        self.assertTrue(saved["company_rates_enabled"])
        self.assertTrue(saved["carrier_realtime_enabled"])
        _, preserved = profile_update(
            {"display_name": "Sales", "preferences": base}, [], [], existing_preferences=saved,
        )
        self.assertTrue(preserved["company_rates_enabled"])
        with self.assertRaisesRegex(ValueError, "公司查价设置"):
            profile_update(
                {"display_name": "Sales", "preferences": {**base, "company_rates_enabled": "yes"}},
                [], [], existing_preferences={},
            )


if __name__ == "__main__":
    unittest.main()
