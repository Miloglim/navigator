# -*- coding: utf-8 -*-
"""pending_review.py —— 待审队列审查（人工审核辅助 + 从错误里学）。只读，不写库不改文件。

人工侧要的不是"451 条列表"，而是三件事：先处理哪批、错误集中在哪、哪些能一次性补掉不再犯。
所以本脚本把队列压成清单，并把每条错误映射到一个**可执行动作**（补主数据 / 登记别名 / 改规范 / 改契约）。

"原记录 → 优化记录 → 优化后正确率"怎么算：
  台账存了 `orig_payload`（人工改前）与 `payload`（改后），正确率**不自己另立判据**，
  而是把两份各自丢回 `/api/validate`（预检、不写库、与放行时同一套闸门）重放，
  改前通过率 vs 改后通过率 = 优化带来的提升。只对"人工真改过"的行跑，量小、成本可控。
  服务没起或一行都没改过时，这一维度会明确写"数据不足"，不会编数字。

    python ops/pending_review.py                 人读报告
    python ops/pending_review.py --json
    python ops/pending_review.py --limit 400     最多看多少行（默认全量）
"""
import argparse
import collections
import json
import sqlite3
import sys
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path
from audit_connection import backend, connect, duplicate_key

sys.stdout.reconfigure(encoding="utf-8")

HERE = Path(__file__).resolve().parent
DB = Path(__file__).resolve().parent.parent / "freight_manual.db"
BASE = "http://127.0.0.1:8795"
STALE_HOURS = 24          # 待审超过它就点名（人工侧的真风险是积压，不是准确率）
TOP_N = 10


def classify(field, msg):
    m = msg or ""
    if "不在主数据" in m or "不在港口" in m:
        return "主数据未匹配（需核实）"
    if "不得为空" in m:
        return "必填漏填"
    if "只三档" in m or "十一档" in m or "枚举" in m:
        return "枚举越界"
    if "含说明文字" in m or "说明文字" in m:
        return "脏格式（说明文字混进港口）"
    if "整数" in m or "非负" in m:
        return "价格格式"
    if "日期" in m or "有效期" in m:
        return "日期/有效期"
    if "占位" in m:
        return "占位符当值"
    return "其他：%s" % (field or m[:12])


def validate_rows(records, table):
    """在强制只读事务内复用台账校验器，不调用需授权的 HTTP 接口。"""
    if not records:
        return []
    import manualdb
    c = connect()
    try:
        master = manualdb.Master(c)
        return [manualdb.validate_record(c, master, table, record) for record in records]
    finally:
        c.close()


def diff_fields(before, after):
    """两份中文键记录的字段级差异：改了哪些键、从什么改成什么。"""
    keys = set(before) | set(after)
    out = []
    for k in sorted(keys):
        b, a = before.get(k), after.get(k)
        if str(b or "").strip() != str(a or "").strip():
            out.append((k, b, a))
    return out


def review(limit):
    engine = backend()
    c = connect(engine)
    try:
        total = c.execute("select count(*) from pending_records").fetchone()[0]
        rows = [dict(r) for r in c.execute("select * from pending_records order by id desc limit ?", (limit,)).fetchall()]
    finally:
        c.close()

    st = collections.Counter()
    kinds = collections.Counter()
    groups = collections.Counter()
    senders = collections.Counter()
    reasons = collections.Counter()
    miss = collections.defaultdict(collections.Counter)     # 主数据缺口：字段 → 未命中值
    empty = collections.Counter()                            # 必填漏填：字段
    dup = collections.Counter()
    awaiting_checks = collections.Counter()
    error_examples = collections.defaultdict(list)
    oldest = None
    for r in rows:
        st[r["review_status"]] += 1
        kinds[r["kind"]] += 1
        try:
            p = json.loads(r["payload"] or "{}")
        except Exception:
            p = {}
        g = p.get("来源群") or "?"
        groups[g] += 1
        if r["review_status"] == "awaiting":
            senders[p.get("发送人") or "?"] += 1
            awaiting_checks[r["check_status"]] += 1
        if r["review_status"] == "awaiting":
            key = duplicate_key(r, p)
            if key is not None:
                dup[key] += 1
        try:
            created = datetime.strptime((r["created_at"] or "")[:19], "%Y-%m-%d %H:%M:%S")
        except ValueError:
            created = None
        if created and r["review_status"] == "awaiting" and (oldest is None or created < oldest):
            oldest = created
        for e in json.loads(r["errors"] or "[]") if r["review_status"] == "awaiting" else []:
            cls = classify(e.get("field"), e.get("msg"))
            reasons[cls] += 1
            if len(error_examples[cls]) < 3:
                error_examples[cls].append({"entry_id": r.get("entry_id"), "field": e.get("field"), "reason": e.get("msg"), "group": g})
            if cls == "主数据未匹配（需核实）":
                miss[e.get("field") or "?"][str(e.get("input") or e.get("msg"))[:40]] += 1
            elif cls == "必填漏填":
                empty[e.get("field") or "?"] += 1

    # 人工编辑学习：edit_log 非空只代表"有留痕"（放行时也会写一条），
    # 真正的优化记录必须 orig_payload 与 payload 有字段差异，否则正确率是空转数字
    edited, traced_only = [], 0
    reviewers = collections.Counter()
    for r in rows:
        if r["reviewed_by"]:
            reviewers[r["reviewed_by"]] += 1
        if r.get("orig_payload") and (r["edit_log"] or "").strip() not in ("", "[]", "null"):
            try:
                if diff_fields(json.loads(r["orig_payload"] or "{}"), json.loads(r["payload"] or "{}")):
                    edited.append(r)
                else:
                    traced_only += 1
            except Exception:
                traced_only += 1
    learning = {"edited_rows": len(edited), "traced_only": traced_only,
                "scope": "所有有字段差异的修订，包含人工及Agent；通过率仅为当前规则机械重放，不代表业务正确率",
                "field_changes": collections.Counter(),
                "before_pass": None, "after_pass": None, "note": ""}
    if edited:
        fb = collections.Counter()
        before_recs, after_recs, tables = [], [], []
        for r in edited:
            try:
                o = json.loads(r["orig_payload"] or "{}")
                a = json.loads(r["payload"] or "{}")
            except Exception:
                continue
            for k, _, _ in diff_fields(o, a):
                fb[k] += 1
            before_recs.append(o)
            after_recs.append(a)
            tables.append("rates" if r["kind"] == "rates" else "space")
        learning["field_changes"] = dict(fb.most_common(TOP_N))
        # 按表分别重放（validate 一次只收一张表）
        res_b, res_a = [], []
        for tbl in ("rates", "space"):
            idx = [i for i, t in enumerate(tables) if t == tbl]
            if not idx:
                continue
            rb = validate_rows([before_recs[i] for i in idx], tbl)
            ra = validate_rows([after_recs[i] for i in idx], tbl)
            if rb is None or ra is None or len(rb) != len(idx) or len(ra) != len(idx):
                learning["note"] = "台账服务不可达，改前/改后正确率这次算不出来（其余维度不受影响）"
                break
            res_b += rb
            res_a += ra
        if not learning["note"] and res_b:
            learning["before_pass"] = round(100.0 * sum(1 for x in res_b if x.get("ok")) / len(res_b), 1)
            learning["after_pass"] = round(100.0 * sum(1 for x in res_a if x.get("ok")) / len(res_a), 1)
            learning["n"] = len(res_b)
    else:
        learning["note"] = "还没有人工编辑记录（edit_log 全空）：正确率维度需要积累编辑数据后才有意义"

    dup_groups = {k: v for k, v in dup.items() if v > 1 and k[1]}
    waited = round((datetime.now() - oldest).total_seconds() / 3600, 1) if oldest else None

    advice = []
    if waited is not None and waited > STALE_HOURS:
        advice.append("最老一条待审已等 %.1f 小时（阈值 %d）：积压是时效的真终点，先清最老的一批，"
                      "别让「拉到」变成「堆着」" % (waited, STALE_HOURS))
    if reasons.get("主数据未匹配（需核实）"):
        tops = []
        for f, cnt in miss.items():
            for val, n in cnt.most_common(3):
                tops.append("%s「%s」×%d" % (f, val, n))
        advice.append("主数据未匹配 %d 次：%s；先核实原文与既有别名。USD等非港口值不能新增为港口，真实缺口才补主数据。"
                      % (reasons["主数据未匹配（需核实）"], "；".join(tops[:6])))
    if reasons.get("必填漏填"):
        advice.append("必填漏填 %d 次（%s）：核对原文是字段漏提、操作通知还是仅附加费；"
                      "已有规则先查实现，不重复追加规范，不猜价格或柜型"
                      % (reasons["必填漏填"], "、".join("%s×%d" % (k, v) for k, v in empty.most_common(4))))
    if reasons.get("枚举越界") or reasons.get("脏格式（说明文字混进港口）"):
        advice.append("枚举越界 %d 次 / 脏格式 %d 次：前者改契约措辞，后者属规范里的「服务码不进港口字段」，"
                      "看是哪几个群集中出错再决定改哪份规范"
                      % (reasons.get("枚举越界", 0), reasons.get("脏格式（说明文字混进港口）", 0)))
    if dup_groups:
        advice.append("完全相同的待审载荷 %d 组共 %d 条（同来源且全部字段一致）：仅为重复候选，"
                      "须核对来源与留痕后处理，不自动删除或放行"
                      % (len(dup_groups), sum(dup_groups.values())))
    bad_rev = sum(n for k, n in reviewers.items() if not k or k.lower() in ("unknown", "none", "?"))
    if bad_rev:
        advice.append("%d 次放行没记审核人（reviewed_by=unknown/空）：这样正确率无法按人归因、也追不了责 → "
                      "看板顶栏先选身份再放行，脚本调用要带 reviewed_by" % bad_rev)
    if learning["edited_rows"]:
        if learning["before_pass"] is not None:
            advice.append("字段修订 %d 行（含人工及Agent）：当前规则下改前通过率 %.1f%% → 改后 %.1f%%（n=%d，不代表业务正确率）；"
                          "最常改的字段 %s —— 高频改动字段说明规范或契约该补话术"
                          % (learning["edited_rows"], learning["before_pass"], learning["after_pass"],
                             learning.get("n"), "、".join("%s×%d" % kv for kv in
                                                           list(learning["field_changes"].items())[:4])))
        else:
            advice.append("人工编辑 %d 行，但%s" % (learning["edited_rows"], learning["note"]))
    else:
        advice.append(learning["note"])
    if not advice:
        advice.append("没有触发任何阈值")

    return {"generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "backend": engine,
            "total_records": total, "truncated": total > len(rows), "scanned": len(rows),
            "duplicate_examples": [[r.get("entry_id") or r["id"] for r in rows
                if r["review_status"] == "awaiting" and duplicate_key(r, json.loads(r["payload"] or "{}")) == key][:3]
                for key in list(dup_groups)[:3]],
            "review_status": dict(st), "kinds": dict(kinds),
            "awaiting_checks": dict(awaiting_checks), "error_examples": dict(error_examples),
            "groups": dict(groups.most_common()), "awaiting_senders": dict(senders.most_common(TOP_N)),
            "error_classes": dict(reasons.most_common()),
            "master_gaps": {f: dict(cnt.most_common(TOP_N)) for f, cnt in miss.items()},
            "empty_required": dict(empty.most_common()), "dup_groups": len(dup_groups),
            "dup_rows": sum(dup_groups.values()), "oldest_awaiting_hours": waited,
            "reviewers": dict(reviewers.most_common()),
            "learning": learning, "advice": advice}


def human(rep):
    if rep.get("error"):
        return "# 待审队列审查\n\n%s" % rep["error"]
    L = ["# 待审队列审查 %s（扫描 %d 行）" % (rep["generated_at"], rep["scanned"]), ""]
    L.append("状态：%s ｜ 类型：%s" % (json.dumps(rep["review_status"], ensure_ascii=False),
                                       json.dumps(rep["kinds"], ensure_ascii=False)))
    if rep["oldest_awaiting_hours"] is not None:
        L.append("最老待审已等 **%.1f 小时**" % rep["oldest_awaiting_hours"])
    L.append("\n## 按群分布（人工排班用）")
    for g, n in rep["groups"].items():
        L.append("- %-28s %d 条" % (g, n))
    L.append("\n## 错误模式 → 该做什么")
    for cls, n in rep["error_classes"].items():
        L.append("- %-26s %d 次" % (cls, n))
    if rep["master_gaps"]:
        L.append("\n### 主数据未匹配（须核实，不等于缺实体）")
        for f, cnt in rep["master_gaps"].items():
            L.append("- **%s**：%s" % (f, "、".join("%s×%d" % (v, n) for v, n in list(cnt.items())[:8])))
    if rep["empty_required"]:
        L.append("\n### 必填漏填明细")
        L.append("- " + "、".join("%s×%d" % kv for kv in rep["empty_required"].items()))
    lr = rep["learning"]
    L.append("\n## 字段修订重放（含人工及Agent，不代表业务正确率）")
    if rep.get("reviewers"):
        L.append("- 参与审核：%s" % "、".join("%s×%d" % kv for kv in rep["reviewers"].items()))
    L.append("- 真改过字段的行：%d（另有 %d 行只有放行留痕、字段没变，不计入学习样本）"
             % (lr["edited_rows"], lr.get("traced_only", 0)))
    if lr["before_pass"] is not None:
        L.append("- 改前通过率 **%.1f%%** → 改后 **%.1f%%**（n=%d，重放 /api/validate 同一套闸门）"
                 % (lr["before_pass"], lr["after_pass"], lr.get("n", 0)))
    L.append(lr["scope"])
    if lr["field_changes"]:
        L.append("- 最常改字段：%s" % "、".join("%s×%d" % kv for kv in lr["field_changes"].items()))
    if lr["note"]:
        L.append("- 说明：%s" % lr["note"])
    if rep["dup_groups"]:
        L.append("\n## 完全重复候选：%d 组 / %d 条（仅提示核对）" % (rep["dup_groups"], rep["dup_rows"]))
    L.append("\n## 建议（按性价比排）")
    for i, a in enumerate(rep["advice"], 1):
        L.append("%d. %s" % (i, a))
    return "\n".join(L)


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="待审队列审查（只读）")
    ap.add_argument("--limit", type=int, default=5000)
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()
    try:
        rep = review(a.limit)
    except Exception as exc:
        # Operational failures must never look like an empty healthy queue.
        rep = {"ok": False, "error": "审查失败，未回退旧库", "error_type": type(exc).__name__}
    print(json.dumps(rep, ensure_ascii=False, indent=1) if a.json else human(rep))
    sys.exit(1 if rep.get("error") else 0)
