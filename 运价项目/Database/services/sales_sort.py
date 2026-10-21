"""Pure sales ordering rules; no IO and no user-supplied SQL identifiers."""
from datetime import date
from functools import cmp_to_key
from core.contracts import (SALES_SORT_PARAM, SALES_SORT_DIRECTION_PARAM,
                            SALES_SORT_FIELDS, SALES_SORT_DIRECTIONS)

PRICE_CABINETS = {"price_20gp": "20GP", "price_40hq": "40HQ/HC", "price_40nor": "40NOR", "price_45hq": "45HQ/HC"}

def parse_sales_sort(query, grouped=False):
    field = str(query.get(SALES_SORT_PARAM) or "").strip()
    direction = str(query.get(SALES_SORT_DIRECTION_PARAM) or "asc").strip()
    if direction not in SALES_SORT_DIRECTIONS:
        raise ValueError("排序方向只能是 asc 或 desc")
    if not field:
        return "", direction
    if field not in SALES_SORT_FIELDS or (grouped and field == "price"):
        raise ValueError("不支持的销售排序字段")
    return field, direction

def sales_order_sql(query):
    field, direction = parse_sales_sort(query)
    if not field:
        return "message_time DESC, id DESC"  # Preserve other consumers' existing default.
    if field in PRICE_CABINETS:
        value = "CASE WHEN container_type = '" + PRICE_CABINETS[field] + "' THEN price_usd ELSE NULL END"
        return "CASE WHEN (" + value + ") IS NULL THEN 1 ELSE 0 END ASC, " + value + " " + direction.upper() + ", message_time DESC, id DESC"
    columns = {"etd": ("etd",), "validity": ("valid_from", "valid_to"),
               "synced_at": ("synced_at",), "price": ("price_usd",)}[field]
    clauses = []
    for column in columns:
        value = column if column == "price_usd" else "NULLIF(" + column + ", '')"
        clauses.extend(["CASE WHEN " + value + " IS NULL THEN 1 ELSE 0 END ASC",
                        value + " " + direction.upper()])
    return ", ".join(clauses + ["message_time DESC", "id DESC"])

def _date_key(value):
    try:
        return date.fromisoformat(str(value or "")[:10]).isoformat()
    except ValueError:
        return None  # Legacy unparsed dates are unknown, never guessed.

def _group_values(group, field):
    row = group.get("current") or next(iter(group.get("history") or []), {})
    if field in PRICE_CABINETS:
        return ((row.get("cab") or {}).get(PRICE_CABINETS[field]),)
    if field == "validity":
        return (_date_key(row.get("valid_from")), _date_key(row.get("valid_to")))
    if field == "etd":
        return (_date_key(row.get("etd")),)
    return (str(row.get("synced_at") or "") or None,)

def sort_sales_groups(groups, query):
    field, direction = parse_sales_sort(query, grouped=True)
    if not field:
        return groups
    def compare(left, right):
        for a, b in zip(_group_values(left, field), _group_values(right, field)):
            a = None if a == "" else a
            b = None if b == "" else b
            if a is None or b is None:
                if (a is None) != (b is None):
                    return 1 if a is None else -1
                continue
            result = (a > b) - (a < b)
            if result:
                return result if direction == "asc" else -result
        return 0  # Stable ties retain the cached baseline order.
    return sorted(groups, key=cmp_to_key(compare))
