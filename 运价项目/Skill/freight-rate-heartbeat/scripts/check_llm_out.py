# -*- coding: utf-8 -*-
"""check_llm_out.py —— Agent 判定文件的只读自查（写完先跑这个，再 advance）。

分工要讲清楚：**台账才是闸门**（必填、主数据命中、归一、留痕），这里只查台账查不到的两类东西：
  1) 越权与断链：msg_id 溯源、禁填键（这些 pipeline 会强制注入）、decision/_table/slug 合法；
  2) 规范执行痕迹：rule_basis 指向的锚点是否真实存在、用了某群「缩写表」里的写法却没引用该群规范 → 疑似没读规范。
所以 errors 非空 = 该轮判定有问题；warnings = 规范层可疑，交人工看，不拦提交。

    python check_llm_out.py --date 0917
    python check_llm_out.py --date 0917 --json
退出码：有 errors → 1，否则 0（即使有 warnings 也返回 0，别把人工闸门抢走）。
"""
import argparse
import glob
import json
import os
import re
import sys

import paths as P
import spec_index

FORBID_KEYS = {"来源群", "发送人", "消息时间", "消息原文", "消息ID", "记录状态",
               "有效期起", "有效期止", "船期ETD"}
PLACEHOLDERS = ("未注明", "待定", "TBD", "N/A", "无", "-")
VALID_DECISION = ("records", "noise")
VALID_TABLE = ("rate", "space")

CTYPE = [c for c in P.CONTRACT.get("ctype", [])]
CTYPE_SORT = sorted(CTYPE, key=len, reverse=True)      # 40HQ/HC 自带斜杠，必须最长优先
STYPE = set(P.CONTRACT.get("space_type_options", []))


def _ctype_tokens(val):
    """柜型串按三档最长优先贪婪消耗；吃不完 = 非法。返回 token 列表或 None。"""
    s = str(val).strip().upper().replace(" ", "")
    if not s:
        return []
    while s:
        for c in CTYPE_SORT:
            cu = c.upper()
            if s.startswith(cu):
                s = s[len(cu):]
                if s.startswith("/"):
                    s = s[1:]
                break
        else:
            return None
    return [c for c in CTYPE_SORT if c in str(val).upper()]


def load_objs(ldir):
    out = []
    for p in sorted(glob.glob(os.path.join(ldir, "*.json"))):
        try:
            d = json.load(open(p, encoding="utf-8"))
        except Exception as e:
            out.append((os.path.basename(p), None, "JSON 解析失败：%s" % e))
            continue
        for o in (d if isinstance(d, list) else [d]):
            if isinstance(o, dict):
                out.append((os.path.basename(p), o, None))
    return out


def load_ids(work, d):
    ids = set()
    mdir = os.path.join(work, "msgs_%s" % d)
    for fn in (os.listdir(mdir) if os.path.isdir(mdir) else []):
        try:
            for m in json.load(open(os.path.join(mdir, fn), encoding="utf-8")):
                ids.add(m.get("id"))
        except Exception:
            pass
    return ids


def check(date, work=None, msgs_by_id=None, limit_detail=40):
    """返回 {"ok","errors","warnings","n_obj","n_rec","detail"}。只读，不写任何文件。"""
    work = work or P.WORK
    ldir = os.path.join(work, "llm_out_%s" % date)
    errs, warns = [], []
    known_ids = set(msgs_by_id) if msgs_by_id is not None else load_ids(work, date)
    slugs = {g.get("slug") for g in P.chat_groups()}
    n_obj = n_rec = 0
    for fn, o, bad in load_objs(ldir):
        if bad:
            errs.append("%s：%s" % (fn, bad))
            continue
        n_obj += 1
        mid = o.get("msg_id")
        grp = o.get("group") or ""
        if mid not in known_ids:
            errs.append("%s：msg_id %s 不在落盘消息里（溯源铁律）" % (fn, mid))
        if o.get("decision") not in VALID_DECISION:
            errs.append("%s：decision=%r 非法（records|noise）" % (fn, o.get("decision")))
        if grp not in slugs:
            errs.append("%s：group slug %r 不在 chat_sources 里" % (fn, grp))
        basis = o.get("rule_basis") or ""
        if not basis:
            errs.append("%s：缺 rule_basis（判定必须可审计）" % fn)
        else:
            ok, why = spec_index.resolve_basis(grp, basis)
            if not ok:
                warns.append("%s：rule_basis 锚点存疑（%s）" % (fn, why))
        spec = spec_index.spec_for(grp)
        writes = [w for a in spec.get("abbrev", []) for w in a["writes"] if len(w) >= 2]
        for r in o.get("records") or []:
            if not isinstance(r, dict):
                continue
            n_rec += 1
            bad_keys = sorted(k for k in FORBID_KEYS if k in r)
            if bad_keys:
                errs.append("%s：填了禁填键 %s（pipeline 强制注入）" % (fn, "/".join(bad_keys)))
            if r.get("_table") not in VALID_TABLE:
                errs.append("%s：_table=%r 非法（rate|space）" % (fn, r.get("_table")))
            ct = str(r.get("柜型") or "").strip()
            if ct:
                toks = _ctype_tokens(ct)
                if toks is None:
                    warns.append("%s：柜型 %r 不在三档内（%s）" % (fn, ct, "、".join(CTYPE)))
                else:
                    pv = str(r.get("海运费USD") or "").strip()
                    if "/" in ct and pv and len(pv.split("/")) != len(toks):
                        warns.append("%s：柜型 %s 是 %d 档，海运费 %s 只有 %d 价（须逐档配价）"
                                     % (fn, ct, len(toks), pv, len(pv.split("/"))))
            st = str(r.get("舱位类型") or "").strip()
            if st and st not in STYPE:
                warns.append("%s：舱位类型 %r 不在十一档内" % (fn, st))
            for k, v in list(r.items()):
                sv = str(v or "").strip()
                if sv in PLACEHOLDERS or (sv.startswith("(") and sv.endswith(")")):
                    warns.append("%s：字段 %s 像占位符（%r），台账会按未填处理" % (fn, k, sv[:20]))
            hit = [w for w in writes if any(w in str(v) for v in r.values())]
            if hit and not basis.startswith("规范.md"):
                warns.append("%s：记录用到该群缩写 %s，但依据没引用本群规范（%s）"
                             % (fn, "/".join(hit[:3]), basis or "无 rule_basis"))
    uniq = lambda xs: list(dict.fromkeys(xs))
    return {"ok": not errs, "date": date, "n_obj": n_obj, "n_rec": n_rec,
            "errors": uniq(errs)[:limit_detail], "warnings": uniq(warns)[:limit_detail],
            "n_err": len(uniq(errs)), "n_warn": len(uniq(warns))}


def summary_lines(rep, limit=10):
    head = "# 判定自查：%d 文件 / %d 记录 · 错误 %d · 可疑 %d" % (
        rep["n_obj"], rep["n_rec"], rep["n_err"], rep["n_warn"])
    out = [head] if (rep["n_err"] or rep["n_warn"]) else []
    for e in rep["errors"][:limit]:
        out.append("#   ✗ %s" % e)
    for w in rep["warnings"][:limit]:
        out.append("#   ⚠ %s" % w)
    if rep["n_warn"] > limit:
        out.append("#   （另 %d 条可疑，跑 python scripts/check_llm_out.py --date %s --json 看全量）"
                   % (rep["n_warn"] - limit, rep["date"]))
    return out


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description="llm_out 判定文件只读自查")
    ap.add_argument("--date", required=True, help="MMDD，与 .work/llm_out_<D> 对应")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()
    rep = check(a.date)
    print(json.dumps(rep, ensure_ascii=False, indent=1) if a.json
          else "\n".join(summary_lines(rep, limit=999)) or "# 判定自查：全部干净")
    sys.exit(1 if rep["errors"] else 0)
