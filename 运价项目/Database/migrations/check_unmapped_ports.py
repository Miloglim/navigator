"""check_unmapped_ports.py — 扫待审核 failed 条目，统计港口（目的港/起运港）未映射的原值"""
import sys, io, json, sqlite3
sys.stdout.reconfigure(encoding='utf-8')

DB = r'C:\Users\YQN\Documents\正式版运价库\运价项目\Database\freight_manual.db'
c = sqlite3.connect(DB); c.row_factory = sqlite3.Row

port_fields = ("目的港", "起运港", "航线")
rows = list(c.execute(
    "SELECT id, kind, payload, errors FROM pending_records "
    "WHERE check_status='failed' AND review_status='awaiting' ORDER BY id"
))
print(f'待处理 failed 条目共 {len(rows)} 条')

from collections import defaultdict
unmapped = defaultdict(lambda: {"n": 0, "pids": [], "groups": set(), "msg": ""})
total_fail_port = 0
for row in rows:
    try:
        errs = json.loads(row["errors"] or "[]")
        payload = json.loads(row["payload"] or "{}")
    except Exception:
        continue
    for e in errs:
        f = e.get("field")
        msg = str(e.get("msg") or "")
        inp = str(e.get("input") or "").strip()
        if f not in port_fields:
            continue
        if "不在" not in msg and "主数据" not in msg:
            continue  # 只统计"不在主数据"类，跳过"含说明文字"脏值类
        total_fail_port += 1
        rec = unmapped[(f, inp)]
        rec["n"] += 1
        rec["pids"].append(row["id"])
        rec["groups"].add(str(payload.get("来源群") or ""))
        rec["msg"] = msg

print(f'其中港口"不在主数据"类错误 {total_fail_port} 处，去重后 {len(unmapped)} 个不同 (字段,原值):')
print()
for (f, inp), info in sorted(unmapped.items(), key=lambda x: -x[1]["n"]):
    groups = "、".join(sorted(g for g in info["groups"] if g))[:40]
    print(f'  [{f}] {inp!r}  ×{info["n"]}  群={groups}')
    print(f'      pids={info["pids"][:8]}')
