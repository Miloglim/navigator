"""Register unresolved September backfill source messages for operator review."""
import argparse
import json
import re
from pathlib import Path

import ledger_client
import paths as P
from record_builder import compose_text


ARCHIVE = Path(P.WORK) / "backfill_20260915_20261007_readonly" / "runtime_v2" / ".work"
EXPECTED_ISSUES = 270
MESSAGE_ID = re.compile(r"`(msg[^`]+)`")


def report_reasons():
    reasons = {}
    report_files = sorted(ARCHIVE.glob("backfill-blockers*.md"))
    detail_files = sorted(path for folder in ("blocked_1007_A", "blocked_1007_B",
                                               "llm_out_1007_A", "llm_out_1007_B")
                          for path in (ARCHIVE / folder).glob("*.md"))
    for path in report_files + detail_files:
        for line in path.read_text(encoding="utf-8").splitlines():
            match = MESSAGE_ID.search(line)
            if not match:
                continue
            if line.startswith("|"):
                parts = line.split("|", 3)
                reason = parts[2].strip() if len(parts) > 2 else ""
            elif line.startswith("- "):
                reason = line[match.end():].lstrip("：: ")
            else:
                continue
            if reason and reason not in ("阻塞原因", "---"):
                reasons[match.group(1)] = reason
    return reasons


def unresolved_issues():
    known_reasons = report_reasons()
    issues = []
    for shard in ("A", "B"):
        state = json.loads((ARCHIVE / f"run_1007_{shard}" / "state.json").read_text(encoding="utf-8"))
        eligible = set(state["new_ids"]) - set(state.get("publisher_excluded_ids") or [])
        messages = {}
        for path in (ARCHIVE / f"msgs_1007_{shard}").glob("*.json"):
            for message in json.loads(path.read_text(encoding="utf-8")):
                messages[message["id"]] = dict(message, _slug=path.stem)
        decisions = set()
        for path in (ARCHIVE / f"llm_out_1007_{shard}").glob("*.json"):
            result = json.loads(path.read_text(encoding="utf-8"))
            decisions.update(item["msg_id"] for item in (result if isinstance(result, list) else [result])
                             if isinstance(item, dict) and item.get("msg_id"))
        quarantined = set()
        for path in (ARCHIVE / f"blocked_1007_{shard}").glob("*.json"):
            result = json.loads(path.read_text(encoding="utf-8"))
            quarantined.update(item["msg_id"] for item in (result if isinstance(result, list) else [result])
                               if isinstance(item, dict) and item.get("msg_id"))
        if not eligible <= messages.keys():
            raise ValueError(f"{shard}: source messages missing: {sorted(eligible - messages.keys())}")
        for mid in sorted(eligible):
            source = messages[mid]
            has_image = bool(source.get("img"))
            if not has_image and mid in decisions:
                continue
            reason = ("原图尚未逐图识读，不能从文字推断报价" if has_image else
                      known_reasons.get(mid) or ("已有候选因核查未通过被隔离，需对照原文逐项核实" if mid in quarantined
                                                 else "历史补采未形成可提交判定，需核对原文和业务含义"))
            issues.append({"source_group": source["_slug"], "message_id": mid,
                           "message_text": compose_text(source), "media_ids": source.get("img") or [],
                           "reasons": [reason]})
    if len(issues) != EXPECTED_ISSUES:
        raise ValueError(f"backfill changed: expected {EXPECTED_ISSUES}, got {len(issues)}")
    return issues


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    issues = unresolved_issues()
    if args.dry_run:
        print(json.dumps({"ok": True, "data": {"total": len(issues),
              "with_images": sum(bool(issue["media_ids"]) for issue in issues)}}, ensure_ascii=False))
        return
    for offset in range(0, len(issues), ledger_client.REVIEW_ISSUES_BATCH_LIMIT):
        batch = issues[offset:offset + ledger_client.REVIEW_ISSUES_BATCH_LIMIT]
        result = ledger_client.report_review_issues(batch)
        if not result.get("ok") or result.get("data", {}).get("count") != len(batch):
            raise RuntimeError(f"issue batch at {offset} not stored: {result.get('error', result)}")
    print(json.dumps({"ok": True, "data": {"submitted": len(issues)}}, ensure_ascii=False))


if __name__ == "__main__":
    main()
