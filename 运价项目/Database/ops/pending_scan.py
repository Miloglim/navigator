# -*- coding: utf-8 -*-
"""待审队列两类系统性问题的只读扫描（批量改值前后都跑一次，用于核对命中条数）。

扫描 A：`起运港` 是台账按 `chat_groups.default_pol` 兜出来的，但消息正文明示了默认港群之外的起运港
        —— 规则 1「正文明示优先」被静默兜底盖掉，且兜底不留任何痕迹。
扫描 B：`备注` 里藏着船期行（`M.D + 船名/航次` 或 `M.D + 空班/售罄/加班/跳港…`），
        而同一 `消息ID` 拆出的舱位记录数不足 —— 规则 7 被当成「动态不入舱位库」的免拆依据。

只 SELECT，不写库、不改任何 check_status。跑法：
    python ops/pending_scan.py            # 两个扫描都跑
    python ops/pending_scan.py --a        # 只跑 A
    python ops/pending_scan.py --b        # 只跑 B
"""
import argparse
import json
import os
import re
import sqlite3
import sys

sys.stdout.reconfigure(encoding="utf-8")
DB = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "freight_manual.db")

# 船期行：日期开头 + （航次代号/船名首词 / 舱位状态词）。排除「9.22-9.30」这类区间与「10天/号/点」噪声；
# 前置 (?<![\d./]) 防止从价格串中间咬一口（如「…1200/20GP」里的「00/20」）
SAIL = re.compile(r"(?<![\d./])\d{1,2}[./]\d{1,2}\s*(?![天日号点分秒])[\u4e00-\u9fa5]{0,3}[A-Z][A-Za-z0-9]{1,}"
                  r"|(?<![\d./])\d{1,2}[./]\d{1,2}\s*(空班|售罄|加班|跳港|现舱|放舱|开舱|撤载)")


def _sails(text):
    """命中且月日合理的船期行片段（12/31 之外的都是价格/编号串，不算船期）。"""
    out = set()
    for m in SAIL.finditer(text or ""):
        frag = m.group(0).strip()
        mm, dd = re.match(r"(\d{1,2})[./](\d{1,2})", frag).groups()
        if 1 <= int(mm) <= 12 and 1 <= int(dd) <= 31:
            out.add(frag)
    return out


def sec(t):
    print("\n" + "=" * 72 + "\n" + t + "\n" + "=" * 72)


def _awaiting(conn):
    out = []
    for r in conn.execute("SELECT id,kind,payload,check_status FROM pending_records "
                          "WHERE review_status='awaiting'"):
        try:
            out.append((r["id"], r["kind"], json.loads(r["payload"] or "{}"), r["check_status"]))
        except Exception as e:  # noqa: BLE001 - payload 坏 JSON 要报出来，不能静默少扫
            print("  ⚠ pending#%s payload 解析失败，已跳过：%s" % (r["id"], e))
    return out


def scan_default_pol(conn):
    """A：起运港＝群默认，但正文明示了默认群之外的港口。"""
    dpol = {r["name"]: r["default_pol"] for r in conn.execute(
        "SELECT name,default_pol FROM chat_groups WHERE active=1 AND default_pol IS NOT NULL")}
    members_of = {r["name"]: {x.strip() for x in (r["members"] or "").split(",") if x.strip()}
                  for r in conn.execute("SELECT name,members FROM port_groups")}
    pol_words = []                      # (触发词, 规范港名)：起运港词表＝ports.kind='both' 的口岸
    for r in conn.execute("SELECT name,aliases,name_cn FROM ports WHERE kind='both'"):
        for w in [r["name"], r["name_cn"]] + [x.strip() for x in (r["aliases"] or "").split(",")]:
            w = (w or "").strip()
            if len(w) >= 2:
                pol_words.append((w, r["name"]))

    def mentioned(text):
        """原文里出现过的起运港候选（ASCII 词带字母边界，中文词子串匹配）。"""
        hit = set()
        for w, name in pol_words:
            if w.isascii():
                if not re.search(r"(?<![A-Za-z])%s(?![A-Za-z])" % re.escape(w), text or "", re.I):
                    continue
            elif w not in (text or ""):
                continue
            hit.add(name)
        return hit

    rows = _awaiting(conn)
    hits = []
    for pid, kind, p, _cs in rows:
        if kind not in ("rates", "space"):
            continue
        grp, pol = p.get("来源群"), (p.get("起运港") or "").strip()
        d = dpol.get(grp)
        if not d or pol != d:
            continue
        outside = sorted(mentioned(p.get("消息原文") or "") - members_of.get(d, set()))
        if outside:
            hits.append((pid, kind, grp, d, outside,
                         p.get("海运费USD") or p.get("价格USD") or "",
                         (p.get("消息时间") or "")[:16]))
    sec("扫描 A｜起运港＝群默认兜底，但正文明示默认群外港口")
    print("扫待审 rates/space %d 条 → 命中 %d 条" % (
        sum(1 for _i, k, _p, _c in rows if k in ("rates", "space")), len(hits)))
    for x in hits:
        print("  #%s %s｜%s｜默认=%s｜正文另提=%s｜价=%s｜%s" % x)
    print("  改法：按正文明示港逐港改值（多港＝多条），或把该群 default_pol 清空交人工；只改值不放行。")
    return hits


def scan_sailing_remark(conn):
    """B：备注藏船期行，同消息舱位记录数不足。"""
    by_mid = {}
    for pid, kind, p, _cs in _awaiting(conn):
        by_mid.setdefault((p.get("消息ID") or "").strip(), []).append((pid, kind, p))
    hits = []
    for mid, items in sorted(by_mid.items(), key=lambda x: -(len(x[1]))):
        if not mid:
            continue
        lines, holder = set(), None
        for pid, _kind, p in items:
            found = _sails(p.get("备注") or "")
            if found:
                lines, holder = found, pid
        n_space = sum(1 for _i, k, _p in items if k == "space")
        if lines and n_space < len(lines):
            g = items[0][2]
            hits.append((mid, g.get("来源群"), (g.get("消息时间") or "")[:16],
                         len(lines), n_space, holder, sorted(lines)))
    sec("扫描 B｜备注藏船期行，同消息舱位记录不足")
    print("扫待审 (消息ID) 组 %d 个 → 命中 %d 组" % (len(by_mid), len(hits)))
    for x in hits:
        print("  %s｜%s %s｜备注船期行 %d 条 vs 舱位 %d 条（示例条目 #%s）" % x[:6])
        print("      %s" % "；".join(x[6][:6]))
    print("  改法：按「每行一条 kind=space」补拆（ETD/船名航次/舱位类型结构化），"
          "备注只留一句拆行结论；无价格不是不拆的理由（舱位库不要求价格）。")
    return hits


def main():
    ap = argparse.ArgumentParser(description="待审队列系统性问题只读扫描")
    ap.add_argument("--a", action="store_true", help="只跑扫描 A")
    ap.add_argument("--b", action="store_true", help="只跑扫描 B")
    args = ap.parse_args()
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    try:
        both = not (args.a or args.b)
        if both or args.a:
            scan_default_pol(conn)
        if both or args.b:
            scan_sailing_remark(conn)
        n = conn.execute("SELECT COUNT(*) FROM pending_records WHERE review_status='awaiting'").fetchone()[0]
        print("\n（口径：只扫 review_status='awaiting' 的 %d 条；已放行进业务表的历史错值不在本工具范围——"
              "那类要走「追加新行 + supersedes」，不能就地改。）" % n)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
