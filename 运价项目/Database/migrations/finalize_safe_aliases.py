"""finalize_safe_aliases.py — 补目标港在主数据确凿存在的别名（逐个核对过，非 difflib 猜）"""
import sys, io, sqlite3
from pathlib import Path
sys.stdout.reconfigure(encoding='utf-8')
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import manualdb

DB = Path(r'C:\Users\YQN\Documents\正式版运价库\运价项目\Database\freight_manual.db')

# (alias, kind, target规范名, scope_group, 依据)  —— 目标均已在主数据核实为同一港
SAFE = [
    ("达米埃塔",              "pod",   "Damietta",    "土耳其价格更新", "埃及杜姆亚特，Damietta 另一中文译名"),
    ("Dumyat",               "pod",   "Damietta",    "土耳其价格更新", "Damietta 拼写变体"),
    ("莱索斯",                "pod",   "Leixões",     "土耳其价格更新", "葡萄牙 Leixões 中文译名"),
    ("CONSTANZA 康斯坦察",    "pod",   "Constanta",   "土耳其价格更新", "罗马尼亚 Constanta 变体拼写"),
    ("Durres 都拉斯",         "pod",   "Durrës",      "土耳其价格更新", "阿尔巴尼亚 Durrës"),
    ("FOS",                  "pod",   "Fos-sur-Mer", "土耳其价格更新", "法国福斯港"),
    ("Puerto Moin",          "pod",   "Moín",        "华南-拉美",     "哥斯达黎加莫因港，主数据规范名 Moín"),
    ("亚德里海",              "route", "亚得里亚海",   "土耳其价格更新", "航线名漏一字的错写，主数据有亚得里亚海"),
]

conn = sqlite3.connect(str(DB)); conn.row_factory = sqlite3.Row
for alias, kind, target, grp, why in SAFE:
    # 校验 target 确实在主数据
    if kind == "route":
        ok = conn.execute("SELECT 1 FROM routes WHERE name=?", (target,)).fetchone()
    else:
        ok = conn.execute("SELECT 1 FROM ports WHERE name=?", (target,)).fetchone()
    if not ok:
        print(f'  !! 跳过 {alias!r}→{target!r}：主数据里查无此规范名'); continue
    try:
        manualdb.alias_scope_save(conn, {
            "alias": alias, "kind": kind, "target": target,
            "scope_group": grp, "source": "manual",
            "note": f"港口未映射批量修复：{why}",
        })
        print(f'  +[{kind}] {alias!r} → {target!r}  （{grp}）')
    except Exception as e:
        print(f'  失败 {alias!r}: {e}')
manualdb.invalidate_master()

# 重跑这些别名命中的 pending 归一改判
m = manualdb.get_master(conn)
targets = {a for a, *_ in SAFE}
flipped = 0
import json
for row in conn.execute("SELECT id, kind, payload FROM pending_records WHERE check_status='failed' AND review_status='awaiting'"):
    try:
        payload = json.loads(row["payload"] or "{}")
    except Exception:
        continue
    if not any(str(payload.get(f) or "").strip() in targets for f in ("目的港", "起运港", "航线")):
        continue
    res = manualdb.update_pending(conn, m, row["id"], {}, kind=row["kind"], actor="港口批量修复2")
    if res.get("check_status") == "passed":
        flipped += 1

conn.close()
print(f'\n重跑转 passed：{flipped} 条')
