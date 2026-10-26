# -*- coding: utf-8 -*-
"""rework.py —— 驳回回炉：把人工驳回并写了原因的条目捞回来，改一版再交回人工复核。

只有两个动作，都在台账侧有硬护栏（manualdb.rework_pending），本脚本不判规则、只搬运：

    python scripts/rework.py list [--days 7] [--limit 20]
        列出返修池（默认最近 7 天、最多 20 条）。空池一行提示就退出，不产生任何后续开销。
    python scripts/rework.py submit --id 856 --fields '{"目的港":"Istanbul"}' --reason '城市不是码头'
        提交修订。服务端会：只允许改「校验报错字段 + 驳回原因里用「」点名的字段」；
        强匹配字段编造主数据里没有的值会被回退；改完仍不过就退回 rejected 并记第几轮；
        同一行最多 2 轮，超了要求人工在「驳回日志」里撤回后改。

放行/驳回永远是人的权力：本脚本绝不调用 /api/pending/approve。
"""
import argparse
import json
import sys
from datetime import datetime, timedelta

import ledger_client as db
import paths as P          # 只为拿 CHAT_SOURCES_F 路径，读全量群的 shard 归属（返修池按分片切分）

sys.stdout.reconfigure(encoding="utf-8")

# 群名（写入记录的 来源群）→ 所属分片，供返修池按 --shard 切分。
# 直接读配置文件全量，绕开 P.chat_groups()（它会按当前 FRT_SHARD 过滤，那样另一片的群就看不全）。
_NAME2SHARD = {g["name"]: str(g.get("shard", "")).strip().upper()
               for g in json.load(open(P.CHAT_SOURCES_F, encoding="utf-8"))["groups"]}

CORE = ("起运港", "目的港", "船司", "航线", "柜型", "海运费USD", "价格USD",
        "有效期船期", "船名航次", "箱量", "舱位类型", "备注")


def allowed_fields(row):
    """与服务端同规则（只为少犯错提前提示；真拦在 manualdb.rework_allowed_fields）。"""
    ok = []
    for e in row.get("errors") or []:
        f = str(e.get("field") or "").strip()
        if f in CORE and f not in ok:
            ok.append(f)
    note = str(row.get("note") or "")
    i = 0
    while True:
        a = note.find("「", i)
        if a < 0:
            break
        b = note.find("」", a + 1)
        if b < 0:
            break
        f = note[a + 1:b].strip()
        if f in CORE and f not in ok:
            ok.append(f)
        i = b + 1
    return ok


def rounds_of(row):
    n = 0
    try:
        for e in json.loads(row.get("edit_log") or "[]"):
            if e.get("action") in ("rework", "rework_failed"):
                n += 1
    except (ValueError, TypeError):
        pass
    return n


def cmd_list(days, limit, shard=None):
    res = db._request("GET", "/api/pending?status=rejected&limit=500", None)
    if not isinstance(res, dict) or not res.get("ok"):
        print("ABORT: 拉返修池失败：%s" % (res or {}))
        return 1
    rows = res.get("rows") or []
    cut = datetime.now() - timedelta(days=days)
    keep = []
    for r in rows:
        try:
            if datetime.strptime((r.get("created_at") or "")[:19], "%Y-%m-%d %H:%M:%S") < cut:
                continue
        except ValueError:
            pass
        # 空原因是人工确认的垃圾条目，只沉淀 noise 学习信号，不进入返修池。
        if not str(r.get("note") or "").strip():
            continue
        if shard:   # 按分片切池：只捞归属本片群的驳回，两片各修各的、不撞同一条
            p = r.get("payload") or {}
            if isinstance(p, str):
                try:
                    p = json.loads(p)
                except ValueError:
                    p = {}
            if _NAME2SHARD.get(str(p.get("来源群") or "").strip()) != shard:
                continue
        keep.append(r)
    keep = keep[:limit]
    if not keep:
        print("REWORK_DONE: 返修池为空（最近 %d 天无待返修条目），本轮不用做任何事" % days)
        return 0
    print("# 返修池 %d 条（近 %d 天内被驳回，按原因修订后用 submit 交回人工）" % (len(keep), days))
    for r in keep:
        p = r.get("payload") or {}
        if isinstance(p, str):
            try:
                p = json.loads(p)
            except ValueError:
                p = {}
        af = allowed_fields(r)
        rd = rounds_of(r)
        print("\n## id=%s %s｜%s｜已返修 %d 轮" % (r.get("id"), r.get("kind"), p.get("来源群") or "?", rd))
        print("   驳回原因：%s" % (r.get("note") or "（未填）"))
        print("   可改字段：%s" % ("、".join(af) if af else "（无法定位字段，请跳过并在汇报里列出，交人工）"))
        for f in af:
            print("     %-8s 现值=%s" % (f, p.get(f)))
        print("   原文片段：%s" % str(p.get("消息原文") or "")[:150].replace("\n", " ⏎ "))
        if af:
            print("   提交：python scripts/rework.py submit --id %s --fields '{\"%s\":\"<改后值>\"}' --reason '<一句依据>'"
                  % (r.get("id"), af[0]))
        if rd >= 2:
            print("   ⚠ 已满 2 轮，服务端会拒绝再返修 —— 请在汇报里点名让人工到「驳回日志」处理")
    print("\n# 注意：值必须是台账主数据里真实存在的写法；编造会被强匹配回退。不许调用放行接口。")
    return 0


def cmd_submit(pid, fields, reason, actor):
    res = db._request("POST", "/api/pending/rework",
                      {"id": pid, "fields": fields, "reason": reason, "actor": actor})
    if not isinstance(res, dict):
        print("SUBMIT_FAIL: 无响应")
        return 1
    if not res.get("ok"):
        print("SUBMIT_FAIL id=%s：%s%s" % (pid, res.get("error"),
              "（越权字段：%s）" % "、".join(res.get("rejected_fields") or []) if res.get("rejected_fields") else ""))
        return 1
    st = res.get("status")
    print("SUBMIT_OK id=%s → %s（第 %s 轮）改动：%s" % (
        pid, "回到待审核等人复核" if st == "awaiting" else "仍不合格，退回驳回日志",
        res.get("round"), "、".join("%s:%s→%s" % (d["field"], d["old"], d["new"]) for d in res.get("changes") or [])))
    for e in res.get("strict_rejected") or []:
        print("   ⚠ 该值主数据里没有、已回退：%s = %s（%s）" % (e.get("field"), e.get("input"), e.get("msg")))
    for e in res.get("errors") or []:
        print("   仍不过：%s：%s" % (e.get("field"), e.get("msg")))
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="驳回回炉（捞返修池 / 提交修订）")
    sub = ap.add_subparsers(dest="cmd", required=True)
    a1 = sub.add_parser("list")
    a1.add_argument("--days", type=int, default=7)
    a1.add_argument("--limit", type=int, default=20)
    a1.add_argument("--shard", default=None, help="只捞该分片（A/B/C/D）归属群的驳回；不给则全量")
    a2 = sub.add_parser("submit")
    a2.add_argument("--id", type=int, required=True)
    a2.add_argument("--fields", required=True, help='JSON 对象，如 {"目的港":"Istanbul"}')
    a2.add_argument("--reason", default="")
    a2.add_argument("--actor", default="采集agent")
    a = ap.parse_args()
    if a.cmd == "list":
        sys.exit(cmd_list(a.days, a.limit, (a.shard or "").strip().upper() or None))
    try:
        f = json.loads(a.fields)
    except ValueError:
        print("fields 不是合法 JSON")
        sys.exit(1)
    sys.exit(cmd_submit(a.id, f, a.reason, a.actor))
