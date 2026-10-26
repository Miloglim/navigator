"""Archive only this run's untouched, superseded image candidates; keep evidence."""
import argparse
import json
import sys
from collections import Counter
from pathlib import Path

import ledger_client
import recover_backfill_images as recovery

ALLOWED_MESSAGES = frozenset({"msg41MMPoycSO7uc+AIjpwaCQ==", "msgEBuCYzMTW3rmbuRtjReVDw=="})
RUN_STARTED_AT = "2026-10-08 10:38:00"


def expected_signatures(source):
    rows, _ = recovery.candidate_records(source)
    signatures = set()
    for kind in ("rates", "space"):
        records = [row["record"] for row in rows if row["kind"] == kind]
        for batch in ledger_client.chunks(records):
            result = ledger_client.validate(kind, batch)
            if not result.get("ok") or len(result.get("results") or []) != len(batch):
                raise ValueError("authoritative validation unavailable")
            for record, checked in zip(batch, result["results"]):
                signatures.add(recovery.receipt_signature(kind, dict(record, **(checked.get("resolved") or {}))))
    return signatures


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--blocked-sources", action="store_true")
    parser.add_argument("--refresh-validation", action="store_true")
    parser.add_argument("--audit", action="store_true")
    args = parser.parse_args()
    sys.path.insert(0, str(Path(ledger_client.images_dir()).parent))
    import manualdb
    import postgresdb

    conn = postgresdb.connect()
    try:
        if args.audit:
            audit_scope(conn)
            return
        if args.refresh_validation:
            refresh_validation(conn, args.apply)
            return
        allowed = set(ALLOWED_MESSAGES)
        if args.blocked_sources:
            receipt = json.loads((recovery.ARCHIVE / recovery.RECEIPT_NAME).read_text(encoding="utf-8"))
            allowed.update(mid for mid, row in receipt.items() if row.get("status") == "blocked"
                           and ("settlement" in row.get("reason", "") or "differ from complete extraction" in row.get("reason", "")))
        for source in recovery.source_scope():
            if source["id"] not in allowed:
                continue
            receipt = json.loads((recovery.ARCHIVE / ("image_ocr_" + source["_shard"] + "_receipt.json")).read_text(encoding="utf-8"))
            verified = next(row for row in receipt["messages"] if row["message_id"] == source["id"])
            if not verified.get("read") or not verified.get("status", "").startswith("complete"):
                raise ValueError("final source reading not complete")
            expected = expected_signatures(source)
            rows = conn.execute("SELECT * FROM pending_records WHERE json_extract(payload,'$.消息ID')=? AND review_status IN ('awaiting','approved') ORDER BY id", (source["id"],)).fetchall()
            seen, obsolete = set(), []
            checked_signatures = {}
            for kind in ("rates", "space"):
                typed = [row for row in rows if row["kind"] == kind]
                for batch in ledger_client.chunks(typed):
                    result = ledger_client.validate(kind, [json.loads(row["payload"]) for row in batch])
                    if not result.get("ok") or len(result.get("results") or []) != len(batch):
                        raise ValueError("existing candidates cannot be validated")
                    for row, checked in zip(batch, result["results"]):
                        payload = json.loads(row["payload"])
                        checked_signatures[row["id"]] = recovery.receipt_signature(kind, dict(payload, **(checked.get("resolved") or {})))
            for row in rows:
                payload = json.loads(row["payload"])
                signature = checked_signatures[row["id"]]
                if signature in expected and signature not in seen:
                    seen.add(signature)
                    continue
                if (row["review_status"] != "awaiting" or row["origin"] != "agent"
                        or row["created_at"] < RUN_STARTED_AT or json.loads(row["edit_log"] or "[]")
                        or payload.get("消息原文") != recovery.record_builder.compose_text(source)
                        or payload.get("消息ID") != source["id"]):
                    raise ValueError("protected historical, approved or edited row: %s" % row["id"])
                obsolete.append(dict(row))
            print(json.dumps({"message_id":source["id"], "expected":len(expected), "retained":len(seen),
                              "archive":len(obsolete), "apply":args.apply}, ensure_ascii=True), flush=True)
            if args.apply and obsolete:
                snapshot = recovery.ARCHIVE / ("superseded_" + source["id"].replace("/", "_") + ".json")
                if snapshot.exists():
                    raise ValueError("archive already exists; inspect it instead of overwriting")
                snapshot.write_text(json.dumps(obsolete, ensure_ascii=False, indent=2), encoding="utf-8")
                for kind, count in Counter(row["kind"] for row in obsolete).items():
                    ids = [row["id"] for row in obsolete if row["kind"] == kind]
                    result = manualdb.reject_pending_ids(conn, ids, kind,
                        note="本轮图片逐图复核：旧候选或重复候选已被完整提取替代；原证据保留于驳回日志及恢复快照",
                        reviewed_by="image-recovery-repair")
                    if len(result["rejected"]) != count or result["skipped"]:
                        raise ValueError("archive receipt incomplete")
    finally:
        conn.close()


def refresh_validation(conn, apply):
    """Recompute derived checks for verified master repairs, never change payloads."""
    requests = []
    for path in recovery.ARCHIVE.glob("master_repairs_images*.json"):
        if "receipt" not in path.name:
            value = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(value, list):
                requests.extend(value)
    extra_b = recovery.ARCHIVE / "extra_B" / "ensure_requests.json"
    if extra_b.exists():
        requests.extend(json.loads(extra_b.read_text(encoding="utf-8"))["requests"])
    groups = {request["source_group"] for request in requests}
    scope = {source["id"]:source for source in recovery.source_scope()}
    master = manual_master(conn)
    changes = []
    import manualdb
    for row in conn.execute("SELECT * FROM pending_records WHERE origin='agent' AND review_status='awaiting' ORDER BY id").fetchall():
        payload = json.loads(row["payload"])
        source = scope.get(payload.get("消息ID"))
        if (not source or payload.get("来源群") not in groups or row["created_at"] < "2026-10-08 10:09:00"
                or json.loads(row["edit_log"] or "[]")
                or payload.get("消息原文") != recovery.record_builder.compose_text(source)):
            continue
        checked = manualdb.validate_record(conn, master, row["kind"], dict(payload))
        status = "passed" if checked["ok"] else "failed"
        if (status == row["check_status"] and checked["resolved"] == json.loads(row["resolved"] or "{}")
                and checked["errors"] == json.loads(row["errors"] or "[]")):
            continue
        changes.append({"id":row["id"], "old":dict(row), "checked":checked})
    print(json.dumps({"refresh_validation":len(changes), "apply":apply}, ensure_ascii=True), flush=True)
    if apply and changes:
        path = recovery.ARCHIVE / "master_validation_refresh_receipt.json"
        previous = json.loads(path.read_text(encoding="utf-8")) if path.exists() else []
        path.write_text(json.dumps(previous + changes, ensure_ascii=False, indent=2), encoding="utf-8")
        for change in changes:
            old, checked = change["old"], change["checked"]
            changed = conn.execute("UPDATE pending_records SET resolved=?,check_status=?,errors=?,warnings=? WHERE id=? AND review_status='awaiting' AND payload=? AND edit_log IS NOT DISTINCT FROM ?",
                (json.dumps(checked["resolved"],ensure_ascii=False),"passed" if checked["ok"] else "failed",
                 json.dumps(checked["errors"],ensure_ascii=False),json.dumps(checked.get("warnings") or [],ensure_ascii=False),
                 old["id"],old["payload"],old["edit_log"]))
            if changed.rowcount != 1:
                conn.rollback()
                raise ValueError("row changed during derived validation refresh")
        conn.commit()


def manual_master(conn):
    import manualdb
    manualdb.invalidate_master()
    return manualdb.get_master(conn)


def audit_scope(conn):
    import manualdb
    receipts = json.loads((recovery.ARCHIVE / recovery.RECEIPT_NAME).read_text(encoding="utf-8"))
    master = manual_master(conn)
    results, errors = [], []
    for source in recovery.source_scope():
        mid = source["id"]
        owner = "parent" if mid in recovery.PARENT_VERIFIED_MESSAGES else source["_shard"]
        progress = json.loads((recovery.ARCHIVE / ("image_ocr_" + owner + "_receipt.json")).read_text(encoding="utf-8"))
        reading = next((row for row in progress["messages"] if row["message_id"] == mid), {})
        if (reading.get("read") is not True or not reading.get("status", "").startswith("complete")
                or set(reading.get("media_ids") or []) != set(source["img"])):
            errors.append({"message_id":mid,"error":"source reading incomplete"})
        decision = recovery.decision_for(source)
        expected = set()
        if not decision or decision.get("decision") != "noise":
            candidates, _ = recovery.candidate_records(source)
            for candidate in candidates:
                record = dict(candidate["record"])
                checked = manualdb.validate_record(conn, master, candidate["kind"], record)
                expected.add(recovery.receipt_signature(candidate["kind"], dict(record, **checked["resolved"])))
        actual = conn.execute("SELECT * FROM pending_records WHERE json_extract(payload,'$.消息ID')=? AND review_status IN ('awaiting','approved') ORDER BY id", (mid,)).fetchall()
        found, failures = set(), []
        for row in actual:
            payload = json.loads(row["payload"])
            checked = manualdb.validate_record(conn, master, row["kind"], dict(payload))
            found.add(recovery.receipt_signature(row["kind"], dict(payload, **checked["resolved"])))
            if (payload.get("消息原文") != recovery.record_builder.compose_text(source)
                    or payload.get("消息ID") != mid
                    or set(str(payload.get("图片名") or "").split(",")) != set(source["img"])):
                errors.append({"message_id":mid,"id":row["id"],"error":"original evidence mismatch"})
            if row["check_status"] != ("passed" if checked["ok"] else "failed"):
                errors.append({"message_id":mid,"id":row["id"],"error":"derived validation stale"})
            if not checked["ok"]:
                failures.append({"id":row["id"],"entry_id":row["entry_id"],"errors":checked["errors"]})
        if found != expected or len(actual) != len(expected):
            errors.append({"message_id":mid,"error":"source quote manifest mismatch","expected":len(expected),"actual":len(actual),"missing":len(expected-found),"extra":len(found-expected)})
        if set(receipts[mid].get("pending_ids") or []) != {row["id"] for row in actual}:
            errors.append({"message_id":mid,"error":"saved source receipt differs from database"})
        issue = conn.execute("SELECT status FROM review_issues WHERE source_group=? AND message_id=?", (source["_slug"],mid)).fetchone()
        if not issue or issue["status"] != "resolved":
            errors.append({"message_id":mid,"error":"obsolete image issue not settled"})
        receipts[mid]["check_failed"] = len(failures)
        results.append({"message_id":mid,"group":source["_slug"],"status":receipts[mid]["status"],
                        "quote_count":len(actual),"passed":len(actual)-len(failures),"failed":len(failures),"failures":failures})
    report = {"ok":not errors,"sources":len(results),"quotes":sum(row["quote_count"] for row in results),
              "passed":sum(row["passed"] for row in results),"failed":sum(row["failed"] for row in results),
              "non_quote":sum(row["status"] == "non_quote" for row in results),"errors":errors,"results":results}
    (recovery.ARCHIVE / "image_final_audit.json").write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding="utf-8")
    if not errors:
        recovery.save_receipt(receipts)
    lines = ["# 图片恢复验收", "", "129条图片消息逐图识读完成；逐源核对最终判定、原文、图片、完整报价维度、数据库回执及旧问题结案。",
             "", "| 来源消息 | 待审报价 | 校验通过 | 校验失败 | 非报价 | 验收错误 |", "|---:|---:|---:|---:|---:|---:|",
             "| %s | %s | %s | %s | %s | %s |" % (report["sources"],report["quotes"],report["passed"],report["failed"],report["non_quote"],len(errors)),
             "", "失败报价已在航管待审核中显示，不是未读图片。未发布正式报价、未推进水位。", "", "| 群 | 消息ID（待审核可搜索） | 报价数 | 通过 | 失败 | 具体原因 |", "|---|---|---:|---:|---:|---|"]
    for row in results:
        reason_examples = {}
        for failure in row["failures"]:
            for error in failure["errors"]:
                reason_examples.setdefault(error["msg"], failure["entry_id"])
        reasons = [reason + "（示例条目：`" + entry_id + "`）" for reason,entry_id in sorted(reason_examples.items())]
        lines.append("| %s | `%s` | %s | %s | %s | %s |" % (row["group"],row["message_id"],row["quote_count"],row["passed"],row["failed"],"；".join(reasons).replace("|","/") or row["status"]))
    (recovery.ARCHIVE / "图片恢复验收.md").write_text("\n".join(lines)+"\n",encoding="utf-8")
    print(json.dumps({key:value for key,value in report.items() if key != "results"},ensure_ascii=True),flush=True)
    if errors:
        raise ValueError("final image audit failed; see source-specific errors")


if __name__ == "__main__":
    main()
