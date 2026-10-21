# -*- coding: utf-8 -*-
"""db_health.py —— 台账库维护与检查（只读；唯一的写操作是可选的 --vacuum，默认不执行）。

维度（自己定的，原则是"台账已有的判据不重写"）：
  A 服务与可用性    8795 是否应答、/api/health/live 能否读（不含业务数据）
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
from audit_connection import backend, connect

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
FRAG_WARN = 0.20          # 空闲页占比阈值
FRAG_SIZE_MIN = 200e6     # 且库文件超过这个量级 —— 两者都满足才建议 VACUUM（做它要停服务，小库不值得）
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


def check_postgres():
    """One enforced read-only snapshot; no SQLite files or startup migrations."""
    import manualdb
    import manual_server
    items = []
    def add(level, dim, msg, advice=""):
        items.append(dict(level=level, dim=dim, msg=msg, advice=advice))
    c = connect("postgres")
    try:
        add("PASS", "A 服务", "当前服务后端 PostgreSQL；审查事务只读")
        master = manualdb.Master(c)
        audit = manualdb.audit_all(c, master)
        issues = manualdb.audit_master(c, master)
        add("WARN" if audit.get("count") or audit.get("integrity_count") or issues.get("issues") else "PASS",
            "B 体检", "标准化违规 %s；完整性提示 %s；主数据问题 %s" %
            (audit.get("count", 0), audit.get("integrity_count", 0), len(issues.get("issues") or [])))
        for issue in (issues.get("issues") or [])[:3]:
            add("WARN", "B 主数据证据", json.dumps(issue, ensure_ascii=False))
        for issue in audit.get("integrity") or []:
            add("WARN", "B 完整性证据", "%s：%s" % (issue["type"], issue["count"]), issue.get("detail", ""))
        for r in c.execute("SELECT record_type, count(*) FROM freight_items GROUP BY record_type"):
            add("PASS", "C 规模", "%s %s 行" % (r[0], r[1]))
        states = {r[0]: r[1] for r in c.execute("SELECT review_status,count(*) FROM pending_records GROUP BY review_status")}
        add("PASS", "C 积压", json.dumps(states, ensure_ascii=False))
        oldest = c.execute("SELECT min(created_at) FROM pending_records WHERE review_status='awaiting'").fetchone()[0]
        if oldest:
            hours = (datetime.now() - datetime.strptime(oldest[:19], "%Y-%m-%d %H:%M:%S")).total_seconds() / 3600
            add("WARN" if hours > STALE_PENDING_H else "PASS", "C 积压", "最老待审 %.1f 小时" % hours)
        for table in ("ports", "carriers", "routes", "port_groups", "senders", "chat_groups", "operators"):
            n = c.execute("SELECT count(*) FROM " + table).fetchone()[0]
            add("PASS" if n else "WARN", "D 主数据", "%s %s 行" % (table, n))
        size = c.execute("SELECT pg_database_size(current_database())").fetchone()[0]
        add("PASS", "E 存储", "PostgreSQL %.1f MB；不套用 SQLite WAL/碎片判据" % (size / 1e6))
        references = {}
        def image_ref(value, entry_id):
            for name in manualdb.split_list(value):
                references.setdefault(name, []).append(entry_id)
        for r in c.execute("SELECT entry_id,image_file FROM freight_items"):
            image_ref(r[1], r[0])
        for r in c.execute("SELECT entry_id,payload FROM pending_records WHERE review_status='awaiting'"):
            p = json.loads(r[1] or "{}")
            image_ref(p.get("图片名") or p.get("运价图片") or p.get("舱位图"), r[0])
        missing = [{"reference": name, "affected_rows": len(ids), "entry_ids": ids[:3]}
                   for name, ids in references.items() if manual_server._resolve_image(name)[0] is None]
        add("WARN" if missing else "PASS", "F 图片", "正式台账及待审核唯一引用 %d，无法解析 %d" % (len(references), len(missing)),
            json.dumps(missing[:3], ensure_ascii=False) if missing else "")
        status_file = HERE / "postgres-backup-status.json"
        if status_file.is_file():
            status = json.loads(status_file.read_text(encoding="utf-8"))
            backup = Path(status.get("path") or "")
            present = status.get("ok") and backup.is_file() and backup.stat().st_size == status.get("bytes")
            age = (datetime.now() - datetime.fromisoformat(status["created_at"])).total_seconds() / 86400 if present else None
            add("PASS" if present and age <= STALE_SNAPSHOT_D else "WARN", "G 备份",
                "PostgreSQL 最近备份 %.1f 天前；仅核对状态与文件，未做恢复演练" % age if present else "最新备份状态或文件校验失败")
        else:
            add("WARN", "G 备份", "没有 PostgreSQL 备份状态证据")
        if WM.is_file():
            wm = json.loads(WM.read_text(encoding="utf-8"))
            ages = [(datetime.now() - datetime.strptime(t, "%Y-%m-%d %H:%M:%S")).total_seconds() / 3600 for t in wm.values()]
            stale = sum(age > STALE_WM_H for age in ages)
            add("WARN" if stale else "PASS", "G 时效", "%d 群水位，%d 群超过 %d 小时；水位不能单独证明拉取完整" % (len(ages), stale, STALE_WM_H))
        else:
            add("WARN", "G 时效", "水位文件不可用")
        # Reuse the spec comparison with aliases from this read-only snapshot.
        sys.path.insert(0, str(DIFF.parent))
        import spec_alias_diff
        aliases = {(r[0], r[1], r[2].strip().lower()): r[3] for r in c.execute("SELECT scope_group,kind,alias,target FROM alias_scope")}
        drift = spec_alias_diff.diff(aliases=aliases)
        hard = len(drift.get("broken_links") or []) + len(drift.get("target_mismatch") or [])
        add("WARN" if hard or drift.get("only_in_spec") else "PASS", "H 规范",
            "冲突/断链 %d；规范待登记 %d" % (hard, len(drift.get("only_in_spec") or [])))
        return dict(generated_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"), backend="postgres",
                    db="PostgreSQL (current service)", items=items, image_missing=missing,
                    fail=sum(x["level"] == "FAIL" for x in items), warn=sum(x["level"] == "WARN" for x in items))
    finally:
        c.close()


def check():
    if backend() == "postgres":
        return check_postgres()
    items = []          # (级别, 维度, 结论, 建议)

    def add(level, dim, msg, advice=""):
        items.append({"level": level, "dim": dim, "msg": msg, "advice": advice})

    # A 服务
    stats, err = _get("/api/health/live")
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
        worth = frag > FRAG_WARN and size > FRAG_SIZE_MIN   # 小库的空闲页不值得折腾 VACUUM（要停服务）
        add("WARN" if worth else "PASS", "E 存储",
            "库 %.1f MB，空闲页占比 %.1f%%（%d/%d 页）%s"
            % (size / 1e6, 100 * frag, fl, pc, "" if worth else "（未到需要 VACUUM 的量级）"),
            "碎片 %.0f%% 且库已 %.0f MB：等没有会话在写的时候 stop-manual → VACUUM → 启动，顺路做一次"
            % (100 * frag, size / 1e6) if worth else "")
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
    try:
        rep = check()
    except Exception as exc:
        # Never expose connection credentials or report a false empty ledger.
        rep = {"generated_at": datetime.now().isoformat(), "fail": 1, "warn": 0,
               "items": [{"level": "FAIL", "dim": "审查", "msg": "审查失败，未回退旧库：" + type(exc).__name__, "advice": "检查服务及只读数据库连接"}]}
    print(json.dumps(rep, ensure_ascii=False, indent=1) if a.json else human(rep))
    sys.exit(rep.get("fail", 0))
