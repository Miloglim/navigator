# -*- coding: utf-8 -*-
"""局域网版接口回归：拿 freight_manual.db 的副本起一个临时端口跑，不污染真库。

    python manual_check.py
"""
from __future__ import annotations

import json
import shutil
import socket
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
HERE = Path(__file__).resolve().parent
PY = sys.executable
SRC_DB = HERE / "freight_manual.db"
TEST_DB = HERE / "_check_manual.db"
PORT = 8791
BASE = f"http://127.0.0.1:{PORT}"
FAILS: list = []


def ck(name, ok, detail=""):
    print(("  OK   " if ok else "  FAIL ") + name + ("  | " + str(detail) if detail else ""))
    if not ok:
        FAILS.append(name)


def call(path, method="GET", body=None, token=None):
    req = urllib.request.Request(BASE + path, method=method)
    if token:
        req.add_header("x-push-token", token)
    data = None
    if body is not None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        req.add_header("Content-Type", "application/json; charset=utf-8")
    try:
        with urllib.request.urlopen(req, data, timeout=20) as r:
            return r.status, json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")
        try:
            return e.code, json.loads(raw)
        except ValueError:
            return e.code, {"raw": raw[:200]}


def wait_port(sec=15.0):
    end = time.time() + sec
    while time.time() < end:
        with socket.socket() as s:
            s.settimeout(0.4)
            if s.connect_ex(("127.0.0.1", PORT)) == 0:
                return True
        time.sleep(0.3)
    return False


def main() -> int:
    if not SRC_DB.exists():
        print("先跑 pull_from_cloud.py 建库"); return 2
    shutil.copyfile(SRC_DB, TEST_DB)
    # 副本会带上生产库里的真实待审核条目；用例靠 limit 窗口找回自己暂存的那几条，
    # 453 条 failed 排在前面会把它们挤出窗口 → 假红。回归起点清空待审（只动副本，不碰真库）
    _c = sqlite3.connect(str(TEST_DB))
    try:
        _c.execute("DELETE FROM pending_records")
        # 作用域别名种子：只在服务启动前写，避开 Master 缓存 TTL
        _c.execute("INSERT OR REPLACE INTO alias_scope(alias,kind,target,scope_group,note,created_at) "
                   "VALUES('TESTX','pod','Santos','作用域测试群','回归用例','2026-09-12 00:00:00')")
        _c.commit()
    finally:
        _c.close()
    proc = subprocess.Popen([PY, str(HERE / "manual_server.py"), "--host", "127.0.0.1",
                             "--port", str(PORT), "--db", str(TEST_DB)],
                            cwd=str(HERE), stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)
    try:
        if not wait_port():
            ck("服务起来了", False, "端口没开")
            return 1
        print("== 局域网版回归 ==")

        st, html = None, (HERE / "web" / "index.html").read_text(encoding="utf-8")
        ck("前端是同一份（含候选列表/区间控件）", "data-ac=" in html and "openRangePicker" in html)
        ck("前端含批量导入（固定列序 18 列）",
           "function btParse" in html and "批量导入" in html and html.count("'船司', '起运港', '目的港', '20GP'") >= 1)
        ck("前端本机免口令（有 local-token 探测）", "/api/local-token" in html)

        st, tk = call("/api/local-token")
        token = tk.get("token") or ""
        ck("本机回环拿到令牌", tk.get("ok") and len(token) > 10, token[:8] + "…")

        st, m = call("/api/master")
        ck("主数据已迁过来", m.get("ok") and len(m["ports"]) > 1000 and len(m["carriers"]) == 50,
           f"ports={len(m['ports'])} carriers={len(m['carriers'])} routes={len(m['routes'])}")
        ck("规范名全英文无 Pt 尾缀",
           not any(p["n"].endswith(" Pt") for p in m["ports"])
           and not any("\u4e00" <= c <= "\u9fff" for p in m["ports"] for c in p["n"]))
        pols = [p["n"] for p in m["ports"] if p["k"] == "both"]
        ck("起运港候选是中国基本港+香港（含 Tianjin New Port / Yantian / Shekou）",
           {"Tianjin New Port", "Yantian", "Shekou", "Hong Kong"} <= set(pols), len(pols))
        st, full = call("/api/master?full=1")
        carr = full["raw"]["carriers"]
        ck("船司按业务方序号排（本地有 sort 列）",
           [c["code"] for c in carr[:3]] == ["MSK", "MSC", "CMA"] and carr[0].get("sort") == 1,
           [(c["code"], c.get("sort")) for c in carr[:3]])
        ck("响应带三份频次", all(k in m for k in ("groupFreq", "senderFreq", "carrierFreq")))

        print("\n== 1.5) 港群双向展开 ==")
        grps = {g["n"]: g for g in (m.get("portGroups") or [])}
        ck("港群已灌进本地库（主数据非空即可，数量以系统为准）", len(grps) >= 1 and "NEB" in grps, sorted(grps))
        neb = grps.get("NEB", {}).get("m") or ""
        ck("NEB 成员含 Pecém 等 8 港", "Pecém" in neb and len([x for x in neb.split(",") if x]) == 8, neb)

        def prec(pod, price):
            return {"起运港": "天津", "目的港": pod, "船司": "达飞", "柜型": "40HQ/HC",
                    "海运费USD": price, "有效期起": "2026-09-10", "有效期止": "2026-09-16",
                    "船期ETD": "2026-09-18", "来源群": "自检群", "发送人": "自检",
                    "消息原文": "港群自检 " + pod}
        # 先清空存量运价行：本副本库是从生产 freight_manual.db 拷来的，可能已带真实数据
        # （如某条 Santos 当前生效），不清会让下面"非成员港 Santos 不串群价"因基线数据误报红。
        # 副本基线可能几百条 rates，一次 limit 拿不完 → 分页 while 直到 rows 空。
        def _clear_table(table, path):
            for _ in range(20):  # 最多 20 轮防死循环
                st0, q0 = call(path + "?limit=200")
                rows0 = q0.get("rows", [])
                if not rows0:
                    return
                for x in rows0:
                    call("/api/record/delete", "POST", {"table": table, "content_key": x["content_key"]}, token)
            raise RuntimeError(f"{table} 分页清理 20 轮仍非空，检查 API 或权限")
        _clear_table("rates", "/api/rates")
        _clear_table("space", "/api/space")
        st, g1 = call("/api/push", "POST", {"table": "rates", "records": [prec("北巴", "2900")]}, token)
        ck("输入「北巴」按群名存一条（不拆成 8 港）", g1.get("accepted") == 1, g1)
        st, g2 = call("/api/push", "POST", {"table": "rates", "records": [prec("Pecém", "3100")]}, token)
        ck("Pecém 单港价写入", g2.get("accepted") == 1, g2)
        st, q = call("/api/rates?pod=Pec%C3%A9m&limit=10")
        ck("筛成员港 Pecém 同时拿到群价与单港价",
           {x["pod"] for x in q.get("rows", [])} == {"NEB", "Pecém"}, sorted({x["pod"] for x in q.get("rows", [])}))
        st, q = call("/api/rates?pod=NEB&limit=10")
        ck("筛群名 NEB 也带出成员精确价", {x["pod"] for x in q.get("rows", [])} == {"NEB", "Pecém"})
        st, q = call("/api/rates?pod=Santos&limit=10")
        ck("筛非成员港 Santos 不串群价", not q.get("rows"), q.get("total"))
        st, gg = call("/api/rates_grouped?pod=Pec%C3%A9m&limit=10")
        ck("折叠视图同样展开港群", bool(gg.get("groups")), len(gg.get("groups") or []))
        _clear_table("rates", "/api/rates")
        ck("港群测试行已清干净", call("/api/stats")[1].get("ratesTotal") == 0)

        REC = {"起运港": "天津", "目的港": "santos,brazil/曼萨尼约", "船司": "达飞", "航线": "南美东岸",
               "柜型": "40HC", "海运费USD": "2850", "目免": "7", "亏舱费": "300", "T/T航程": "35",
               "有效期起": "2026-09-10", "有效期止": "2026-09-16", "船期ETD": "2026-09-18",
               "来源群": "自检群", "发送人": "自检员", "消息时间": "2020-01-01 00:00",
               "消息原文": "自检：达飞 天津-santos/曼萨尼约 40HC 2850"}
        st, r = call("/api/push", "POST", {"table": "rates", "records": [REC]}, token)
        ck("写入成功 + 多港拆两行", r.get("ok") and r.get("accepted") == 2, r.get("accepted"))
        st, q = call("/api/rates?limit=10")
        rows = q.get("rows") or []
        ck("起运港归一 天津→Tianjin New Port", rows and rows[0]["pol"] == "Tianjin New Port",
           rows[0]["pol"] if rows else "无行")
        _master_pods = {p["name"] for p in (full.get("raw", {}).get("ports") or []) if p.get("name")}
        _pods = sorted(x["pod"] for x in rows)
        ck("目的港拆分且归一到主数据规范名（以 master 为准，不写死）",
           len(_pods) == 2 and all(v in _master_pods for v in _pods) and "Santos" in _pods, _pods)
        ck("船司/航线/柜型归一", rows and rows[0]["carrier"] == "CMA"
           and rows[0]["route"] == "南美东" and rows[0]["container_type"] == "40HQ/HC",
           (rows[0]["carrier"], rows[0]["route"], rows[0]["container_type"]) if rows else "")
        ck("Freetime/T-T 落库为整数", rows and int(rows[0]["free_days"]) == 7
           and int(rows[0]["transit_days"]) == 35,
           (rows[0]["free_days"], rows[0]["transit_days"]) if rows else "")
        ck("录入时间被强制覆盖", rows and not str(rows[0]["message_time"]).startswith("2020"),
           rows[0]["message_time"] if rows else "")

        st, bad = call("/api/push", "POST", {"table": "rates", "records": [dict(REC, **{"海运费USD": "2850.5"})]}, token)
        ck("小数海运费被闸门拒绝", bad.get("ok") and bad.get("accepted") == 0
           and any("整数" in e.get("msg", "") for rj in bad.get("rejected", []) for e in rj.get("errors", [])),
           bad.get("rejected"))
        st, un = call("/api/push", "POST", {"table": "rates", "records": [dict(REC, **{"目的港": "Nowhereville", "海运费USD": "1999"})]}, token)
        ck("未匹配主数据被拒绝（不再原样入库）", un.get("ok") and un.get("accepted") == 0
           and any(any(e.get("input") == "Nowhereville" for e in rj.get("errors", [])) for rj in un.get("rejected", [])),
           un.get("rejected"))

        key = rows[0]["content_key"]
        st, rv = call("/api/record/revise", "POST", {"table": "rates", "content_key": key,
                                                    "fields": {"海运费USD": "2780", "船期ETD": "2026-09-25"}}, token)
        chg = {c["col"]: (c["from"], c["to"]) for c in rv.get("changes", [])}
        ck("留痕更新 + 摘要不含录入时间噪声", rv.get("ok") and chg.get("freight_usd") == ("2850", "2780")
           and "message_time" not in chg, chg)
        st, ov = call("/api/rates?status=%E5%B7%B2%E8%A2%AB%E8%A6%86%E7%9B%96&limit=5")
        ck("旧行已标已被覆盖", any(x["content_key"] == key for x in ov.get("rows", [])))
        st, g = call("/api/rates_grouped?limit=20")
        ck("新行带 supersedes 指回旧版",
           sum(1 for x in g.get("rows", []) if x.get("supersedes") == key) == 1)
        st, same = call("/api/record/revise", "POST", {"table": "rates", "content_key": key, "fields": {"备注": ""}}, token)
        ck("没有实质改动被拒", not same.get("ok"), same.get("error"))

        st, ops = call("/api/push", "POST", {"table": "rates", "records": [REC]}, "wrong-token")
        ck("错口令 401", st == 401, st)

        st, ms = call("/api/master/save", "POST",
                      {"table": "senders", "row": {"name": "张三", "note": "自检"}}, token)
        ck("主数据可后台新增", ms.get("ok") and ms.get("action") == "created", ms)
        st, m2 = call("/api/master")
        ck("新增后候选立刻跟上", "张三" in (m2.get("senders") or []))
        st, md = call("/api/master/delete", "POST", {"table": "senders", "name": "张三"}, token)
        ck("主数据可删", md.get("ok") and md.get("deleted") == 1, md)

        st, s1 = call("/api/stats")
        ck("统计口径正常", s1.get("ok") and s1.get("ratesTotal", 0) >= 3 and s1.get("groups", 0) >= 1,
           {k: s1.get(k) for k in ("ratesTotal", "ratesCurrent", "groups", "carriers")})

        st, ex = call("/api/export")
        ck("整库导出可用", ex.get("ok") and len(ex.get("ports", [])) > 1000 and "freight_rates" in ex,
           len(ex.get("freight_rates", [])))

        print("\n== 6) 标准化闸门 / 自动更新 / 待审核 / 体检 ==")
        st, vd = call("/api/validate", "POST", {"table": "rates", "records": [
            dict(REC), dict(REC, **{"船司": "NoSuchLine"})]})
        ck("validate 逐行给结论（一好一坏）",
           vd.get("ok") and vd.get("good") == 1 and vd.get("bad") == 1,
           {"good": vd.get("good"), "bad": vd.get("bad")})

        ident = {"起运港": "天津", "目的港": "Santos", "船司": "达飞", "柜型": "20GP",
                 "海运费USD": "100", "有效期起": "2026-09-10", "有效期止": "2026-09-16",
                 "来源群": "更新自检群", "发送人": "自检", "消息原文": "AUTO-SUPERSEDE-MARK"}
        st, p1 = call("/api/push", "POST", {"table": "rates", "records": [ident]}, token)
        st, p2 = call("/api/push", "POST", {"table": "rates", "records": [dict(ident, **{"海运费USD": "150"})]}, token)
        _, cur = call("/api/rates?pod=Santos&carrier=CMA&status=%E5%BD%93%E5%89%8D%E7%94%9F%E6%95%88&limit=50")
        ncur = len([x for x in cur.get("rows", []) if (x.get("message_text") or "") == "AUTO-SUPERSEDE-MARK"])
        ck("同港同司同原文再录=更新留痕，当前生效只剩 1 条",
           p1.get("accepted") == 1 and p2.get("revised") == 1 and ncur == 1,
           {"a1": p1.get("accepted"), "r2": p2.get("revised"), "ncur": ncur})

        st, pd = call("/api/pending", "POST", {"kind": "rates", "origin": "agent", "records": [
            dict(ident, **{"消息原文": "AGENT-GOOD-1", "海运费USD": "200"}),
            {"起运港": "天津", "目的港": "NowherePort", "船司": "达飞", "柜型": "20GP",
             "海运费USD": "1", "有效期起": "2026-09-10", "有效期止": "2026-09-16",
             "消息原文": "AGENT-BAD-1"}]}, token)
        ck("agent 暂存进待审核（2 条）", pd.get("ok") and pd.get("staged") == 2, pd)
        _, lst = call("/api/pending?status=awaiting&limit=50")
        rows_pd = lst.get("rows", [])
        good = next((r for r in rows_pd if r.get("check_status") == "passed"
                     and (r.get("payload") or {}).get("消息原文") == "AGENT-GOOD-1"), None)
        badr = next((r for r in rows_pd if r.get("check_status") == "failed"
                     and (r.get("payload") or {}).get("消息原文") == "AGENT-BAD-1"), None)
        ck("待审核区分通过/未过", bool(good) and bool(badr), {"good": bool(good), "bad": bool(badr)})
        if badr:
            _, rj = call("/api/pending/approve", "POST", {"id": badr["id"]}, token)
            ck("未过校验的不能放行", not rj.get("ok"), rj.get("error"))
        if good:
            _, ap = call("/api/pending/approve", "POST", {"id": good["id"]}, token)
            _res = (ap.get("result") or {})
            ck("通过的放行后入库（新增，或同产品身份命中→更新留痕，皆算成功）",
               ap.get("ok") and (_res.get("accepted") == 1 or _res.get("revised") == 1), ap)
        # 合并港口：暂存即按港拆成多条，每港一张工单（不合并）
        st, pm = call("/api/pending", "POST", {"kind": "rates", "origin": "agent", "records": [
            {"起运港": "天津", "目的港": "Santos/曼萨尼约", "船司": "达飞", "柜型": "20GP",
             "海运费USD": "300", "有效期起": "2026-09-10", "有效期止": "2026-09-16",
             "消息原文": "AGENT-MERGE-1"}]}, token)
        ck("合并港口暂存拆成每港一条", pm.get("ok") and pm.get("staged") == 2, pm.get("staged"))
        _, lmp = call("/api/pending?status=awaiting&limit=80")
        merged = [r for r in lmp.get("rows", []) if (r.get("payload") or {}).get("消息原文") == "AGENT-MERGE-1"]
        ck("拆分后无合并港残留（每条 目的港 不含 /）",
           len(merged) == 2 and all("/" not in str((r.get("payload") or {}).get("目的港", "")) for r in merged),
           [ (r.get("payload") or {}).get("目的港") for r in merged ])
        for r in merged:
            call("/api/pending/delete", "POST", {"id": r["id"]}, token)
        # 引用原文：新消息带 ——引用…mID=…—— 指向旧行 → 单向判更新（留痕），卡片徽标同判
        qbase_txt = "QUOTE-BASE 达飞 天津-Santos 20GP 100"
        qbase = {"起运港": "天津", "目的港": "Santos", "船司": "达飞", "柜型": "20GP",
                 "海运费USD": "100", "有效期起": "2026-09-10", "有效期止": "2026-09-16",
                 "消息ID": "QBASE-M1", "消息原文": qbase_txt}
        call("/api/push", "POST", {"table": "rates", "records": [qbase]}, token)
        qupd_txt = "QUOTE-UPD 改价 150\n\n——引用 2026-09-10 10:00 自检 mID=QBASE-M1——\n" + qbase_txt
        qupd = dict(qbase, **{"海运费USD": "150", "消息ID": "QUPD-M1", "消息原文": qupd_txt})
        _, qu = call("/api/push", "POST", {"table": "rates", "records": [qupd]}, token)
        _, ccur = call("/api/rates?pod=Santos&carrier=CMA&status=%E5%BD%93%E5%89%8D%E7%94%9F%E6%95%88&limit=80")
        cur_rows = ccur.get("rows", [])
        base_cur = [x for x in cur_rows if (x.get("message_text") or "") == qbase_txt]
        upd_cur = [x for x in cur_rows if (x.get("message_text") or "") == qupd_txt]
        ck("引用 mID 命中→判更新留痕（新行生效、旧行覆盖）",
           qu.get("revised") == 1 and not base_cur and len(upd_cur) == 1
           and str(upd_cur[0].get("freight_usd")) == "150",
           {"revised": qu.get("revised"), "base_cur": len(base_cur), "upd": len(upd_cur)})
        _, pq = call("/api/pending", "POST", {"kind": "rates", "origin": "agent", "records": [
            dict(qbase, **{"海运费USD": "180", "消息ID": "QUPD-M2",
                           "消息原文": "QUOTE-UPD2\n\n——引用 2026-09-10 10:00 自检 mID=QUPD-M1——\n" + qupd_txt})]}, token)
        _, lq = call("/api/pending?status=awaiting&limit=200")
        qc = next((r for r in lq.get("rows", []) if (r.get("payload") or {}).get("消息ID") == "QUPD-M2"), None)
        ck("暂存带引用记录按被引旧行判更新（卡片徽标 action=update）",
           bool(qc) and qc.get("action") == "update", qc and qc.get("action"))
        if qc:
            call("/api/pending/delete", "POST", {"id": qc["id"]}, token)
        # 引用已被覆盖的老版 → 顺 supersedes 链追到当前版再更新，绝不在历史上分叉
        a_txt = "QCHAIN-A 天津-Santos 20GP 100"
        a = {"起运港": "天津", "目的港": "Santos", "船司": "达飞", "柜型": "20GP", "海运费USD": "100",
             "有效期起": "2026-09-10", "有效期止": "2026-09-16", "消息ID": "QC-A", "消息原文": a_txt}
        call("/api/push", "POST", {"table": "rates", "records": [a]}, token)
        b_txt = "QCHAIN-B 改价 120\n\n——引用 2026-09-10 10:00 自检 mID=QC-A——\n" + a_txt
        call("/api/push", "POST", {"table": "rates", "records": [dict(a, **{"海运费USD": "120", "消息ID": "QC-B", "消息原文": b_txt})]}, token)
        c_txt = "QCHAIN-C 改价 150\n\n——引用 2026-09-10 10:00 自检 mID=QC-A——\n" + a_txt
        _, cq = call("/api/push", "POST", {"table": "rates", "records": [dict(a, **{"海运费USD": "150", "消息ID": "QC-C", "消息原文": c_txt})]}, token)
        _, ccur2 = call("/api/rates?pod=Santos&carrier=CMA&status=%E5%BD%93%E5%89%8D%E7%94%9F%E6%95%88&limit=80")
        cur_txts = {(x.get("message_text") or "") for x in ccur2.get("rows", [])}
        ck("引用已被覆盖的老版→追链到当前版更新、不分叉（当前只剩 C）",
           cq.get("revised") == 1 and a_txt not in cur_txts and b_txt not in cur_txts and c_txt in cur_txts,
           {"revised": cq.get("revised"), "a_cur": a_txt in cur_txts, "b_cur": b_txt in cur_txts, "c_cur": c_txt in cur_txts})
        # 多值查询：pod 多值内部 OR；pol 与 pod 同时多值 → 交集
        call("/api/push", "POST", {"table": "rates", "records": [
            dict(ident, **{"目的港": "Santos", "消息ID": "MV-1", "消息原文": "MV-SANTOS", "海运费USD": "111"}),
            dict(ident, **{"目的港": "Pecém", "消息ID": "MV-2", "消息原文": "MV-PECEM", "海运费USD": "222"})]}, token)
        _, mv = call("/api/rates?pod=Santos,Pec%C3%A9m&limit=50")
        mv_txts = {(x.get("message_text") or "") for x in mv.get("rows", [])}
        ck("目的港多值 OR：Santos,Pecém 两条都命中", {"MV-SANTOS", "MV-PECEM"} <= mv_txts, sorted(mv_txts))
        _, mx = call("/api/rates?pol=Tianjin%20New%20Port&pod=Santos,Pec%C3%A9m&limit=50")
        ck("起运+目的都多值→交集（同 pol 且 pod∈集合）",
           len(mx.get("rows", [])) >= 2 and all(x.get("pol") == "Tianjin New Port"
           and x.get("pod") in ("Santos", "Pecém") for x in mx.get("rows", [])),
           len(mx.get("rows", [])))
        # 同产品身份·待审撤旧留新：连发两次更新，待审只留最新一条
        call("/api/pending", "POST", {"kind": "rates", "origin": "agent", "records": [
            {"起运港": "天津", "目的港": "Santos", "船司": "达飞", "柜型": "20GP", "海运费USD": "500",
             "有效期起": "2026-09-10", "有效期止": "2026-09-20", "船期ETD": "2026-09-22", "消息原文": "DUP-A"}]}, token)
        call("/api/pending", "POST", {"kind": "rates", "origin": "agent", "records": [
            {"起运港": "天津", "目的港": "Santos", "船司": "达飞", "柜型": "20GP", "海运费USD": "560",
             "有效期起": "2026-09-10", "有效期止": "2026-09-20", "船期ETD": "2026-09-22", "消息原文": "DUP-B"}]}, token)
        _, adup = call("/api/pending?status=awaiting&limit=300")
        dups = [r for r in adup.get("rows", []) if (r.get("payload") or {}).get("消息原文") in ("DUP-A", "DUP-B")]
        ck("同产品身份待审撤旧留新（只留最新 DUP-B ¥560）",
           len(dups) == 1 and dups[0]["payload"].get("消息原文") == "DUP-B"
           and str(dups[0]["payload"].get("海运费USD")) == "560",
           [d["payload"].get("消息原文") for d in dups])
        if badr:
            bid = badr["id"]
            # 编辑：修正脏目的港 → 校验状态由 failed 翻 passed
            _, up = call("/api/pending/update", "POST", {"id": bid, "fields": {"目的港": "Santos"}}, token)
            ck("编辑暂存记录后校验翻转（failed→passed）",
               up.get("ok") and up.get("check_status") == "passed", up)
            # 驳回=软释放：移入驳回日志（留原因），行仍在库
            _, rj = call("/api/pending/reject", "POST", {"id": bid, "note": "自检驳回"}, token)
            _, rej = call("/api/pending?status=rejected&limit=50")
            inlog = next((r for r in rej.get("rows", []) if r["id"] == bid), None)
            ck("驳回=移入驳回日志（留原因，行未删）",
               rj.get("ok") and bool(inlog) and inlog.get("note") == "自检驳回",
               {"ok": rj.get("ok"), "inlog": bool(inlog)})
            _, aw3 = call("/api/pending?status=awaiting&limit=50")
            ck("驳回后离开待处理队列", not any(r["id"] == bid for r in aw3.get("rows", [])), None)
            # 恢复=撤回误驳：重回待处理
            _, rs = call("/api/pending/restore", "POST", {"id": bid}, token)
            _, aw4 = call("/api/pending?status=awaiting&limit=50")
            ck("恢复后重回待处理", rs.get("ok") and any(r["id"] == bid for r in aw4.get("rows", [])), rs)
            # 护栏（2026-09-17 审查新增）：awaiting 行直接物理删除应被拒
            _, dlg = call("/api/pending/delete", "POST", {"id": bid}, token)
            ck("awaiting 行物理删除被护栏拒绝", not dlg.get("ok"), dlg)
            # 物理删除（仅驳回日志内触发）：先驳回再删，彻底消失
            call("/api/pending/reject", "POST", {"id": bid, "note": "自检删除前驳回"}, token)
            _, dl = call("/api/pending/delete", "POST", {"id": bid}, token)
            _, allp = call("/api/pending?status=all&limit=200")
            ck("物理删除后暂存行彻底消失",
               dl.get("ok") and not any(r["id"] == bid for r in allp.get("rows", [])), dl)
        # 放行自动登记来源群/发送人：库里还没有的，放行入库时分别写进群聊库/发送人库，且幂等只补不覆盖
        NEW_G, NEW_S = "自投放行群", "自投放行人"
        call("/api/pending", "POST", {"kind": "rates", "origin": "agent", "records": [
            {"起运港": "天津", "目的港": "Santos", "船司": "达飞", "柜型": "20GP",
             "海运费USD": "401", "有效期起": "2026-09-10", "有效期止": "2026-09-16",
             "来源群": NEW_G, "发送人": NEW_S, "消息ID": "REG-M1",
             "消息原文": "AGENT-REG-1 天津-Santos 20GP 401"}]}, token)
        _, lrg = call("/api/pending?status=awaiting&limit=200")
        rrg = next((r for r in lrg.get("rows", [])
                    if (r.get("payload") or {}).get("消息ID") == "REG-M1"
                    and r.get("check_status") == "passed"), None)
        ck("自动登记用例：暂存一条带全新来源群/发送人且过校验", rrg is not None,
           rrg and rrg.get("check_status"))
        if rrg:
            _, ap = call("/api/pending/approve", "POST", {"id": rrg["id"]}, token)
            reg = ap.get("registered") or {}
            ck("放行回报 registered 标出新群/新人均已登记",
               ap.get("ok") and reg.get("group_added") and reg.get("sender_added"), reg)
            _, cg = call("/api/chat-groups")
            ck("群聊库补登记了该来源群", any(g.get("name") == NEW_G for g in cg.get("rows", [])), NEW_G)
            _, mm = call("/api/master?full=1")
            ck("发送人库补登记了该发送人",
               any(s.get("name") == NEW_S for s in (mm.get("raw") or {}).get("senders", [])), NEW_S)
            call("/api/pending", "POST", {"kind": "rates", "origin": "agent", "records": [
                {"起运港": "天津", "目的港": "Pecém", "船司": "达飞", "柜型": "20GP",
                 "海运费USD": "402", "有效期起": "2026-09-10", "有效期止": "2026-09-16",
                 "来源群": NEW_G, "发送人": NEW_S, "消息ID": "REG-M2",
                 "消息原文": "AGENT-REG-2 天津-Pecém 20GP 402"}]}, token)
            _, lrg2 = call("/api/pending?status=awaiting&limit=200")
            rrg2 = next((r for r in lrg2.get("rows", [])
                         if (r.get("payload") or {}).get("消息ID") == "REG-M2"
                         and r.get("check_status") == "passed"), None)
            if rrg2:
                _, ap2 = call("/api/pending/approve", "POST", {"id": rrg2["id"]}, token)
                reg2 = ap2.get("registered") or {}
                ck("重复群/人不二次登记（registered 两个 added 均 False）",
                   not reg2.get("group_added") and not reg2.get("sender_added"), reg2)
                _, cg2 = call("/api/chat-groups")
                ck("群聊库里该群仍只有一条（幂等无重复）",
                   sum(1 for g in cg2.get("rows", []) if g.get("name") == NEW_G) == 1, NEW_G)
        print("\n== 6.8) 多值展开 / 脏值硬闸 / 主数据兜底 ==")
        RB = {"起运港": "天津", "目的港": "Santos", "船司": "达飞", "柜型": "20GP", "海运费USD": "100",
              "有效期起": "2026-09-10", "有效期止": "2026-09-16"}

        def staged(mid):
            _, l = call("/api/pending?status=awaiting&limit=400")
            return [r for r in l.get("rows", []) if (r.get("payload") or {}).get("消息ID") == mid]

        call("/api/pending", "POST", {"kind": "rates", "origin": "agent", "records": [
            dict(RB, **{"船司": "ONE/HPL/MSC/HMM", "海运费USD": "700", "消息ID": "EXP-C1",
                        "消息原文": "EXP-C1 天津-Santos ONE/HPL/MSC/HMM 20GP 700"})]}, token)
        rows = staged("EXP-C1")
        ck("合并船司 ONE/HPL/MSC/HMM 展开成 4 条且各自过闸",
           len(rows) == 4 and all(r["check_status"] == "passed" for r in rows)
           and sorted(str((r.get("payload") or {}).get("船司")) for r in rows) == ["HMM", "HPL", "MSC", "ONE"],
           [str((r.get("payload") or {}).get("船司")) for r in rows])

        call("/api/pending", "POST", {"kind": "rates", "origin": "agent", "records": [
            dict(RB, **{"船司": "ONE", "柜型": "20GP/40HQ/HC/40NOR", "海运费USD": "100/200/300",
                        "消息ID": "EXP-T1", "消息原文": "EXP-T1 三档三价"})]}, token)
        rows = staged("EXP-T1")
        ck("柜型三档×三价 → 拆三条且价格逐一对应",
           len(rows) == 3 and all(r["check_status"] == "passed" for r in rows)
           and sorted((str((r.get("payload") or {}).get("柜型")),
                       str((r.get("payload") or {}).get("海运费USD"))) for r in rows)
           == [("20GP", "100"), ("40HQ/HC", "200"), ("40NOR", "300")],
           [(str((r.get("payload") or {}).get("柜型")),
             str((r.get("payload") or {}).get("海运费USD"))) for r in rows])
        call("/api/pending", "POST", {"kind": "rates", "origin": "agent", "records": [
            dict(RB, **{"船司": "ONE", "柜型": "20GP/40HQ/HC/40NOR", "海运费USD": "100",
                        "消息ID": "EXP-T2", "消息原文": "EXP-T2 三档一价"})]}, token)
        rows = staged("EXP-T2")
        ck("柜型三档只给一个价 → 三条都打回，绝不把同一个价复制给三档",
           len(rows) == 3 and all(r["check_status"] == "failed" for r in rows)
           and all("海运费USD" in "".join(e["msg"] for e in (r.get("errors") or [])) for r in rows),
           [str((r.get("payload") or {}).get("海运费USD")) for r in rows])

        call("/api/pending", "POST", {"kind": "rates", "origin": "agent", "records": [
            dict(RB, **{"目的港": "(未注明，只去Manzanillo", "消息ID": "EXP-P1",
                        "消息原文": "EXP-P1 脏目的港"})]}, token)
        rows = staged("EXP-P1")
        ck("目的港含说明文字/括号 → failed 且报「含说明文字」（不是缺主数据）",
           len(rows) == 1 and rows[0]["check_status"] == "failed"
           and "含说明文字" in "".join(e["msg"] for e in (rows[0].get("errors") or [])),
           [e.get("msg", "")[:28] for e in (rows[0].get("errors") or [])] if rows else None)
        call("/api/pending", "POST", {"kind": "rates", "origin": "agent", "records": [
            dict(RB, **{"目的港": "Manzanillo(MX)", "消息ID": "EXP-P4", "消息原文": "EXP-P4 国家后缀写法"})]}, token)
        rows = staged("EXP-P4")
        ck("结尾国家码消歧后缀不误报成「含说明文字」（报的是不在主数据）",
           len(rows) == 1 and rows[0]["check_status"] == "failed"
           and "不在港口/港群主数据" in "".join(e["msg"] for e in (rows[0].get("errors") or [])),
           [e.get("msg", "")[:30] for e in (rows[0].get("errors") or [])] if rows else None)
        call("/api/pending", "POST", {"kind": "rates", "origin": "agent", "records": [
            dict(RB, **{"目的港": "Cartagena(CO)", "消息ID": "EXP-P2", "消息原文": "EXP-P2 带国家规范名"})]}, token)
        rows = staged("EXP-P2")
        ck("带国家的合法消歧规范名不被误伤（Cartagena(CO) 过闸）",
           len(rows) == 1 and rows[0]["check_status"] == "passed", rows and rows[0]["check_status"])
        call("/api/pending", "POST", {"kind": "rates", "origin": "agent", "records": [
            dict(RB, **{"柜型": "40GP", "消息ID": "EXP-P3", "消息原文": "EXP-P3 普柜不该被改成高柜"})]}, token)
        rows = staged("EXP-P3")
        ck("40GP 不被悄悄归成 40HQ/HC，按柜型三档打回",
           len(rows) == 1 and rows[0]["check_status"] == "failed"
           and str((rows[0].get("payload") or {}).get("柜型")) == "40GP",
           rows and str((rows[0].get("payload") or {}).get("柜型")))

        call("/api/pending", "POST", {"kind": "space", "origin": "agent", "records": [
            {"起运港": "宁波", "船司": "MSC", "船名航次": "(未注明船名) SANTANA", "舱位类型": "现舱",
             "有效期起": "2026-09-10", "消息ID": "EXP-V1", "消息原文": "EXP-V1 占位符船名"}]}, token)
        rows = staged("EXP-V1")
        ck("占位符船名按未填处理 → failed（不再让 (未注明船名) 混进业务表）",
           len(rows) == 1 and rows[0]["check_status"] == "failed"
           and "占位符" in "".join(e["msg"] for e in (rows[0].get("errors") or [])),
           [e.get("msg", "")[:20] for e in (rows[0].get("errors") or [])] if rows else None)

        _, mm0 = call("/api/master?full=1")
        n0 = len((mm0.get("raw") or {}).get("carriers") or [])
        v = call("/api/validate", "POST", {"table": "rates", "records": [
            dict(RB, **{"船司": "ZZK1", "消息ID": "EXP-A0", "消息原文": "EXP-A0 预检不该写主数据"})]})
        _, mm1 = call("/api/master?full=1")
        ck("只读预检不新增主数据（validate 后 carriers 数量不变）",
           len((mm1.get("raw") or {}).get("carriers") or []) == n0 and not v[1].get("good"), n0)
        call("/api/pending", "POST", {"kind": "rates", "origin": "agent", "records": [
            dict(RB, **{"船司": "ZZK1", "消息ID": "EXP-A1", "来源群": "兜底测试群",
                        "消息原文": "EXP-A1 未知船司自动补主数据"})]}, token)
        rows = staged("EXP-A1")
        _, mm2 = call("/api/master?full=1")
        ck("未知但形态像船司 → 自动补 carriers 主键并过闸",
           len(rows) == 1 and rows[0]["check_status"] == "passed"
           and len((mm2.get("raw") or {}).get("carriers") or []) == n0 + 1,
           {"条数": len(rows), "状态": rows and rows[0]["check_status"], "carriers": n0})
        note = next((cc.get("note") for cc in ((call("/api/master?full=1")[1].get("raw") or {})
                     .get("carriers") or []) if cc.get("code") == "ZZK1"), None)
        ck("自动新增的船司带「auto + 来源」溯源备注", bool(note) and "auto" in str(note), str(note)[:40])

        pods20 = "/".join(["Santos", "Rio de Janeiro", "Paranaguá", "Itajai", "Montevideo", "Buenos Aires",
                           "Callao", "Guayaquil", "Cartagena(CO)", "Kingston", "Manzanillo(MX)",
                           "Lázaro Cárdenas", "Pecém", "Suape", "Recife", "Salvador", "Belem",
                           "Vera Cruz", "Santana", "Rio Grande"])
        call("/api/pending", "POST", {"kind": "rates", "origin": "agent", "records": [
            dict(RB, **{"船司": "ONE/HPL/MSC/HMM", "目的港": pods20, "消息ID": "EXP-M1",
                        "消息原文": "EXP-M1 多维炸行"})]}, token)
        rows = staged("EXP-M1")
        ck("展开超上限只落一条超限工单（不刷屏待审）",
           len(rows) == 1 and rows[0]["check_status"] == "failed"
           and "超过上限" in "".join(e["msg"] for e in (rows[0].get("errors") or [])), len(rows))

        for mid in ("EXP-C1", "EXP-T1", "EXP-T2", "EXP-P1", "EXP-P2", "EXP-P3", "EXP-P4",
                    "EXP-V1", "EXP-A1", "EXP-M1"):
            for r in staged(mid):
                # 测试自清理：EXP-* 行是 awaiting 状态，护栏后需显式 force
                call("/api/pending/delete", "POST", {"id": r["id"], "force": True}, token)
        call("/api/master/delete", "POST", {"table": "carriers", "key": "ZZK1"}, token)
        _, mmz = call("/api/master?full=1")
        left = [r for r in (call("/api/pending?status=awaiting&limit=400")[1].get("rows") or [])
                if str((r.get("payload") or {}).get("消息ID") or "").startswith("EXP-")]
        ck("用例清理干净（测试待审行与测试船司已删）",
           not left and not any(cc.get("code") == "ZZK1" for cc in (mmz.get("raw") or {}).get("carriers") or []),
           {"left": len(left)})

        print("\n== 6.9) 主数据体检 / 港群保存校验 ==")
        _, au = call("/api/audit")
        mh = au.get("master_health") or {}
        ck("/api/audit 带 master_health（字典自身的洞）",
           au.get("ok") and isinstance(mh.get("issues"), list), [i.get("code") for i in mh.get("issues") or []])
        errs = [i for i in (mh.get("issues") or []) if i.get("level") == "error"]
        ck("error 级主数据问题为 0（港群成员断链 / 括号不闭合）", mh.get("ok") and not errs,
           [(e.get("code"), e.get("count")) for e in errs])
        _, g0 = call("/api/master?full=1")
        n_g0 = len((g0.get("raw") or {}).get("port_groups") or [])
        st, bad = call("/api/master/save", "POST", {"table": "port_groups", "row": {
            "name": "体检假群", "kind": "pod", "members": "Santos,根本不存在的港"}}, token)
        ck("港群保存拒绝不存在的成员（防双向展开静默断链）",
           not bad.get("ok") and "成员港" in str(bad.get("error") or ""), str(bad)[:90])
        _, g1 = call("/api/master?full=1")
        ck("被拒的港群没有写脏数据（数量不变）",
           len((g1.get("raw") or {}).get("port_groups") or []) == n_g0, n_g0)
        _, v1 = call("/api/validate", "POST", {"table": "rates", "records": [
            {"起运港": "天津", "目的港": "MXZLO", "船司": "达飞", "柜型": "20GP", "海运费USD": "100",
             "有效期起": "2026-09-10", "有效期止": "2026-09-16", "消息原文": "MD-LOCODE"}]})
        got = (((v1.get("results") or [None])[0] or {}).get("resolved") or {}).get("目的港")
        ck("清理死别名后 LOCODE 归一仍有效（MXZLO→Manzanillo）", got == "Manzanillo", got)
        _, v2 = call("/api/validate", "POST", {"table": "rates", "records": [
            {"起运港": "天津", "目的港": "地中海", "船司": "达飞", "柜型": "20GP", "海运费USD": "100",
             "有效期起": "2026-09-10", "有效期止": "2026-09-16", "消息原文": "MD-REGION"}]})
        r2 = ((v2.get("results") or [{}])[0]) or {}
        ck("区域词不再被当港口归一（目的港=地中海 报不在主数据）",
           not r2.get("ok") and "不在港口/港群主数据" in "".join(e.get("msg", "") for e in (r2.get("errors") or [])),
           [e.get("msg", "")[:30] for e in (r2.get("errors") or [])])

        # 航线是港口的子属性：ports.route 已种子的港可派生；只补空不覆盖；未定的留空
        _, dv = call("/api/validate", "POST", {"table": "rates", "records": [
            {"起运港": "宁波", "目的港": "Santos", "船司": "MSC", "柜型": "20GP", "海运费USD": "100",
             "有效期起": "2026-09-10", "有效期止": "2026-09-20", "消息原文": "DRV-1"},
            {"起运港": "宁波", "目的港": "Santos", "船司": "MSC", "柜型": "20GP", "海运费USD": "100",
             "航线": "墨西哥", "有效期起": "2026-09-10", "有效期止": "2026-09-20", "消息原文": "DRV-2"},
            {"起运港": "宁波", "目的港": "NEB", "船司": "MSC", "柜型": "20GP", "海运费USD": "100",
             "有效期起": "2026-09-10", "有效期止": "2026-09-20", "消息原文": "DRV-3"}]})
        rs = dv.get("results") or [{}, {}, {}]
        ck("未填航线时按目的港所属港群派生子线（Santos→南美东）",
           (rs[0].get("resolved") or {}).get("航线") == "南美东", (rs[0].get("resolved") or {}).get("航线"))
        ck("已明示航线不被派生覆盖（Santos+墨西哥 保持墨西哥）",
           (rs[1].get("resolved") or {}).get("航线") == "墨西哥", (rs[1].get("resolved") or {}).get("航线"))
        ck("挂起未定的港群不猜航线（NEB 留空）",
           (rs[2].get("resolved") or {}).get("航线", "") == "", (rs[2].get("resolved") or {}).get("航线"))

        # 作用域别名：同一写法只在登记的来源群里生效
        _, sv = call("/api/validate", "POST", {"table": "rates", "records": [
            {"起运港": "宁波", "目的港": "TESTX", "船司": "MSC", "柜型": "20GP", "海运费USD": "100",
             "来源群": "作用域测试群", "有效期起": "2026-09-10", "有效期止": "2026-09-20", "消息原文": "SC-1"},
            {"起运港": "宁波", "目的港": "TESTX", "船司": "MSC", "柜型": "20GP", "海运费USD": "100",
             "来源群": "别的群", "有效期起": "2026-09-10", "有效期止": "2026-09-20", "消息原文": "SC-2"}]})
        sr = sv.get("results") or [{}, {}]
        ck("作用域别名在登记群里命中（TESTX→Santos）",
           (sr[0].get("resolved") or {}).get("目的港") == "Santos", (sr[0].get("resolved") or {}).get("目的港"))
        ck("同一写法在别的群里不越界命中",
           (sr[1].get("resolved") or {}).get("目的港") != "Santos", (sr[1].get("resolved") or {}).get("目的港"))

        # 港群候选：agent 只能提，人工放行才写 port_groups
        GS = {"港群": "测试建议群", "标签": "回归用", "类型": "pod",
              "来源群": "作用域测试群", "消息ID": "GS-1", "消息原文": "GS 建议把桑托斯/伊塔雅伊并成一群"}
        call("/api/pending", "POST", {"kind": "group_suggestion", "origin": "agent", "records": [
            dict(GS, **{"成员": "Santos,根本不存在的港"}),
            dict(GS, **{"港群": "测试建议群2", "成员": "Santos,Itajai", "消息ID": "GS-2"})]}, token)
        _, gl = call("/api/pending?status=awaiting&kind=group_suggestion&limit=50")
        g1 = next((r for r in gl.get("rows", []) if (r.get("payload") or {}).get("消息ID") == "GS-1"), None)
        g2 = next((r for r in gl.get("rows", []) if (r.get("payload") or {}).get("消息ID") == "GS-2"), None)
        ck("港群候选：成员含虚构港名 → failed 并点名缺哪个",
           g1 and g1["check_status"] == "failed"
           and "成员港不在" in "".join(e["msg"] for e in (g1.get("errors") or [])),
           g1 and [e["msg"][:26] for e in (g1.get("errors") or [])])
        ck("港群候选：成员都真实 → passed 且预判动作=新建港群",
           g2 and g2["check_status"] == "passed"
           and (g2.get("resolved") or {}).get("动作") == "新建港群", g2 and g2.get("resolved"))
        if g2:
            _, ap = call("/api/pending/approve", "POST", {"id": g2["id"]}, token)
            _, mm3 = call("/api/master?full=1")
            grp = next((x for x in (mm3.get("raw") or {}).get("port_groups") or []
                        if x["name"] == "测试建议群2"), None)
            ck("放行后才真正写入 port_groups 且成员齐全",
               ap.get("ok") and grp and set(grp["members"].split(",")) == {"Santos", "Itajai"},
               grp and grp["members"])
            call("/api/master/delete", "POST", {"table": "port_groups", "key": "测试建议群2"}, token)
        for g in (g1, g2):
            if g:
                call("/api/pending/delete", "POST", {"id": g["id"]}, token)

        # 健康度体检（只报告、不阻断）：造孤儿群/人/港 + 过期仍生效，确认不拦录入
        AUD_G, AUD_S = "体检孤儿群", "体检孤儿人"
        _, ap_push = call("/api/push", "POST", {"table": "rates", "records": [
            {"起运港": "天津", "目的港": "Santos", "船司": "达飞", "柜型": "20GP",
             "海运费USD": "700", "有效期起": "2020-01-01", "有效期止": "2020-01-05",
             "来源群": AUD_G, "发送人": AUD_S, "消息ID": "AUDIT-EXPIRED",
             "消息原文": "AUDIT-EXPIRED 天津-Santos 20GP 700 过期"}]}, token)
        ck("体检用例：/api/push 也触发写入即登记（人工入口同样覆盖）",
           AUD_G in ((ap_push.get("registered") or {}).get("added_groups") or []), ap_push.get("registered"))
        call("/api/chat-group/delete", "POST", {"name": AUD_G}, token)
        call("/api/master/delete", "POST", {"table": "senders", "name": AUD_S}, token)
        call("/api/sync", "POST", {"table": "freight_rates", "rows": [
            {"content_key": "AUDIT-BADPORT", "pol": "Tianjin New Port", "pod": "ZzzNoSuchPort",
             "carrier": "CMA", "container_type": "20GP", "freight_usd": 888,
             "status": "当前生效", "valid_to": "2099-01-01", "origin": "manual"}]}, token)
        _, au2 = call("/api/audit")
        itypes = {c.get("type") for c in (au2.get("integrity") or [])}
        ck("体检含 integrity 结构", au2.get("ok") and isinstance(au2.get("integrity"), list), au2.get("integrity_count"))
        ck("体检报出「来源群未入库」孤儿", "来源群未入库" in itypes, sorted(itypes))
        ck("体检报出「发送人未入库」孤儿", "发送人未入库" in itypes, sorted(itypes))
        ck("体检报出「港口不在主数据」孤儿", "港口不在主数据" in itypes, sorted(itypes))
        ck("体检报出「已过期仍当前生效」", "已过期仍当前生效" in itypes, sorted(itypes))
        _, chk = call("/api/rates?pod=Santos&limit=80")
        ck("健康度只报告不拦录入：孤儿/过期行照常可查在库",
           any((x.get("message_text") or "") == "AUDIT-EXPIRED 天津-Santos 20GP 700 过期" for x in chk.get("rows", [])),
           [x.get("message_text") for x in chk.get("rows", [])])
        _, au = call("/api/audit")
        ck("全库体检返回结构", au.get("ok") and isinstance(au.get("violations"), list), au.get("count"))

        for kind in ("rates", "space"):
            st, allr = call(f"/api/{kind}?limit=500")
            for x in allr.get("rows", []):
                call("/api/record/delete", "POST", {"table": kind, "content_key": x["content_key"]}, token)
        st, s2 = call("/api/stats")
        ck("测试行清理干净", s2.get("ratesTotal") == 0 and s2.get("spaceTotal") == 0,
           {k: s2.get(k) for k in ("ratesTotal", "spaceTotal")})
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=8)
        except subprocess.TimeoutExpired:
            proc.kill()
        if TEST_DB.exists():
            TEST_DB.unlink()
    print("\n结果:", "全部通过" if not FAILS else "失败 %d 项 -> %s" % (len(FAILS), FAILS))
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
