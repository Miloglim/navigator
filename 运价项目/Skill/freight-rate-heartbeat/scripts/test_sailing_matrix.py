import copy
import unittest
import record_builder as rb
from freight_images.services.completeness import matrix_errors

MSG = {"_slug": "turkey_price", "id": "sailing-fixture", "s": "fixture", "t": "2026-09-29 11:20", "c": "航次报价"}

def fixture():
    rows = [{"pol": pol, "etd": etd, "cells": [{"pod": "Sousse", "20GP": 3800, "40HQ/HC": 5500, "45HC": 7300, "remark": "VIA ALIAGA"}]}
            for pol, etd in (("Qingdao", "2026-10-12"), ("Taicang", "2026-10-13"), ("Ningbo", "2026-10-15"), ("Nansha", "2026-10-18"))]
    return {"type": "sailing_matrix", "carrier": "AKKON", "vessel": "M/V OU SHENG OSE0326W", "rows": rows,
            "source_inventory": {"pols": [{"pol": r["pol"], "etd": r["etd"]} for r in rows], "pod_rows": 1,
                                 "priced_cells": 12, "carrier": "AKKON", "vessel": "M/V OU SHENG OSE0326W"}}

class SailingMatrixTests(unittest.TestCase):
    def test_four_origins_split_prices_and_space_fields_across_dual_records(self):
        cache = fixture()
        out = rb.t_sailing_matrix_cache(MSG, cache)
        self.assertEqual(len(out), 24)
        self.assertEqual({r["record"]["起运港"] for r in out}, {"Qingdao", "Taicang", "Ningbo", "Nansha"})
        rates = [row for row in out if row["_meta"]["table"] == "rate"]
        spaces = [row for row in out if row["_meta"]["table"] == "space"]
        self.assertEqual((len(rates), len(spaces)), (12, 12))
        self.assertEqual({row["record"]["海运费USD"] for row in rates}, {3800, 5500, 7300})
        for row in rates:
            self.assertEqual(row["record"]["船名航次"], cache["vessel"])
            self.assertIn(row["record"]["海运费USD"], (3800, 5500, 7300))
            self.assertNotIn("价格USD", row["record"])
            self.assertNotIn("舱位类型", row["record"])
        for row in spaces:
            self.assertEqual(row["record"]["船名航次"], cache["vessel"])
            self.assertEqual(row["record"]["舱位类型"], "现舱")
            self.assertNotIn("海运费USD", row["record"])
            self.assertNotIn("价格USD", row["record"])
            self.assertIsNone(row["record"].get("箱量"))
    def test_missing_origin_or_priced_cell_fails_before_template(self):
        for mutation in (lambda c: c["rows"].pop(), lambda c: c["rows"][0]["cells"][0].update({"45HC": "N/A"}),
                         lambda c: c.update({"vessel": ""}), lambda c: c.update({"carrier": ""})):
            cache = fixture(); mutation(cache)
            self.assertTrue(matrix_errors(cache, "turkey_price"))
            self.assertIsNone(rb.t_sailing_matrix_cache(MSG, cache))
    def test_old_false_one_origin_limit_is_not_accepted(self):
        cache = {"type": "rate_matrix", "rows": [], "footer_notes": ["原图起运港还包含TAICANG、NINGBO、NANSHA"]}
        self.assertTrue(matrix_errors(cache, "turkey_price"))
    def test_past_etd_is_not_replaced_with_future_validity(self):
        cache = fixture(); cache["rows"][0]["etd"] = "2026-09-28"; cache["source_inventory"]["pols"][0]["etd"] = "2026-09-28"
        rows = rb.t_sailing_matrix_cache(MSG, cache)
        q = rows[0]["record"]
        self.assertEqual((q["船期ETD"], q["有效期止"]), ("2026-09-28", "2026-09-28"))
        self.assertIn("须人工核对", q["备注"])
    def test_legacy_vessel_matrix_requires_inventory_and_respects_group_kind(self):
        cache = fixture(); cache["type"] = "rate_matrix"
        rows = rb.t_rate_matrix_cache(MSG, cache)
        self.assertEqual({r["_meta"]["table"] for r in rows}, {"rate", "space"})
        del cache["source_inventory"]
        self.assertIsNone(rb.t_rate_matrix_cache(MSG, cache))

if __name__ == "__main__": unittest.main()
