# -*- coding: utf-8 -*-
"""heartbeat_perf.py —— 心跳拉取效率与稳定性体检（只读，不改任何文件、不连写接口）。

为什么不信 state.json：pipeline 每轮 DONE 后，下一次 next 会把当天 state 重置成 PULL，
所以晚上复盘时 state.json 里只剩"当前这一轮"，当天的产出早被覆盖（实测 0918：451 条已入库，
state 却显示 pulled_total=None）。本脚本改用**留得住的痕迹**重建每轮：

  · run_<MMDD>/raw/*           文件 mtime 聚类（间隔 > ROUND_GAP 分钟算新一轮）→ 每轮 dws 次数与时刻
  · llm_out_<MMDD>/*.json      mtime 落在哪一轮窗口 → 每轮判定文件数
  · ocr_cache/*.json           mtime → 每轮识图张数
  · 台账 pending_records.created_at（只读 sqlite）→ 每轮真正入库多少条、多少 failed
  · watermark.json             水位线新鲜度
  · --runlogs <file>           产品侧 runAt/durationMs（agent 用 qw_query qwenwork.cron.runlogs 存下来）
                               → 真实耗时、非 ok 轮次、长轮次吃掉几个排程点
  · state.json                 只当"当前轮快照"展示，并标注它可能已被重置

    python ops/heartbeat_perf.py                 今天
    python ops/heartbeat_perf.py --days 7 --runlogs runs.json
    python ops/heartbeat_perf.py --json
"""
import argparse
import glob
import json
import os
import sqlite3
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

HERE = Path(__file__).resolve().parent
DB_DIR = HERE.parent
PROJ = DB_DIR.parent
RUNTIME = Path(os.environ.get("FRT_RUNTIME_DIR") or (PROJ / "Skill" / "heartbeat-data"))
WORK = RUNTIME / ".work"
DB = Path(os.environ.get("FRT_DB") or (DB_DIR / "freight_manual.db"))

# 阈值集中在这里，调策略改这一处
ROUND_GAP_MIN = 5           # raw 文件 mtime 间隔超过它就认为是新一轮
LONG_BY_SEG = {"低频段": 60, "高频段": 10}   # 超过对应节奏 = 吃掉后续排程点
FAILED_RATE_WARN = 0.30
HEAVY_JUDGE = 8             # 单轮判定文件数
HEAVY_IMAGE = 4             # 单轮识图张数
SLOTS_EATEN_WARN = 1


def _mtimes(pattern):
    out = []
    for p in glob.glob(pattern):
        try:
            out.append((datetime.fromtimestamp(os.path.getmtime(p)), p))
        except OSError:
            pass
    return sorted(out)


def _load_json(p, default=None):
    try:
        return json.loads(Path(p).read_text(encoding="utf-8"))
    except Exception:
        return default


def cluster(times, gap_min=ROUND_GAP_MIN):
    """把一串时刻按间隔切成轮次，返回 [(start, end, n), ...]。"""
    rounds, cur = [], []
    for t in times:
        if cur and (t - cur[-1]).total_seconds() > gap_min * 60:
            rounds.append((cur[0], cur[-1], len(cur)))
            cur = []
        cur.append(t)
    if cur:
        rounds.append((cur[0], cur[-1], len(cur)))
    return rounds


def ledger_by_window(date_iso):
    """台账侧：当天 pending 的写入时刻与校验结果（只读连接）。"""
    if not DB.is_file():
        return []
    c = sqlite3.connect("file:%s?mode=ro" % str(DB).replace("\\", "/"), uri=True)
    try:
        rows = c.execute(
            "select created_at, check_status, kind from pending_records where created_at like ?",
            (date_iso + "%",)).fetchall()
    except sqlite3.Error:
        return []
    finally:
        c.close()
    out = []
    for ca, cs, kd in rows:
        try:
            t = datetime.strptime(ca[:19], "%Y-%m-%d %H:%M:%S")
        except ValueError:
            continue
        out.append({"t": t, "check": cs, "kind": kd})
    return out


def _iso_from_mmdd(date):
    """.work 目录名是 MMDD，不是 YYMM——之前按年份拼出过 "2009-18-"，台账一条都匹配不上。
    跨年兜底：拼出的日期若晚于今天，说明是去年 12 月的轮次。"""
    now = datetime.now()
    d = datetime(now.year, int(date[:2]), int(date[2:4]))
    if d.date() > now.date():
        d = d.replace(year=now.year - 1)
    return d.strftime("%Y-%m-%d")


def rounds_of(date):
    """一天的轮次重建：dws 聚类为主轴，判定/识图/入库按时间窗归属。"""
    rd = WORK / ("run_%s" % date)
    raw = _mtimes(str(rd / "raw" / "*"))
    rounds = cluster([t for t, _ in raw])
    judged = [t for t, _ in _mtimes(str(WORK / ("llm_out_%s" % date) / "*.json"))]
    ocr = [t for t, _ in _mtimes(str(WORK / "ocr_cache" / "*.json"))]
    date_iso = _iso_from_mmdd(date)
    ledger = ledger_by_window(date_iso)
    out = []
    for i, (start, end, n_dws) in enumerate(rounds):
        nxt = rounds[i + 1][0] if i + 1 < len(rounds) else datetime.now()
        win = [start, nxt]
        inwin = lambda ts: win[0] <= ts < win[1]
        subs = [x for x in ledger if inwin(x["t"])]
        out.append({
            "start": start.strftime("%H:%M:%S"), "end": end.strftime("%H:%M:%S"),
            "dws_calls": n_dws,
            "judged_files": sum(1 for t in judged if inwin(t)),
            "ocr_images": sum(1 for t in ocr if inwin(t)),
            "submitted": len(subs),
            "failed": sum(1 for x in subs if x["check"] == "failed"),
            "kinds": {k: sum(1 for x in subs if x["kind"] == k) for k in {x["kind"] for x in subs}},
        })
    total = {"rounds": len(out), "dws_calls": sum(r["dws_calls"] for r in out),
             "judged_files": sum(r["judged_files"] for r in out),
             "ocr_images": sum(r["ocr_images"] for r in out),
             "submitted": sum(r["submitted"] for r in out),
             "failed": sum(r["failed"] for r in out)}
    st = _load_json(rd / "state.json", {}) or {}
    total["state_now"] = {"stage": st.get("stage"), "pulled_total": st.get("pulled_total"),
                          "records_submitted": st.get("records_submitted"),
                          "note": "仅当前轮快照，DONE 后会被下一轮重置，不能当全天数据"}
    total["run_dir_exists"] = rd.is_dir()
    return {"date": date, "rounds": out, "totals": total}


def watermark_age():
    wm = _load_json(RUNTIME / "watermark.json", {}) or {}
    now = datetime.now()
    ages = {}
    for slug, ts in wm.items():
        try:
            ages[slug] = round((now - datetime.strptime(ts, "%Y-%m-%d %H:%M:%S")).total_seconds() / 3600, 1)
        except ValueError:
            pass
    stale = {k: v for k, v in ages.items() if v > 26}
    return {"groups": len(ages), "oldest_hours": max(ages.values()) if ages else None,
            "newest_hours_ago": min(ages.values()) if ages else None, "stale_over_26h": stale}


def runlog_stats(path):
    logs = _load_json(path, [])
    if isinstance(logs, dict):
        logs = (logs.get("data") or {}).get("logs") or []
    if not logs:
        return None
    seg = {}
    for x in logs:
        name = x.get("taskName") or ""
        key = "高频段" if "高频" in name else ("低频段" if "低频" in name else (name or "未分段"))
        d = seg.setdefault(key, {"rounds": 0, "durations": [], "errors": 0, "slots_eaten": 0,
                                 "first": None, "last": None})
        dur = (x.get("durationMs") or 0) / 60000.0
        d["rounds"] += 1
        d["durations"].append(dur)
        if x.get("status") != "ok":
            d["errors"] += 1
        cad = LONG_BY_SEG.get(key, 10)
        if dur > cad:
            d["slots_eaten"] += int(dur // cad)
        if x.get("runAt"):
            s = datetime.fromtimestamp(x["runAt"] / 1000).strftime("%m-%d %H:%M")
            d["first"] = min(d["first"], s) if d["first"] else s
            d["last"] = max(d["last"], s) if d["last"] else s
    for d in seg.values():
        ds = sorted(d["durations"])
        d["p50_min"] = round(ds[len(ds) // 2], 1) if ds else None
        d["p90_min"] = round(ds[min(len(ds) - 1, int(len(ds) * 0.9))], 1) if ds else None
        d["max_min"] = round(ds[-1], 1) if ds else None
        del d["durations"]
    return seg


def advise(days, wm, rlog):
    out = []
    today = days[-1] if days else {}
    tot = today.get("totals") or {}
    sub, failed = tot.get("submitted") or 0, tot.get("failed") or 0
    if sub and failed / sub > FAILED_RATE_WARN:
        out.append("今天入库 %d 条、failed %d 条（%.0f%%）：瓶颈在值域不在频率——先把主数据缺口和别名补上，"
                   "排程调密只会把更多 failed 堆到人工侧" % (sub, failed, 100.0 * failed / sub))
    for r in today.get("rounds") or []:
        if r["judged_files"] > HEAVY_JUDGE:
            out.append("%s 那轮判定 %d 个文件（阈值 %d）：判定项数直接决定墙钟，建议给单轮设上限，"
                       "超出留到下一轮" % (r["start"], r["judged_files"], HEAVY_JUDGE))
            break
    for r in today.get("rounds") or []:
        if r["ocr_images"] > HEAVY_IMAGE:
            out.append("%s 那轮识图 %d 张（阈值 %d）：识图是单轮最贵的一步，可考虑图表型群单独分片"
                       % (r["start"], r["ocr_images"], HEAVY_IMAGE))
            break
    if rlog:
        for k, d in rlog.items():
            if d["slots_eaten"] >= SLOTS_EATEN_WARN:
                out.append("%s：有轮次长到吃掉 %d 个排程点（最长 %.1f 分钟）——名义间隔 %d 分钟形同虚设，"
                           "先缩短单轮再谈加密度" % (k, d["slots_eaten"], d["max_min"], LONG_BY_SEG.get(k, 10)))
            if d["errors"]:
                out.append("%s：%d 轮非 ok，去 runlogs 看 error 字段" % (k, d["errors"]))
    else:
        out.append("没喂 --runlogs：真实耗时与排队延迟算不出来，只能给磁盘侧计数。"
                   "审查任务应先用 qw_query qwenwork.cron.runlogs 存一份 JSON 再调本脚本")
    stale = wm.get("stale_over_26h") or {}
    if stale:
        out.append("水位线超 26 小时未推进的群 %d 个：%s" % (len(stale), ", ".join(sorted(stale)[:6])))
    if not tot.get("run_dir_exists", True) and not (today.get("rounds") or []):
        out.append("今天没有可重建的轮次：任务可能没跑（查 enabled 与 nextRunAt），或 raw 目录被清过")
    if not out:
        out.append("没有触发任何阈值：节奏与产出都在设定范围内")
    return out


def build(dates, runlogs_path):
    days = [rounds_of(d) for d in dates]
    wm = watermark_age()
    rlog = runlog_stats(runlogs_path) if runlogs_path else None
    return {"generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "days": days,
            "watermark": wm, "runlog": rlog, "advice": advise(days, wm, rlog),
            "thresholds": {"ROUND_GAP_MIN": ROUND_GAP_MIN, "FAILED_RATE_WARN": FAILED_RATE_WARN,
                           "HEAVY_JUDGE": HEAVY_JUDGE, "HEAVY_IMAGE": HEAVY_IMAGE}}


def human(rep):
    L = ["# 心跳效率体检 %s" % rep["generated_at"], ""]
    wm = rep["watermark"]
    L.append("水位线：%d 群，最新 %.1f 小时前 / 最旧 %.1f 小时前%s"
             % (wm["groups"], wm["newest_hours_ago"] or 0, wm["oldest_hours"] or 0,
                "；超 26h 未推进 %d 群" % len(wm["stale_over_26h"]) if wm["stale_over_26h"] else ""))
    for d in rep["days"]:
        t = d["totals"]
        L.append("\n## %s：%d 轮 · dws %d 次 · 判定 %d · 识图 %d · 入库 %d（failed %d）"
                 % (d["date"], t["rounds"], t["dws_calls"], t["judged_files"], t["ocr_images"],
                    t["submitted"], t["failed"]))
        if d["rounds"]:
            L.append("| 起 | 止 | dws | 判定 | 识图 | 入库 | failed | 类型 |")
            L.append("|---|---|---|---|---|---|---|---|")
            for r in d["rounds"]:
                L.append("| %s | %s | %d | %d | %d | %d | %d | %s |" % (
                    r["start"], r["end"], r["dws_calls"], r["judged_files"], r["ocr_images"],
                    r["submitted"], r["failed"], json.dumps(r["kinds"], ensure_ascii=False)))
        else:
            L.append("（当天没有可重建的轮次）")
        sn = t.get("state_now") or {}
        L.append("> state.json 当前轮快照：stage=%s pulled=%s submitted=%s（%s）"
                 % (sn.get("stage"), sn.get("pulled_total"), sn.get("records_submitted"), sn.get("note")))
    if rep["runlog"]:
        L.append("\n## 产品侧执行记录（真实耗时）")
        for k, d in rep["runlog"].items():
            L.append("- **%s**：%d 轮，p50 %s / p90 %s / 最长 %s 分钟，非 ok %d 轮，吃掉排程点 %d 个（%s ~ %s）"
                     % (k, d["rounds"], d["p50_min"], d["p90_min"], d["max_min"], d["errors"],
                        d["slots_eaten"], d["first"], d["last"]))
    L.append("\n## 判断与建议")
    for i, a in enumerate(rep["advice"], 1):
        L.append("%d. %s" % (i, a))
    return "\n".join(L)


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="心跳拉取效率与稳定性体检（只读）")
    ap.add_argument("--date", help="MMDD，默认今天")
    ap.add_argument("--days", type=int, default=1)
    ap.add_argument("--runlogs", help="qw_query qwenwork.cron.runlogs 的结果 JSON 文件")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()
    dates = [a.date] if a.date else [(datetime.now() - timedelta(days=i)).strftime("%m%d")
                                     for i in range(a.days - 1, -1, -1)]
    rep = build(dates, a.runlogs)
    print(json.dumps(rep, ensure_ascii=False, indent=1) if a.json else human(rep))
