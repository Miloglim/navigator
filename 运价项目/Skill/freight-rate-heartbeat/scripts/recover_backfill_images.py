"""Recover only the authorized image-message scope; never change watermarks."""
import argparse
import json
import os
import re
import shutil
from decimal import Decimal, InvalidOperation
from pathlib import Path
from urllib.parse import urlencode

import check_llm_out
import ledger_client
import paths as P
import pipeline
import record_builder
import spec_index
from freight_images.services.completeness import matrix_errors

ARCHIVE = Path(P.WORK) / "backfill_20260915_20261007_readonly" / "runtime_v2" / ".work"
EXPECTED_MESSAGES = 129
PARENT_VERIFIED_MESSAGES = frozenset({"msg//3m+IiNv8eSgFg5cVFC9A==", "msgJQX2TDncr5puVG+e0DLhdA==", "msgc7B2HgMLn7wVN1BBpGlKPA==", "msgru7J5fx8weztTB9ZEcM5fA=="})
RECEIPT_NAME = "image_recovery_receipt.json"
RECEIPT_FIELDS = ("起运港", "目的港", "船司", "柜型", "海运费USD", "价格USD", "有效期起", "有效期止", "船期ETD", "船名航次", "舱位类型", "航线服务", "目的码头", "箱量", "目免", "亏舱费", "T/T航程", "备注", "图片名", "来源群ID", "发送人ID", "发送人OpenID")
INLAND_MARKER = re.compile(r"by[\s-]*rail|rail\s*way|inland|铁路|内陆|\bICD\b|\bCY\b", re.I)


def receipt_signature(kind, record):
    record = dict(record)
    note = str(record.get("备注") or "").strip()
    for field in ("起运港", "目的港"):
        port = str(record.get(field) or "").strip()
        if INLAND_MARKER.search(port):
            tag = "内陆铁路点(by rail)：" + port
            if tag not in note:
                note += ("；" if note else "") + tag
    record["备注"] = note
    values = []
    for key in RECEIPT_FIELDS:
        value = str(record.get(key) or "").strip()
        if key in ("海运费USD", "价格USD") and value:
            try:
                value = str(Decimal(value).normalize())
            except InvalidOperation:
                pass  # Non-numeric prices remain literal and fail authoritative validation.
        values.append(value)
    return (kind, *values)


def source_scope():
    scope = []
    for shard in ("A", "B"):
        state = json.loads((ARCHIVE / f"run_1007_{shard}" / "state.json").read_text(encoding="utf-8"))
        wanted = set(state["new_ids"]) - set(state.get("publisher_excluded_ids") or [])
        found = set()
        for path in sorted((ARCHIVE / f"msgs_1007_{shard}").glob("*.json")):
            for source in json.loads(path.read_text(encoding="utf-8")):
                if source["id"] in wanted:
                    found.add(source["id"])
                    if source.get("img"):
                        scope.append(dict(source, _slug=path.stem, _shard=shard))
        if found != wanted:
            raise ValueError("current backfill sources incomplete")
    if len(scope) != EXPECTED_MESSAGES or len({(s["_slug"], s["id"]) for s in scope}) != len(scope):
        raise ValueError("authorized image scope changed")
    return scope


def load_cache(media_id):
    path = ARCHIVE / "ocr_cache" / (media_id.lstrip("@") + ".json")
    if not path.exists():
        raise ValueError("识图尚未完成：%s" % media_id)
    cache = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(cache, dict):
        raise ValueError("OCR cache must be an object")
    return path, cache


def decision_for(source):
    matches = []
    for path in (ARCHIVE / ("llm_out_1007_" + source["_shard"])).glob("*.json"):
        value = json.loads(path.read_text(encoding="utf-8"))
        for item in (value if isinstance(value, list) else [value]):
            if isinstance(item, dict) and item.get("msg_id") == source["id"]:
                matches.append(item)
    if len(matches) > 1:
        raise ValueError("conflicting or repeated source decisions")
    if not matches:
        return None
    decision = matches[0]
    if decision.get("group") != source["_slug"]:
        raise ValueError("decision group does not match source")
    valid, why = spec_index.resolve_basis(source["_slug"], decision.get("rule_basis") or "")
    if not valid:
        raise ValueError("decision rule basis invalid: " + why)
    if decision.get("decision") not in check_llm_out.VALID_DECISION:
        raise ValueError("decision kind invalid")
    return decision


def cache_inventory_errors(cache):
    errors = []
    rows = cache.get("rows") or []
    raw_rows = cache.get("raw_source_rows")
    if raw_rows is not None:
        if (not isinstance(raw_rows, list) or not raw_rows
                or any(not isinstance(row, dict) or not isinstance(row.get("prices"), list)
                       or any(type(value) not in (int, float) for value in row["prices"]) for row in raw_rows)):
            return ["原图逐行金额清单格式无效"]
        if cache.get("source_rows") != len(raw_rows):
            errors.append("原图逐行清单与行数不一致")
        if cache.get("source_priced_cells") != sum(len(row["prices"]) for row in raw_rows):
            errors.append("原图逐行清单与金额数量不一致")
        extracted = {value for row in rows for key, value in row.items()
                     if key.upper().replace("'", "") in ("20GP", "20DC", "40GP", "40DC", "40HQ", "40HC", "40HQ/HC", "40NOR", "45HQ", "45HC")
                     and type(value) in (int, float)}
        if cache.get("type") in ("price_card", "formula_card") and extracted != {value for row in raw_rows for value in row["prices"]}:
            errors.append("原图逐行清单与展开金额不一致")
        return errors
    if cache.get("source_rows") is not None and cache["source_rows"] != len(rows):
        errors.append("原图行数与OCR不一致")
    if cache.get("source_priced_cells") is not None and cache.get("type") in ("price_card", "formula_card"):
        grid = cache.get("raw_price_grid")
        if grid is not None:
            if not isinstance(grid, list) or len(grid) != len(rows) or any(not isinstance(row, list) for row in grid):
                return errors + ["原图金额网格格式不一致"]
            cells = sum(type(value) in (int, float) for row in grid for value in row)
            for row, source_prices in zip(rows, grid):
                extracted = [value for key,value in row.items() if key in ("20GP","40GP","40HQ","40HC","40HQ/HC","40NOR")]
                if set(source_prices) != set(extracted):
                    errors.append("原图金额网格与规范行不一致")
        else:
            cells = sum(1 for row in rows for key, value in row.items()
                    if key.upper().replace("'", "") in ("20GP", "20DC", "40GP", "40DC", "40HQ", "40HC", "40HQ/HC", "40NOR", "45HQ", "45HC")
                    and type(value) in (int, float))
        if cells != cache["source_priced_cells"]:
            errors.append("原图有价单元格数与OCR不一致")
    return errors


def candidate_records(source):
    caches = [load_cache(mid) for mid in source["img"]]
    for _, cache in caches:
        errors = matrix_errors(cache, source["_slug"]) + cache_inventory_errors(cache)
        if errors:
            raise ValueError("；".join(errors))
    decision = decision_for(source)
    if decision and decision["decision"] == "noise":
        raise ValueError("已识图判为非报价，需记录可核实的非报价结案依据，不生成空报价")
    candidates = []
    if decision and decision["decision"] == "records":
        records = decision.get("records")
        if not isinstance(records, list) or not records:
            raise ValueError("records decision has no records")
        for record in records:
            if not isinstance(record, dict) or record.get("_table") not in check_llm_out.VALID_TABLE:
                raise ValueError("record table invalid")
            if check_llm_out.FORBID_KEYS & record.keys():
                raise ValueError("decision overrides source provenance/date fields")
            missing = check_llm_out._incomplete_priced_rate(source, record, [cache for _, cache in caches]) + check_llm_out._incomplete_priced_space(source, record)
            if missing:
                raise ValueError("source quote extraction incomplete: " + "/".join(missing))
            fields = {key: value for key, value in record.items() if key != "_table"}
            if not fields.get("起运港"):
                fields["起运港"] = record_builder.GMAP[source["_slug"]]["default_pol"]
            vf, vt, etd, ai = record_builder.vdates(fields.get("有效期船期") or "", source["t"], source["_slug"])
            fields.update({key:value for key,value in (("有效期起",vf),("有效期止",vt),("船期ETD",etd)) if value})
            if ai:
                fields["AI判断"] = "是"
            candidates.append(record_builder.base_cells(record["_table"], source, fields, decision.get("confidence") or "med"))
    if not decision or decision.get("replace_template") is not True:
        for _, cache in caches:
            if record_builder._quoted_image_needs_review(source, cache):
                raise ValueError("引用图片与当前回复语义尚未确认，不能把旧报价直接当作新报价")
            generator = record_builder.CACHE_TPL.get(cache.get("type"))
            if not generator:
                if candidates:
                    continue
                raise ValueError("OCR已读但尚无完整可执行判定：%s" % cache.get("type"))
            rows = generator(source, cache)
            if not rows:
                raise ValueError("OCR已读但表格生成器没有完整候选")
            candidates.extend(rows)
    result = []
    for candidate in candidates:
        kind = "rates" if candidate["_meta"]["table"] == "rate" else "space"
        base = dict(candidate["record"], 图片名=",".join(source["img"]))
        if kind == "rates":
            base = pipeline._normalize_pod(base)
        expanded = pipeline._expand_combo_rate(base) if kind == "rates" else [base]
        expanded = [part for record in expanded for part in pipeline._split_pol(record)]
        expanded = [part for record in expanded for part in pipeline._split_pod(record)]
        for record in expanded:
            result.append({"kind":kind,"record":pipeline._coerce(record)})
    unique = {}
    for item in result:
        signature = pipeline._record_sig(item["kind"], item["record"])
        unique[signature] = item
    if not unique:
        raise ValueError("source produced no candidate records")
    missing_pairs = check_llm_out._missing_explicit_pairs(source, [dict(x["record"], _table="rate" if x["kind"] == "rates" else "space") for x in unique.values()])
    if missing_pairs:
        raise ValueError("正文明确价格/柜型配对遗漏：%s" % missing_pairs)
    return list(unique.values()), caches


def save_receipt(receipt):
    path = ARCHIVE / RECEIPT_NAME
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(receipt, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temporary, path)


def recover(source, dry_run):
    owner = "parent" if source["id"] in PARENT_VERIFIED_MESSAGES else source["_shard"]
    receipt_path = ARCHIVE / ("image_ocr_" + owner + "_receipt.json")
    progress = json.loads(receipt_path.read_text(encoding="utf-8"))
    verified = next((item for item in progress.get("messages", []) if item.get("message_id") == source["id"]), None)
    if (not verified or verified.get("read") is not True
            or not str(verified.get("status") or "").startswith("complete")
            or set(verified.get("media_ids") or []) != set(source["img"])):
        raise ValueError("Agent正在完成本消息识读与业务判定，不能使用未完成的临时缓存入库")
    decision = decision_for(source)
    if decision and decision["decision"] == "noise":
        caches = [load_cache(mid) for mid in source["img"]]
        reason = str(decision.get("note") or "").strip()
        if not reason or decision.get("records"):
            raise ValueError("non-quote decision requires an explicit reason and no records")
        if any(matrix_errors(cache, source["_slug"]) + cache_inventory_errors(cache) for _, cache in caches):
            raise ValueError("non-quote image inventory incomplete")
        if not dry_run:
            settled = ledger_client._request("POST", "/api/agent/review-issues/settle", {
                "source_group":source["_slug"],"message_id":source["id"],
                "classification":"non_quote","reason":reason,"media_ids":source["img"],
                "pending_ids":[],"expected_count":0})
            if not settled.get("ok"):
                raise RuntimeError("non-quote settlement failed: %s" % settled.get("error"))
        return {"message_id":source["id"],"group":source["_slug"],"status":"non_quote",
                "reason":reason,"candidate_count":0,"issue_settled":not dry_run}
    rows, caches = candidate_records(source)
    validation_errors, expected = [], len(rows)
    expected_receipts = set()
    for kind in ("rates", "space"):
        records = [item["record"] for item in rows if item["kind"] == kind]
        for batch in ledger_client.chunks(records):
            response = ledger_client.validate(kind, batch)
            if not response.get("ok") or len(response.get("results") or []) != len(batch):
                raise RuntimeError("authoritative validation failed: %s" % response.get("error"))
            for record, result in zip(batch, response["results"]):
                normalized = dict(record)
                normalized.update(result.get("resolved") or {})
                expected_receipts.add(receipt_signature(kind, normalized))
                if not result.get("ok"):
                    validation_errors.append({"pol":record.get("起运港"),"pod":record.get("目的港"),
                                              "errors":result.get("errors") or []})
    raw_candidates = expected
    expected = len(expected_receipts)
    receipt = {"message_id":source["id"],"group":source["_slug"],"candidate_count":expected,"extracted_count":raw_candidates,
               "check_failed":len(validation_errors),"validation_errors":validation_errors}
    if dry_run:
        return dict(receipt, status="validated")
    pending_ids = set()
    for kind in ("rates", "space"):
        records = [item["record"] for item in rows if item["kind"] == kind]
        for batch in ledger_client.chunks(records):
            response = ledger_client.submit_missing(kind, batch)
            if not response.get("ok") or not (response.get("coverage") or {}).get("ok"):
                coverage = response.get("coverage") or {}
                raise RuntimeError("pending submission/coverage failed: %s; expected=%s actual=%s missing=%s" % (
                    response.get("error") or "incomplete source receipt", coverage.get("expected_unique"),
                    coverage.get("actual_unique"), len(coverage.get("missing") or [])))
            pending_ids.update(response.get("covered_ids") or [])
    readback = ledger_client._request("GET", "/api/pending?" + urlencode({
        "id_query":source["id"],"status":"all","limit":500,"offset":0}))
    if not readback.get("ok"):
        raise RuntimeError("pending readback failed")
    actual = list(readback.get("rows") or [])
    offset = len(actual)
    total = int(readback.get("total") or 0)
    while offset < total:
        page = ledger_client._request("GET", "/api/pending?" + urlencode({
            "id_query":source["id"],"status":"all","limit":500,"offset":offset}))
        if not page.get("ok") or int(page.get("total") or 0) != total or not page.get("rows"):
            raise RuntimeError("pending pagination drift")
        actual.extend(page["rows"])
        offset = len(actual)
    staged = [row for row in actual if row["id"] in pending_ids]
    if len(staged) != len(pending_ids) or len(pending_ids) != expected:
        raise RuntimeError("pending source count differs from expected complete quote inventory")
    if any(row["payload"].get("消息原文") != record_builder.compose_text(source)
           or row["payload"].get("消息ID") != source["id"] for row in staged):
        raise RuntimeError("pending source evidence changed")
    if {receipt_signature(row["kind"], dict(row["payload"], **(row.get("resolved") or {}))) for row in staged} != expected_receipts:
        raise RuntimeError("pending prices, dates, product dimensions or publisher evidence differ from complete extraction")
    settled = ledger_client.settle_review_issue(source["_slug"], source["id"], sorted(pending_ids))
    if not settled.get("ok") or not settled.get("data", {}).get("resolved"):
        raise RuntimeError("quotes staged but issue settlement failed: %s" % settled.get("error"))
    shared = Path(P.OCR_CACHE)
    shared.mkdir(parents=True, exist_ok=True)
    for path, _ in caches:
        shutil.copy2(path, shared / path.name)
    return dict(receipt, status="staged", pending_ids=sorted(pending_ids),
                check_failed=sum(row["check_status"] != "passed" for row in staged), issue_settled=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--group", action="append", default=[])
    parser.add_argument("--message-id", action="append", default=[])
    args = parser.parse_args()
    scope = source_scope()
    selected = [source for source in scope if (not args.group or source["_slug"] in args.group)
                and (not args.message_id or source["id"] in args.message_id)]
    if not selected:
        raise ValueError("selected source scope is empty")
    receipt_path = ARCHIVE / RECEIPT_NAME
    receipt = json.loads(receipt_path.read_text(encoding="utf-8")) if receipt_path.exists() else {}
    for source in selected:
        try:
            result = recover(source, args.dry_run)
        except (ValueError, RuntimeError, OSError, TypeError) as error:
            result = {"message_id":source["id"],"group":source["_slug"],"status":"blocked","reason":str(error)}
        display = {key:value for key,value in result.items() if key not in ("validation_errors", "pending_ids")}
        if result.get("pending_ids"):
            display["pending_count"] = len(result["pending_ids"])
        display["error_summary"] = sorted({"%s:%s:%s" % (error.get("field"),error.get("input"),error.get("msg"))
                                            for row in result.get("validation_errors") or [] for error in row["errors"]})
        print(json.dumps(display, ensure_ascii=True), flush=True)
        if not args.dry_run:
            receipt[source["id"]] = result
            save_receipt(receipt)
    counts = {status:sum(row.get("status") == status for row in receipt.values()) for status in ("staged","non_quote","blocked")}
    print(json.dumps({"authorized_messages":len(scope),"selected":len(selected),"saved_results":counts}, ensure_ascii=True))


if __name__ == "__main__":
    main()
