from freight_flow.core.contracts import STAGES, ERROR_SAMPLE_LIMIT
import re


def source_row_key(row):
    value = str(row.get('_source_row', row.get('source_row', '')) or '').strip()
    if value and not re.fullmatch(r'(?:bill:[A-Za-z0-9][A-Za-z0-9/_-]*|image:[1-9][0-9]*:row:[1-9][0-9]*|text:[1-9][0-9]*)', value):
        raise ValueError('来源行标识不符合证据定位契约')
    return value


def plan(source):
    if any(not isinstance(source.get(k), str) or not source[k]
           for k in ("message_id", "text", "group_knowledge")):
        raise ValueError("需要 message_id、text、group_knowledge")
    return [s for s in STAGES if s != "vision" or source.get("image_path")]


def coverage(records, inventory):
    """Compare each evidence cell, not a set of price numbers."""
    def container(value):
        aliases = {"40HQ": "40HQ/HC", "40HC": "40HQ/HC", "45HQ": "45HQ/HC", "45HC": "45HQ/HC"}
        return aliases.get(value.upper().strip(), value.upper().strip())
    def dimensions(row):
        values = []
        for key in ("起运港", "ETD", "有效期开始", "有效期截止", "service", "tag", "船名航次", "目的码头", "箱量"):
            value = str(row.get(key, "")).strip()
            if key == "船名航次":
                value = re.sub(r"\s*/\s*", " ", value)
                value = re.sub(r"\s+", " ", value)
            values.append(value)
        return (*values, source_row_key(row))
    def is_product(row):
        return (str(row.get("section_role") or "product").strip().lower() == "product")
    def is_rate_product(row):
        return is_product(row) and str(row.get("_table") or "rate").lower() != "space"
    def has_base_price(row):
        value = row.get("海运费USD", row.get("价格USD", row.get("price", "")))
        if type(value) in (int, float):
            return value >= 0
        # A standalone surcharge or an explicit no-service marker is evidence,
        # not a base product price. This prevents phantom coverage failures.
        return bool(re.match(r'^\s*(?:USD\s*)?\d{2,7}(?:\b|[/.+(])', str(value or ""), re.I))
    product_records = [r for r in records if is_rate_product(r) and has_base_price(r)]
    product_inventory = [r for r in inventory if is_rate_product(r) and has_base_price(r)]
    cells = {(r.get("目的港", "").strip(), container(r.get("柜型", "")),
              str(r.get("海运费USD", r.get("价格USD", ""))), dimensions(r)) for r in product_records}
    if len(cells) != len(product_records):
        raise ValueError("候选存在同港同柜型同船期同价格的重复行")
    missing = [c for c in product_inventory if (c["pod"].strip(), container(c["container"]), str(c["price"]), dimensions(c)) not in cells]
    if missing:
        raise ValueError(f"明确报价格未覆盖 {len(missing)} 格，示例: " + repr(missing[:ERROR_SAMPLE_LIMIT]))
    if product_inventory and not product_records:
        raise ValueError("有明确报价却无候选")
    if product_records and not product_inventory:
        raise ValueError("候选有价但独立盘点没有任何报价格")
    if len(product_inventory) != len(product_records):
        raise ValueError("盘点报价格与候选行数不一致，不能用多余行凑覆盖")
    space_records = [r for r in records if is_product(r) and not is_rate_product(r)]
    # Inventory cells do not carry the source table. An empty price is a
    # space allocation; placeholders such as "见原图" remain rate cells.
    space_inventory = [r for r in inventory
                       if is_product(r) and not str(r.get("price", "")).strip()]
    space_cells = {(r.get("目的港", "").strip(), container(r.get("柜型", "")), dimensions(r))
                   for r in space_records}
    if len(space_cells) != len(space_records):
        raise ValueError("舱位候选存在重复行")
    missing_spaces = [cell for cell in space_inventory
                      if (cell.get("pod", "").strip(), container(cell.get("container", "")),
                          dimensions(cell)) not in space_cells]
    if space_inventory:
        if missing_spaces:
            raise ValueError(f"明确舱位未覆盖 {len(missing_spaces)} 格")
        if len(space_inventory) != len(space_records):
            raise ValueError("盘点舱位格与候选行数不一致")
    covered = product_records + space_records
    preserved_records = [r for r in records if is_product(r) and r not in covered]
    return {"ok": True, "priced_cells": len(product_inventory),
            "space_cells": len(space_inventory),
            "records": covered + preserved_records,
            "ignored_inventory_cells": len(inventory) - len(product_inventory) - len(space_inventory),
            "ignored_candidate_records": len(records) - len(covered) - len(preserved_records)}


def validate_schema(value, spec):
    """Validate the deliberately small supported JSON-schema subset."""
    kind = spec.get("type")
    if isinstance(kind, list):
        for k in kind:
            try:
                validate_schema(value, {**spec, "type": k})
                return value
            except ValueError:
                continue  # Try the next explicitly permitted scalar type.
        raise ValueError("JSON value type mismatch")
    correct = {"object": isinstance(value, dict), "array": isinstance(value, list),
               "string": isinstance(value, str), "integer": type(value) is int,
               "boolean": type(value) is bool}.get(kind, False)
    if not correct:
        raise ValueError("JSON value type mismatch: " + str(kind))
    if "enum" in spec and value not in spec["enum"]:
        raise ValueError("JSON enum mismatch")
    if kind == "object":
        properties = spec["properties"]
        if set(spec.get("required", ())) - value.keys() or (not spec.get("additionalProperties", True)
                                                          and value.keys() - properties.keys()):
            raise ValueError("JSON fields mismatch")
        for key, item in value.items():
            if key in properties:
                validate_schema(item, properties[key])
    if kind == "array":
        for item in value:
            validate_schema(item, spec["items"])
    return value
