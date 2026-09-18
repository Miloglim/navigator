# -*- coding: utf-8 -*-
"""给"驳回回炉"补最后两处（用补丁脚本而非编辑器，避免与并发写入互相覆盖）。

R1 manualdb.rework_pending：加"等于没改"护栏——update_pending 会把主数据查无此值的强匹配字段回退，
   于是全被回退的提交等于什么都没改，却会带着 passed 回到人工队列伪装成"已处理"。
R2 rework_allowed_fields：兼容另一会话新加的"组完整性"连坐标记（那条错误没有真实 field，
   不能让它的存在把白名单挤成空、也不能把连坐标记当成可改字段）。
T1 manual_check：加一组端到端用例（越权字段 / 编造值全回退 / 正常返修回队列 / 满 2 轮拒再刷 / 收尾恢复原状）。

幂等：锚点已应用过就跳过；命中数不为 1 直接退出不写盘。
"""
import re
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
DB = Path(r"C:\Users\YQN\Documents\正式版运价库\运价项目\Database")


def load(p):
    return (DB / p).read_text(encoding="utf-8")


def save(p, s):
    (DB / p).write_text(s, encoding="utf-8")


def apply_once(s, old, new, tag, done_marker):
    if done_marker in s:
        print("  跳过 %s（已应用）" % tag)
        return s, False
    n = s.count(old)
    if n != 1:
        raise SystemExit("%s 锚点命中 %d 次（应为 1），中止不写盘" % (tag, n))
    print("  应用 %s" % tag)
    return s.replace(old, new), True


# ---------- R1 + R2：manualdb.py ----------
m = load("manualdb.py")
R1_OLD = '''    passed = res.get("check_status") == "passed"
    diff = [{"field": k, "old": base.get(k), "new": v} for k, v in patch.items() if str(base.get(k)) != str(v)]'''
R1_NEW = '''    after = json.loads(conn.execute(
        "SELECT payload FROM pending_records WHERE id=?", (pid,)).fetchone()[0] or "{}")
    # 真实改动以落库后的 payload 为准：update_pending 会把主数据查无此值的强匹配字段回退掉，
    # 所以"提交了但全被回退"其实等于什么都没改。
    diff = [{"field": k, "old": base.get(k), "new": after.get(k)}
            for k in patch if str(base.get(k)) != str(after.get(k))]
    if not diff:
        _append_edit_log(conn, pid, [{"ts": now_cn(), "actor": actor, "action": "rework_failed",
                                      "field": "、".join(sorted(patch)), "old": reject_note[:60],
                                      "new": json.dumps({"提交": patch, "结果": "全部被回退或与原值相同",
                                                         "轮次": rounds + 1}, ensure_ascii=False)}])
        conn.execute("UPDATE pending_records SET review_status='rejected', note=? WHERE id=?",
                     ("%s｜第%d轮 agent 提交的值主数据里没有或与原值相同，等于没改"
                      % (reject_note, rounds + 1), pid))
        conn.commit()
        return {"ok": False, "error": "提交的值全部被回退（主数据查无此值）或与原值相同，等于没改",
                "status": "rejected", "round": rounds + 1, "strict_rejected": res.get("rejected") or []}
    passed = res.get("check_status") == "passed"'''
m, c1 = apply_once(m, R1_OLD, R1_NEW, "R1 等于没改护栏", "真实改动以落库后的 payload 为准")

R2_OLD = '''        for e in json.loads(row["errors"] or "[]"):
            f = str(e.get("field") or "").strip()
            if f in REWORK_CORE_FIELDS:
                ok.add(f)'''
R2_NEW = '''        for e in json.loads(row["errors"] or "[]"):
            # 另一会话的"组完整性"连坐标记不是真实字段错误（它没有 field），
            # 既不能当可改字段，也不能因为它在就把白名单判空。
            if GROUP_MARK in str(e.get("msg") or "") or str(e.get("field") or "") == GROUP_MARK:
                continue
            f = str(e.get("field") or "").strip()
            if f in REWORK_CORE_FIELDS:
                ok.add(f)'''
m, c2 = apply_once(m, R2_OLD, R2_NEW, "R2 组完整性标记兼容", "连坐标记不是真实字段错误")
if c1 or c2:
    save("manualdb.py", m)

# ---------- T1：manual_check.py 端到端用例 ----------
k = load("manual_check.py")
T_OLD = '''            # 护栏（2026-09-17 审查新增）：awaiting 行直接物理删除应被拒'''
T_NEW = '''            # 驳回回炉（2026-09-18）：带原因驳回 → agent 改 → 回人工队列，护栏逐条验
            call("/api/pending/reject", "POST", {"id": bid, "note": "自检：「目的港」错了"}, token)
            _, rw_out = call("/api/pending/rework", "POST",
                             {"id": bid, "fields": {"船司": "MSC"}, "reason": "越权尝试"}, token)
            ck("返修只能改驳回点名字段，改别的被拒",
               (not rw_out.get("ok")) and "船司" in (rw_out.get("rejected_fields") or []), rw_out)
            _, rw_fake = call("/api/pending/rework", "POST",
                              {"id": bid, "fields": {"目的港": "NotAPortZZZ"}, "reason": "编造值"}, token)
            ck("返修不许编主数据里没有的值（全被回退＝等于没改，退回驳回日志）",
               (not rw_fake.get("ok")) and rw_fake.get("status") == "rejected"
               and bool(rw_fake.get("strict_rejected")), rw_fake)
            _, rw_ok = call("/api/pending/rework", "POST",
                            {"id": bid, "fields": {"目的港": "Rio de Janeiro"}, "reason": "改成真港"}, token)
            _, aw5 = call("/api/pending?status=awaiting&limit=80")
            back = next((r for r in aw5.get("rows", []) if r["id"] == bid), None)
            ck("返修通过闸门后回到人工队列并带返修标记",
               rw_ok.get("ok") and rw_ok.get("status") == "awaiting" and bool(back)
               and str(back.get("note") or "").startswith("返修第1轮"), rw_ok)
            call("/api/pending/reject", "POST", {"id": bid, "note": "自检：「目的港」再驳回"}, token)
            call("/api/pending/rework", "POST",
                 {"id": bid, "fields": {"目的港": "Santos"}, "reason": "第2轮"}, token)
            call("/api/pending/reject", "POST", {"id": bid, "note": "自检：第3次驳回"}, token)
            _, rw_cap = call("/api/pending/rework", "POST",
                             {"id": bid, "fields": {"目的港": "Paranaguá"}, "reason": "第三轮"}, token)
            ck("返修满 2 轮后拒绝再刷，交人工处理",
               not rw_cap.get("ok") and "轮" in (rw_cap.get("error") or ""), rw_cap)
            _, rs2 = call("/api/pending/restore", "POST", {"id": bid}, token)
            ck("撤回误驳顺带清掉返修标记（后续用例状态复原）", rs2.get("ok"), rs2)
            # 护栏（2026-09-17 审查新增）：awaiting 行直接物理删除应被拒'''
k, c3 = apply_once(k, T_OLD, T_NEW, "T1 回归用例", "驳回回炉（2026-09-18）")
if c3:
    save("manual_check.py", k)

# 语法自查
import py_compile
for f in ("manualdb.py", "manual_check.py"):
    py_compile.compile(str(DB / f), doraise=True)
print("编译通过：manualdb.py / manual_check.py")
