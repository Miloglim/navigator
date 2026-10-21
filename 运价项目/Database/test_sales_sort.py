"""Sales ordering checks on disposable databases, including pagination and cache reuse."""
import sqlite3
import unittest
from copy import deepcopy
import manualdb
from services.sales_sort import sort_sales_groups

def seed_sales_sort(connection):
    # Keep fixtures in the future so the grouped query's real expiry filter
    # does not make this ordering test depend on the calendar date.
    specs = [("A", 500, "2099-11-03", "2099-10-02", "2099-11-30", "2026-09-29 12:00:00"),
             ("B", 100, "2099-11-01", "2099-10-01", "2099-10-31", "2026-09-29 11:00:00"),
             ("C", 300, "2099-11-02", "2099-10-01", "2099-10-15", "2026-09-29 10:00:00"),
             ("D", None, "", "", "", ""),
             ("E", 0, "2099-11-01", "2099-10-01", "2099-10-15", "2026-09-29 09:00:00")]
    for kind in ("rates", "space"):
        for label, price, etd, start, end, stamp in specs:
            for ctype in (("20GP", "40HQ/HC", "40NOR") if kind == "rates" else ("40HQ/HC",)):
                data = dict(entry_id=kind+label+ctype, content_key=kind+label+ctype,record_type=kind,
                            pod=label,pol="Tianjin",carrier="CMA",route="Test",container_type=ctype,
                            price_usd=price,etd=etd,valid_from=start,valid_to=end,synced_at=stamp,
                            message_time="2026-09-29 08:00:00",status="当前生效" if kind == "rates" else "当前有效")
                connection.execute("INSERT INTO freight_items ("+",".join(data)+") VALUES ("+",".join("?" for _ in data)+")",tuple(data.values()))
    connection.commit()

def verify_sales_sort(case, connection):
    manualdb._GROUPED_CACHE.clear()
    seed_sales_sort(connection)
    master=manualdb.Master(connection)
    def flat(field, direction, offset=0, limit=20):
        return manualdb.query(connection,master,"space",dict(sort=field,direction=direction,limit=str(limit),offset=str(offset)))["rows"]
    def grouped(field, direction, offset=0, limit=20):
        return manualdb.query_grouped(connection,master,"rates",dict(sort=field,direction=direction,limit=str(limit),offset=str(offset)))["groups"]
    def labels(rows, group=False):
        return [r["current"]["pod"] if group else r["pod"] for r in rows]
    for field in ("price_20gp", "price_40hq", "price_40nor"):
        case.assertEqual(labels(grouped(field,"asc"),True),["E","B","C","A","D"])
        case.assertEqual(labels(grouped(field,"desc"),True),["A","C","B","E","D"])
        case.assertEqual(labels(grouped(field,"asc",2,2),True),["C","A"])
    case.assertEqual(labels(flat("price","asc")),["E","B","C","A","D"])
    case.assertEqual(labels(flat("price","desc",2,2)),["B","E"])
    case.assertEqual(labels(flat("synced_at","desc")),["A","B","C","E","D"])
    case.assertEqual(labels(grouped("synced_at","asc"),True),["E","C","B","A","D"])
    case.assertEqual(labels(flat("validity","asc"))[-2:],["A","D"])
    case.assertEqual(labels(grouped("validity","asc"),True)[:2],["E","C"])
    case.assertEqual(labels(grouped("validity","desc"),True),["A","B","E","C","D"])
    case.assertEqual(labels(grouped("etd","desc"),True),["A","C","E","B","D"])
    case.assertEqual(labels(flat("etd","asc"))[-2:],["A","D"])
    baseline=manualdb.query_grouped(connection,master,"rates",{})
    grouped("price_20gp","asc");grouped("price_20gp","desc")
    case.assertEqual(manualdb.query_grouped(connection,master,"rates",{}),baseline)
    groups=grouped("price_20gp","asc",0,2)
    case.assertEqual(len(groups),2);case.assertEqual(len(groups[0]["current"]["cab"]),3)
    for bad in ({"sort":"price; DROP TABLE freight_items"},{"sort":"price","direction":"sideways"}):
        with case.assertRaises(ValueError):manualdb.query(connection,master,"space",bad)
    case.assertEqual(connection.execute("SELECT COUNT(*) FROM freight_items").fetchone()[0],20)

class SalesSortTests(unittest.TestCase):
    def test_sqlite_global_sort_before_pagination(self):
        connection=sqlite3.connect(":memory:");connection.row_factory=sqlite3.Row
        try:
            manualdb.init_schema(connection);verify_sales_sort(self,connection)
        finally:connection.close();manualdb._GROUPED_CACHE.clear()

    def test_grouped_result_is_not_silently_truncated(self):
        count = 10001
        connection=sqlite3.connect(":memory:");connection.row_factory=sqlite3.Row
        try:
            manualdb.init_schema(connection);manualdb._GROUPED_CACHE.clear()
            connection.executemany("INSERT INTO freight_items(entry_id,content_key,record_type,pod,carrier,container_type,price_usd,status) VALUES(?,?,'rates',?,'CMA','20GP',?,'当前生效')",
                                   (("bulk-"+str(i),"bulk-"+str(i),"P"+str(i),count-i) for i in range(count)))
            connection.commit()
            result=manualdb.query_grouped(connection,manualdb.Master(connection),"rates",{"sort":"price_20gp","direction":"asc","limit":"1"})
            self.assertEqual(result["total"],count)
            self.assertEqual(result["groups"][0]["current"]["pod"],"P10000")
        finally:connection.close();manualdb._GROUPED_CACHE.clear()

    def test_history_and_missing_values_stay_attached(self):
        groups=[{"current":{"cab":{"20GP":200}},"history":[{"entry_id":"old-a"}]},
                {"current":{"cab":{"20GP":100}},"history":[{"entry_id":"old-b"}]}]
        before=deepcopy(groups)
        ordered=sort_sales_groups(groups,{"sort":"price_20gp","direction":"asc"})
        self.assertEqual(ordered[0]["history"][0]["entry_id"],"old-b");self.assertEqual(groups,before)

if __name__ == "__main__":unittest.main()
