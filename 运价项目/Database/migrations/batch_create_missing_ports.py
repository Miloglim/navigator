"""batch_create_missing_ports.py — 按用户拍板批量建缺失港口/航线 + 现有港补别名，再重跑 pending。

用户决定：① Ancona/Bari 两独立港 ② 新建航线"地西" ③ 希腊归地东 ④ 内陆CY点也算目的港
不自动建：GUARANAO（巴西，与现有 Puerto Guarani 是否同港存疑）→ 留人工
标待复核：Gijón(原文中英不符维戈=Vigo)、Panama City、Puerto del Rosario 等新港航线归属
"""
import sys, io, json, sqlite3
from pathlib import Path
sys.stdout.reconfigure(encoding='utf-8')
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import manualdb

DB = Path(r'C:\Users\YQN\Documents\正式版运价库\运价项目\Database\freight_manual.db')
conn = sqlite3.connect(str(DB)); conn.row_factory = sqlite3.Row

# ---------- 1. 航线：新建"地西"，地东加别名"希腊" ----------
routes_region = {r['name']: r for r in conn.execute("SELECT name,region,sort,aliases FROM routes WHERE active=1")}
if '地西' not in routes_region:
    manualdb.master_save(conn, "routes", {
        "name": "地西", "region": "欧地线", "sort": 121,
        "aliases": "西地中海,西地中海线,west med,western med,Wesmed",
        "active": 1, "note": "2026-09-17 心跳新拓土耳其地中海西航线，人工确认新建",
    })
    print("+routes 地西（西地中海）")
_d = routes_region.get('地东')
if _d and '希腊' not in [x.strip() for x in (_d['aliases'] or '').split(',')]:
    conn.execute("UPDATE routes SET aliases=? WHERE name=?",
                 (f"{_d['aliases']},希腊", '地东'))
    conn.commit(); print("+routes 地东.aliases += 希腊")

# ---------- 2. 新港（name/country/name_cn/route/aliases含原值变体）----------
# 每条 = (规范名, 国家, 中文名, 建议航线, [别名含pending原值英中串], 复核标记)
NEW_PORTS = [
    ("Ancona",                  "IT", "安科纳",            "亚得里亚海", ["Ancona 安科纳", "安科纳"], ""),
    ("Bari",                    "IT", "巴里",              "亚得里亚海", ["BARI 巴里", "巴里"], ""),
    ("Bar",                     "ME", "巴尔",              "亚得里亚海", ["Bar 巴尔", "巴尔"], ""),
    ("Beirut",                  "LB", "贝鲁特",            "地东",       ["Beirut 贝鲁特", "贝鲁特"], ""),
    ("Limassol",                "CY", "利马索尔",          "地东",       ["Limassol 利马索尔", "利马索尔"], ""),
    ("Kumport",                 "TR", "库姆波特",          "地东",       ["KUMPORT", "库姆波特"], "土耳其私港,规范名待核"),
    ("Las Palmas de Gran Canaria","ES","拉斯帕尔马斯",      "地西",       ["Las Palmas 拉斯帕尔马斯", "Las Palmas", "拉斯帕尔马斯"], "加那利,航线归属待核"),
    ("Santa Cruz de Tenerife",  "ES", "圣克鲁斯德特内里费","地西",       ["Santa Cruz De Tenerife 圣克鲁斯-德特内里费", "Santa Cruz de Tenerife", "圣克鲁斯"], "加那利"),
    ("Arrecife",                "ES", "阿雷西费",          "地西",       ["Arrecife 阿雷西费", "阿雷西费"], "加那利"),
    ("Puerto del Rosario",      "ES", "罗萨里奥港",        "地西",       ["Puerto del Rosario 罗萨里奥港", "罗萨里奥港"], "加那利，勿混阿根廷 Rosario"),
    ("Madrid",                  "ES", "马德里",            "地西",       ["MADRID CY", "马德里"], "内陆铁路点CY"),
    ("Zaragoza",                "ES", "萨拉戈萨",          "地西",       ["ZARAGOZA CY", "萨拉戈萨"], "内陆铁路点CY"),
    ("Gijón",                   "ES", "希洪",              "地西",       ["GIJON 维戈", "GIJON", "Gijon", "希洪"], "原文中英不符(维戈=Vigo)，待复核"),
    ("Philipsburg",             "CW", "菲利普斯堡",        "加勒比",     ["PHILIPSBURG", "菲利普斯堡"], "库拉索"),
    ("Cap-Haïtien",             "HT", "海地角",            "加勒比",     ["Cap Haitien", "Cap-Haitien", "海地角"], ""),
]
existing = {r['name'] for r in conn.execute("SELECT name FROM ports")}
created = 0
for name, country, name_cn, route, alias_vars, note in NEW_PORTS:
    if name in existing:
        print(f"  跳过已存在 {name}"); continue
    aliases = [name] + [a for a in alias_vars if a]
    manualdb.master_save(conn, "ports", {
        "name": name, "country": country, "name_cn": name_cn, "name_en": name,
        "route": route, "kind": "pod", "active": 1,
        "aliases": ",".join(dict.fromkeys(aliases)),
        "source": "manual-import",
        "note": ("2026-09-17 心跳新港批量建" + ("；" + note if note else "")),
    })
    created += 1
    flag = f"  [{route}]"
    print(f"  +ports {name!r:26s} {country} {name_cn}{flag}{' ⚠'+note if note else ''}")

# ---------- 3. 现有港补别名（同港异拼，不新建）----------
# HAITI Lafito → Lafiteau(海地)；Cartagena → Cartagena(CO)(拉美线语境)
def add_port_alias(port_name, alias, why):
    r = conn.execute("SELECT aliases FROM ports WHERE name=?", (port_name,)).fetchone()
    if not r:
        print(f"  !! {port_name} 不存在"); return
    al = [x.strip() for x in (r['aliases'] or '').split(',') if x.strip()]
    if alias in al:
        print(f"  = 已有 {port_name} 别名 {alias!r}"); return
    al.append(alias)
    conn.execute("UPDATE ports SET aliases=?, note=COALESCE(note,'')||? WHERE name=?",
                 (",".join(al), f"；+{alias}({why})", port_name))
    conn.commit(); print(f"  +ports {port_name}.aliases += {alias!r}（{why}）")

add_port_alias("Lafiteau", "HAITI Lafito", "海地拉菲特同港")

# Cartagena 群限定归 CO（拉美线群语境；西班牙 Cartagena(ES) 另在别处）
for grp in ("COSCO/HMM/MSK/OOCL交流群", "华南-拉美"):
    try:
        manualdb.alias_scope_save(conn, {
            "alias": "Cartagena", "kind": "pod", "target": "Cartagena(CO)",
            "scope_group": grp, "source": "manual",
            "note": "拉美线语境 bare Cartagena 归哥伦比亚(西=Cartagena(ES))",
        })
        print(f"  +alias_scope [{grp}] Cartagena→Cartagena(CO)")
    except Exception as e:
        print(f"  Cartagena @{grp} 失败: {e}")

manualdb.invalidate_master()

# ---------- 4. 重跑所有 failed pending 归一改判 ----------
m = manualdb.get_master(conn)
flipped = 0
still = []
for row in conn.execute("SELECT id, kind, payload, errors FROM pending_records "
                        "WHERE check_status='failed' AND review_status='awaiting'"):
    res = manualdb.update_pending(conn, m, row["id"], {}, kind=row["kind"], actor="批量建港后重跑")
    if res.get("check_status") == "passed":
        flipped += 1
    else:
        still.append((row["id"], [e.get("field")+":"+str(e.get("input")) for e in res.get("errors", [])
                                 if "不在" in str(e.get("msg","")) or "主数据" in str(e.get("msg",""))]))
print(f"\n重跑转 passed: {flipped} 条")
print(f"仍 failed(还含未映射，多为 GUARANAO 等待人工): {len(still)} 条")
for pid, pf in still[:20]:
    print(f"  #{pid}: {pf}")
conn.close()
