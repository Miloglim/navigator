# -*- coding: utf-8 -*-
"""Read-only, independent pending-ledger evidence for pull-health sidecar."""
import json
import pathlib
import sys
import argparse

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "Database"))
import postgresdb
from services.submission_coverage import fingerprint, assess_receipt


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--receipts-dir')
    args = parser.parse_args()
    db = postgresdb.connect(pathlib.Path(r"C:\ProgramData\FreightRate\freight-postgres.env"))
    try:
        db._conn.execute("SET TRANSACTION READ ONLY")
        rows = db.execute("SELECT kind, payload, entry_id, edit_log FROM pending_records").fetchall()
        counts = {}
        entries = {}
        malformed = 0
        for row in rows:
            payload = row[1]
            try:
                obj = json.loads(payload) if isinstance(payload, str) else payload
            except (ValueError, TypeError):
                malformed += 1
                continue
            if not isinstance(obj, dict):
                continue
            msg_id = obj.get("消息ID") or obj.get("message_id")
            if msg_id:
                counts[str(msg_id)] = counts.get(str(msg_id), 0) + 1
            edits = json.loads(row[3] or '[]')
            entries[row[2]] = {'fingerprint':fingerprint(row[0], obj),'message_id':str(msg_id or ''),
                               'edited':any(e.get('action') in ('update','approve_diff') for e in edits)}
        receipt_issues = []
        if args.receipts_dir:
            for path in pathlib.Path(args.receipts_dir).glob('*.json'):
                try:
                    receipt = json.loads(path.read_text(encoding='utf-8'))
                    found = assess_receipt(receipt, entries, counts)
                    if found:
                        receipt_issues.append({'file':path.name,'issues':found})
                except (ValueError, KeyError, TypeError, OSError) as error:
                    receipt_issues.append({'file':path.name,'issues':[{'reason':'receipt_unreadable','detail':str(error)}]})
        print(json.dumps({"ok": True, "data": {"source_counts": counts,
                            "receipt_issues":receipt_issues,"pending_rows": len(rows), "malformed": malformed}}, ensure_ascii=False))
    finally:
        db.rollback()
        db.close()


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(json.dumps({"ok": False, "error": type(exc).__name__}, ensure_ascii=False))
        sys.exit(1)
