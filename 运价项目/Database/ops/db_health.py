# -*- coding: utf-8 -*-
"""db_health.py —— 台账库维护与检查（只读；唯一的写操作是可选的 --vacuum，默认不执行）。

维度（自己定的，原则是"台账已有的判据不重写"）：
  A 服务与可用性    8795 是否应答、/api/stats 能否读
  B 全库体检        GET /api/audit → violations（标准化）/ integrity（健康度）/ master_health
  C 规模与积压      业务表行数、待审三态、最老待审等待时长
  D 主数据健康      ports/carriers/routes/port_groups/senders/chat_groups/operators 行数、
                    note=auto 的待复核条数
  E 存储卫生        库文件大小、页碎片率（freelist/page）、-wal/-journal 残留
  F 图片完整性      业务表 image_file 引用 vs Database/images 实际文件（缺图 = 看板显示不出原图）
  G 备份与时效      最近一次整库快照的新鲜度、水位线是否推进
  H 规范漂移        复用 skill 的 spec_alias_diff.py（规范缩写表 ↔ alias_scope + 断链）

    python ops/db_health.py            人读报告（PASS/WARN/FAIL + 建议）
    python ops/db_health.py --json
退出码 = FAIL 项数（0 表示全绿），供定时任务判断要不要提醒。
"""
import argparse
import glob
import json
import os
import sqlite3
import subprocess
import sys
import urllib.request
from datetime import datetime
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

HERE = Path(__file__).resolve().parent
DB_DIR = HERE.parent                      # .../Database
PROJ = DB_DIR.parent                      # .../运价项目
DB = Path(os.environ.get("FRT_DB") or (DB_DIR / "freight_manual.db"))
IMAGES = DB_DIR / "images"
BASE = os.environ.get("FRT_BOARD_URL", "http://127.0.0.1:8795")
DIFF = PROJ / "Skill" / "freight-rate-heartbeat" / "scripts" / "spec_alias_diff.py"
WM = PROJ / "Skill" / "heartbeat-data" / "watermark.json"
SNAPSHOT_GLOBS = [str(PROJ.parent / "归档" / "**" / "*.json"), str(DB_DIR / "*export*.json")]

# 阈值
FRAG_WARN = 0.20          # 空闲页占比超过它 → 建议 VACUUM
STALE_PENDING_H = 24      # 最老待审等待小时数
STALE_SNAPSHOT_D = 7      # 快照超过几天算旧
STALE_WM_H = 26           # 水位线超过几小时没推进


def _get(path, timeout=25):
    try:
        with urllib.request.urlopen(BASE + path, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8")), None
    except Exception as e:
        return None, str(e)[:120]


def _sql(q, args=()):
    c = sqlite3.connect("file:%s?mode=ro" % str(DB).replace("\\", "/"), uri=True)
    try:
        return c.execute(q, args).fetchall()
    finally:
        c.close()


def check():
    items = []          # (级别, 维度, 结论, 建议)

    def add(level, dim, msg, advice=""):
        items.append({"level": level, "dim": dim, "msg": msg, "advice": advice})

    # A 服务
    stats, err = _get("/api/stats")
    if stats is None:
        add("FAIL", "A 服务", "台账服务不可达（%s）" % err,
            "双击「运价看板-启动并打开.bat」；起不来就看 manual.log 尾部")
        return {"generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "items": items,
                "fail": sum(1 for x in items if x["level"] == "FAIL")}
    add("PASS", "A 服务", "8795 应答正常")

    # B 全库体检（判据来自台账本身，不在这里另写一套）
    audit, aerr = _get("/api/audit", timeout=60)
    if audit is None:
        add("WARN", "B 体检", "/api/audit 调不通：%s" % aerr, "服务在但体检失败，看 manual.log")
    else:
        v, i = audit.get("count", 0), audit.get("integrity_count", 0)
        mh = (audit.get("master_health") or {}).get("issues") or []
        lvl = "FAIL" if v else ("WARN" if (i or mh) else "PASS")
        add(lvl, "B 体检", "标准化违规 %d 项、健康度 %d 项、主数据问题 %d 项" % (v, i, len(mh)),
            "违规去「航管端 → 待审核/价舱管理」按行修；健康度多为孤儿主数据，去「配置端 → 库管理」补全"
            if (v or i) else "")
        for x in mh[:5]:
            add("WARN", "B 体检", "主数据：%s（%s）" % (x.get("code"), x.get("level")), str(x.get("detail"))[:120])

    # C 规模与积压
    for t in ("freight_rates", "space_records"):
        try:
            n = _sql("select count(*) from %s" % t)[0][0]
            add("PASS" if n else "WARN", "C 规模", "%s %d 行" % (t, n),
                "" if n else "业务表是空的：确认是否刚清库，或采集/放行链路没通")
        except sqlite3.Error as e:
            add("FAIL", "C 规模", "%s 读不了：%s" % (t, e))
    try:
        st = dict(_sql("select review_status, count(*) from pending_records group by 1"))
        add("PASS", "C 积压", "待审 %s" % json.dumps(st, ensure_ascii=False))
        old = _sql("select min(created_at) from pending_records where review_status='awaiting'")
        if old and old[0][0]:
            hours = (datetime.now() - datetime.strptime(old[0][0][:19], "%Y-%m-%d %H:%M:%S")).total_seconds() / 3600
            add("WARN" if hours > STALE_PENDING_H else "PASS", "C 积压",
                "最老待审已等 %.1f 小时" % hours,
                "超过 %d 小时：积压会让「拉到」失去意义，优先清最老一批" % STALE_PENDING_H
                if hours > STALE_PENDING_H else "")
    except sqlite3.Error as e:
        add("FAIL", "C 积压", "pending 读不了：%s" % e)

    # D 主数据
    try:
        for t in ("ports", "carriers", "routes", "port_groups", "senders", "chat_groups", "operators"):
            n = _sql("select count(*) from %s" % t)[0][0]
            auto = 0
            cols = [r[1] for r in _sql("PRAGMA table_info(%s)" % t)]
            if "note" in cols:
                auto = _sql("select count(*) from %s where note like '%%auto%%'" % t)[0][0]
            lvl = "WARN" if (n == 0 or auto) else "PASS"
            add(lvl, "D 主数据", "%s %d 行%s" % (t, n, "，其中 note=auto 待复核 %d 行" % auto if auto else ""),
                "auto 行是闸门自动补的主键，需要人工确认规范名/别名" if auto else
                ("该表为空：主数据没播种，归一会大面积 failed" if n == 0 else ""))
    except sqlite3.Error as e:
        add("FAIL", "D 主数据", "读不了：%s" % e)

    # E 存储卫生
    try:
        size = DB.stat().st_size
        pc = _sql("PRAGMA page_count")[0][0]
        fl = _sql("PRAGMA freelist_count")[0][0]
        frag = fl / pc if pc else 0
        add("WARN" if frag > FRAG_WARN else "PASS", "E 存储",
            "库 %.1f MB，空闲页占比 %.1f%%（%d/%d 页）" % (size / 1e6, 100 * frag, fl, pc),
            "碎片超 %d%%：停服务后跑一次 VACUUM（服务在跑时别做，会锁库）" % int(FRAG_WARN * 100)
            if frag > FRAG_WARN else "")
        for suf in ("-wal", "-journal", "-shm"):
            for p in glob.glob(str(DB) + suf + "*"):
                add("WARN", "E 存储", "残留 %s（%.1f MB）" % (Path(p).name, os.path.getsize(p) / 1e6),
                    "服务正常关闭会自动合并；长期残留说明上次是异常退出")
    except (sqlite3.Error, OSError) as e:
        add("FAIL", "E 存储", "读不了：%s" % e)

    # F 图片完整性：image_file 存的是带前导 @ 的 mediaId，images/ 里文件名是 <mediaId>_宽_高.png，
    # 所以必须剥 @ 再做词干前缀匹配；并按唯一引用计数（一张周表会被几十条记录共用）
    try:
        have = sorted(f.name for f in IMAGES.iterdir()) if IMAGES.is_dir() else []
        refs = set()
        for t in ("freight_rates", "space_records"):
            for (v,) in _sql("select image_file from %s where image_file is not null and image_file<>''" % t):
                for name in str(v).replace(";", ",").split(","):
                    if name.strip():
                        refs.add(name.strip())
        missing = sorted(r for r in refs if not any(h.startswith(r.lstrip("@")[:16]) for h in have))
        add("WARN" if missing else "PASS", "F 图片",
            "唯一引用 %d 张，命中 %d 张，真缺 %d 张（images/ 现有 %d 个文件）"
            % (len(refs), len(refs) - len(missing), len(missing), len(have)),
            ("缺图会让看板「原图溯源」显示不出来：按消息原文里的 mediaId 重新下载归位；缺的样例 "
             + ", ".join(missing[:3])) if missing else "")
    except (sqlite3.Error, OSError) as e:
        add("WARN", "F 图片", "查不了：%s" % e)

    # G 备份与时效
    newest = None
    for pat in SNAPSHOT_GLOBS:
        for p in glob.glob(pat, recursive=True):
            try:
                t = datetime.fromtimestamp(os.path.getmtime(p))
            except OSError:
                continue
            if newest is None or t > newest[0]:
                newest = (t, Path(p).name)
    if newest:
        days = (datetime.now() - newest[0]).total_seconds() / 86400
        add("WARN" if days > STALE_SNAPSHOT_D else "PASS", "G 备份",
            "最近整库快照 %s（%.1f 天前）" % (newest[1], days),
            "超过 %d 天：代码回滚有 git，但数据回退点只能靠 GET /api/export，建议导一份" % STALE_SNAPSHOT_D
            if days > STALE_SNAPSHOT_D else "")
    else:
        add("WARN", "G 备份", "找不到任何整库快照", "跑一次 GET /api/export 存到 归档/ 下")
    try:
        wm = json.loads(WM.read_text(encoding="utf-8"))
        ages = []
        for ts in wm.values():
            ages.append((datetime.now() - datetime.strptime(ts, "%Y-%m-%d %H:%M:%S")).total_seconds() / 3600)
        if ages:
            stale = sum(1 for a in ages if a > STALE_WM_H)
            add("WARN" if stale else "PASS", "G 时效",
                "水位线 %d 群，最旧 %.1f 小时前，超 %dh 未推进 %d 群" % (len(ages), max(ages), STALE_WM_H, stale),
                "水位不动＝采集停了或该群长期无消息，查定时任务 enabled/nextRunAt" if stale else "")
    except Exception as e:
        add("WARN", "G 时效", "水位线读不了：%s" % str(e)[:80])

    # H 规范漂移（复用 skill 侧脚本，不重写判据）
    if DIFF.is_file():
        try:
            r = subprocess.run([sys.executable, "-X", "utf8", str(DIFF), "--db", str(DB), "--json", "--strict"],
                               cwd=str(DIFF.parent), capture_output=True, timeout=120)
            d = json.loads(r.stdout or "{}")
            hard = bool(d.get("broken_links")) or bool(d.get("target_mismatch"))
            add("FAIL" if hard else "PASS", "H 规范",
                "规范缩写行 %s、alias_scope %s、待登记 %d、库里未写 %d、冲突 %d、断链 %d"
                % (d.get("n_spec_rows"), d.get("n_alias_rows"), len(d.get("only_in_spec") or []),
                   len(d.get("only_in_db") or []), len(d.get("target_mismatch") or []),
                   len(d.get("broken_links") or [])),
                "断链/冲突必须修（规范与库两张皮）；待登记去「配置端 → 别名管理」补"
                if hard or d.get("only_in_spec") else "")
        except Exception as e:
            add("WARN", "H 规范", "spec_alias_diff 跑不了：%s" % str(e)[:100])
    else:
        add("WARN", "H 规范", "找不到 spec_alias_diff.py，规范漂移这项跳过")

    return {"generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "db": str(DB),
            "items": items, "fail": sum(1 for x in items if x["level"] == "FAIL"),
            "warn": sum(1 for x in items if x["level"] == "WARN")}


def human(rep):
    L = ["# 台账库体检 %s" % rep["generated_at"], ""]
    L.append("库：%s" % rep.get("db"))
    L.append("FAIL %d 项 · WARN %d 项" % (rep.get("fail", 0), rep.get("warn", 0)))
    L.append("")
    mark = {"PASS": "✅", "WARN": "⚠️", "FAIL": "❌"}
    for x in rep["items"]:
        L.append("%s **%s** ｜ %s" % (mark.get(x["level"], "?"), x["dim"], x["msg"]))
        if x["advice"]:
            L.append("   → %s" % x["advice"])
    return "\n".join(L)


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="台账库维护与检查（只读）")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()
    rep = check()
    print(json.dumps(rep, ensure_ascii=False, indent=1) if a.json else human(rep))
    sys.exit(rep.get("fail", 0))
