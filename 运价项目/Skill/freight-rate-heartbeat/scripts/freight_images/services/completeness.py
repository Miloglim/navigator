"""Pure source-inventory comparison; no IO or master-data normalization."""
from datetime import date
from freight_images.core.contracts import MATRIX_TYPES, OMITTED_POL_MARKERS, SAILING_MATRIX_TYPE


def source_iso_date(value, message_date):
    raw = str(value or "").strip()
    if not raw:
        return ""
    parts = raw.replace("/", "-").replace(".", "-").split("-")
    try:
        if len(parts) == 2:
            return date(int(str(message_date)[:4]), int(parts[0]), int(parts[1])).isoformat()
        if len(parts) == 3:
            return date(*map(int, parts)).isoformat()
    except ValueError:
        return ""
    return ""


def is_sailing_matrix(cache, group=""):
    return (cache.get("type") == SAILING_MATRIX_TYPE or cache.get("record_kind") == "space"
            or (group == "turkey_price" and bool(cache.get("vessel"))))


def matrix_errors(cache, group=""):
    if cache.get("type") not in MATRIX_TYPES:
        return []
    errors = []
    notes = str(cache.get("footer_notes") or "")
    if any(marker in notes for marker in OMITTED_POL_MARKERS):
        errors.append("原图明示其他起运港，不能只放备注；需逐港结构化")
    inventory = cache.get("source_inventory")
    sailing = is_sailing_matrix(cache, group)
    if not inventory:
        if sailing:
            errors.append("航次价表缺少原图维度清单 source_inventory")
        return errors
    if not isinstance(inventory, dict):
        return errors + ["source_inventory必须为对象"]
    rows = cache.get("rows") or []
    if not rows or any(not isinstance(row, dict) for row in rows):
        return errors + ["矩阵rows为空或格式错误"]
    expected = inventory.get("pols")
    if not isinstance(expected, list) or not expected or any(not isinstance(row, dict) for row in expected):
        return errors + ["原图清单须逐项列出pol/etd"]
    if any(not isinstance(cell, dict) for row in rows for cell in row.get("cells") or []):
        return errors + ["目的港cell必须为对象"]
    wanted = {(str(row.get("pol") or ""), str(row.get("etd") or "")) for row in expected}
    actual = {(str(row.get("pol") or ""), str(row.get("etd") or "")) for row in rows}
    if len(wanted) != len(expected) or wanted != actual or any(not all(key) for key in wanted):
        errors.append("原图起运港/ETD清单与矩阵不一致")
    if (not isinstance(inventory.get("pod_rows"), int) or inventory["pod_rows"] <= 0
            or any(len(row.get("cells") or []) != inventory["pod_rows"] for row in rows)):
        errors.append("原图目的港行数与矩阵不一致")
    priced = sum(1 for row in rows for cell in row.get("cells") or []
                 for key,value in cell.items() if key in ("20GP", "40GP", "40HQ", "40HC", "40HQ/HC", "40NOR", "45HQ", "45HC")
                 and isinstance(value, (int, float)) and not isinstance(value, bool))
    if priced <= 0 or priced != inventory.get("priced_cells"):
        errors.append("原图有价单元格数与矩阵不一致（包含未支持柜型）")
    if inventory.get("carrier") and cache.get("carrier") != inventory["carrier"]:
        errors.append("原图已识别船司不能因主数据未命中被清空")
    if sailing and (not cache.get("vessel") or cache.get("vessel") != inventory.get("vessel")):
        errors.append("原图船名航次未完整结构化")
    for row in rows:
        cells = row.get("cells") or []
        pods = [str(cell.get("pod") or "") for cell in cells]
        if any(not pod for pod in pods) or len(set(pods)) != len(pods):
            errors.append("目的港行缺失或重复，不能用重复行补齐数量")
    return list(dict.fromkeys(errors))
