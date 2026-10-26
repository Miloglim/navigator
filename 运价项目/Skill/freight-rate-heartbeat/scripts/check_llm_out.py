# -*- coding: utf-8 -*-
"""check_llm_out.py —— Agent 判定文件的只读自查（写完先跑这个，再 advance）。

分工要讲清楚：**台账才是闸门**（必填、主数据命中、归一、留痕），这里只查台账查不到的两类东西：
  1) 越权与断链：msg_id 溯源、禁填键（这些 pipeline 会强制注入）、decision/_table/slug 合法；
  2) 规范执行痕迹：rule_basis 指向的锚点是否真实存在、用了某群「缩写表」里的写法却没引用该群规范 → 疑似没读规范。
errors按消息进入隔离，不拖住其他消息；warnings为普通可疑项，继续台账预检。

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

# 分片/单群：命令行 --shard / --only 先写进环境变量，再 import paths（paths 导入时读它定 sfx，
# 决定 llm_out_<D>_A/ 或 llm_out_<D>_<slug>/ 目录）。pipeline 自查提示已把这两个 flag 带上。
for _i, _a in enumerate(sys.argv):
    if _a == "--shard" and _i + 1 < len(sys.argv):
        os.environ["FRT_SHARD"] = sys.argv[_i + 1]
    elif _a.startswith("--shard="):
        os.environ["FRT_SHARD"] = _a.split("=", 1)[1]
    elif _a == "--only" and _i + 1 < len(sys.argv):
        os.environ["FRT_ONLY"] = sys.argv[_i + 1].strip().lower()
    elif _a.startswith("--only="):
        os.environ["FRT_ONLY"] = _a.split("=", 1)[1].strip().lower()

import paths as P
import spec_index

FORBID_KEYS = {"来源群", "发送人", "消息时间", "消息原文", "消息ID", "记录状态",
               "有效期起", "有效期止", "船期ETD"}
PLACEHOLDERS = ("未注明", "待定", "TBD", "N/A", "无", "-")
VALID_DECISION = ("records", "noise", "template")
VALID_TABLE = ("rate", "space")
MONEY_SIGNAL = re.compile(r"(?i)(?:USD|US\$|\$|美金)\s*[0-9][0-9,]*(?:\s*/\s*[0-9][0-9,]*)*")
BARE_PRICE_SIGNAL = re.compile(r"(?<![0-9.])([1-9][0-9]{3,4})(?![0-9.])")
SPACE_FACT_SIGNAL = re.compile(
    r"(?i)(?:\b(?:20GP|40HQ|40HC|40H|40NOR)\b|\b(?:ETD|船期|截单|截关)\b|"
    r"\b(?:ZIM|MSC|MSK|HMM|COSCO|OOCL|EMC|ONE|HPL|CMA|PIL|WHL)\b|"
    r"\b(?:[1-9][0-9]{3,4})\s*/\s*(?:20GP|40HQ|40HC|40H|40NOR)\b)")
EXPLICIT_SPACE_SIGNAL = re.compile(r"(?i)(?:带价现舱|现舱|现仓|SPOT|VOLUME|箱量|箱型箱量|舱位类型|库存|截单|截关)")
EXPLICIT_PRICE_CTYPE = re.compile(
    r"(?i)(?<![\d./])([1-9]\d{3,4})\s*/\s*(45HQ/HC|45HQ|45HC|40HQ/HC|40NOR|20GP|40HQ|40HC|40H)\b")
RECORD_CTYPE = re.compile(r"(?i)45HQ/HC|45HQ|45HC|40HQ/HC|40NOR|20GP|40HQ|40HC|40H")
LOSS_FEE = re.compile(r"亏[舱仓](?:费)?\s*(?:USD|CNY|RMB|¥|\$)?\s*(\d{2,4})", re.I)


def safe_message_id(value):
    return re.sub(r"[^A-Za-z0-9_.\-]", "_", str(value))[:60]

CTYPE = [c for c in P.CONTRACT.get("ctype", [])]
CTYPE_SORT = sorted(CTYPE, key=len, reverse=True)      # 40HQ/HC 自带斜杠，必须最长优先
STYPE = set(P.CONTRACT.get("space_type_options", []))


def _ctype_tokens(val):
    """柜型串按四档最长优先贪婪消耗；吃不完 = 非法。返回 token 列表或 None。"""
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


def verified_formula_record(source, record, caches):
    if not source.get("img") or len(caches) != len(source["img"]):
        return False
    for cache in caches:
        formula = str(cache.get("cost_formula") or "").strip()
        if (cache.get("type") != "formula_card" or not formula
                or formula not in str(source.get("c") or "")
                or formula not in str(record.get("备注") or "")):
            return False
    return True


def _incomplete_priced_rate(source, record, formula_caches=()):
    """Price-bearing source text cannot be reduced to an empty rate placeholder."""
    text = str(source.get("c") or "")
    bare_prices = [int(match.group(1)) for match in BARE_PRICE_SIGNAL.finditer(text)
                   if not 1900 <= int(match.group(1)) <= 2100]
    if record.get("_table") != "rate" or not (MONEY_SIGNAL.search(text) or bare_prices):
        return []
    missing = []
    if not str(record.get("柜型") or "").strip():
        missing.append("柜型")
    if str(record.get("海运费USD") or "").strip() == "" and not verified_formula_record(source, record, formula_caches):
        missing.append("海运费USD")
    return missing


def _incomplete_priced_space(source, record):
    """Reject an empty space placeholder when the source contains extractable facts."""
    text = str(source.get("c") or "")
    if record.get("_table") != "space":
        return []
    has_price = bool(MONEY_SIGNAL.search(text) or
                     any(1900 > int(m.group(1)) or int(m.group(1)) > 2100
                         for m in BARE_PRICE_SIGNAL.finditer(text)))
    if not (has_price and SPACE_FACT_SIGNAL.search(text)):
        return []
    present = {key for key in ("起运港", "目的港", "航线", "航线服务", "船司", "柜型", "价格USD", "船名航次")
               if str(record.get(key) or "").strip()}
    return [] if present else ["核心字段"]


def _missing_dual_tables(source, records):
    """Priced space needs both rate and space; plain price cards need no forced space record."""
    text = str(source.get("c") or "")
    has_price = bool(MONEY_SIGNAL.search(text) or
                     any(1900 > int(m.group(1)) or int(m.group(1)) > 2100
                         for m in BARE_PRICE_SIGNAL.finditer(text)))
    if not (has_price and EXPLICIT_SPACE_SIGNAL.search(text)):
        return []
    tables = {str(r.get("_table") or "") for r in records if isinstance(r, dict)}
    return [name for name in ("rate", "space") if name not in tables]


def _invalid_space_fact(source, record):
    if record.get("_table") != "space":
        return []
    issues = []
    if not any(str(record.get(key) or "").strip() for key in ("目的港", "航线", "航线服务")):
        issues.append("缺少目的范围：目的港/港群、航线、航线服务至少填写一项，确保销售可以检索")
    space_type = str(record.get("舱位类型") or "").strip()
    has_anchor = any(str(record.get(key) or "").strip() for key in
                     ("目的港", "航线", "航线服务", "船名航次", "箱型箱量", "箱量"))
    if not has_anchor and space_type in ("", "现舱"):
        issues.append("缺少可定位的舱位类型/港口/船名/箱量，不能用空舱位占位")
    price = str(record.get("价格USD") or "").strip()
    fees = {match.group(1) for match in LOSS_FEE.finditer(str(source.get("c") or ""))}
    if price and price in fees:
        issues.append("价格USD与原文明示亏舱费相同，需核对是否把费用误当运价")
    return issues


def _priced_pairs(record):
    """Return only unambiguous container/price pairs from a model record."""
    ctype = str(record.get("柜型") or "").upper().replace(" ", "")
    ctypes = [match.group() for match in RECORD_CTYPE.finditer(ctype)]
    if not ctypes or "/".join(ctypes) != ctype:
        return set()
    price_key = "价格USD" if record.get("_table") == "space" else "海运费USD"
    prices = [part.strip().replace(",", "") for part in str(record.get(price_key) or "").split("/")]
    if len(prices) != len(ctypes) or any(not part.isdigit() for part in prices):
        return set()
    return {(int(price), _normalize_ctype(container)) for price, container in zip(prices, ctypes)}


def _normalize_ctype(value):
    if value.upper() in {"45HQ", "45HC", "45HQ/HC"}:
        return "45HQ/HC"
    return "40HQ/HC" if value.upper() in {"40HQ", "40HC", "40H", "40HQ/HC"} else value.upper()


def _missing_explicit_pairs(source, records):
    """Check high-confidence price/container pairs against all records in one message."""
    active_text = re.sub(r"~~.*?~~", "", str(source.get("c") or ""), flags=re.S)
    source_pairs = {(int(match.group(1)), _normalize_ctype(match.group(2)))
                    for match in EXPLICIT_PRICE_CTYPE.finditer(active_text)}
    record_pairs = set().union(*(_priced_pairs(record) for record in records)) if records else set()
    return sorted(source_pairs - record_pairs)


def load_objs(ldir, message_ids=None):
    out = []
    paths = sorted(glob.glob(os.path.join(ldir, "*.json")))
    for p in paths:
        if not os.path.exists(p):
            continue
        try:
            with open(p, encoding="utf-8") as stream:
                d = json.load(stream)
        except Exception as e:
            out.append((os.path.basename(p), None, "JSON 解析失败：%s" % e))
            continue
        for o in (d if isinstance(d, list) else [d]):
            if isinstance(o, dict):
                out.append((os.path.basename(p), o, None))
            else:
                out.append((os.path.basename(p), None, '判定必须是对象'))
    return out


def load_sources(work, d):
    sources, errors = {}, []
    mdir = os.path.join(work, "msgs_%s%s" % (d, P.sfx()))
    for fn in (os.listdir(mdir) if os.path.isdir(mdir) else []):
        if not fn.endswith(".json"):
            continue
        try:
            with open(os.path.join(mdir, fn), encoding="utf-8") as stream:
                messages = json.load(stream)
            for m in messages:
                if isinstance(m, dict) and m.get("id"):
                    sources[m["id"]] = dict(m, _slug=fn[:-5])
        except (OSError, ValueError, TypeError) as error:
            errors.append("原始消息文件 %s 无法读取：%s" % (fn, error))
    return sources, errors


def load_draft_records(work, d):
    """Include template records that BUILD will merge with Agent decisions."""
    result = {}
    draft_dir = os.path.join(work, "draft_%s%s" % (d, P.sfx()))
    for table in VALID_TABLE:
        path = os.path.join(draft_dir, "draft_%s.json" % table)
        if not os.path.exists(path):
            continue
        with open(path, encoding="utf-8") as stream:
            rows = json.load(stream)
        for row in rows:
            if not isinstance(row, dict):
                continue
            mid = (row.get("_meta") or {}).get("msg_id")
            record = row.get("record")
            if mid and isinstance(record, dict):
                result.setdefault(mid, []).append(dict(record, _table=table))
    return result


def check(date, work=None, msgs_by_id=None, limit_detail=40, message_ids=None):
    """返回 {"ok","errors","warnings","n_obj","n_rec","detail"}。只读，不写任何文件。"""
    work = work or P.WORK
    ldir = os.path.join(work, "llm_out_%s%s" % (date, P.sfx()))
    errs, warns = [], []
    blocked, unreadable, seen = {}, [], {}
    if msgs_by_id is None:
        msgs_by_id, source_errors = load_sources(work, date)
        errs.extend(source_errors)
    known_ids = set(msgs_by_id)
    try:
        draft_records = load_draft_records(work, date)
    except (OSError, ValueError, TypeError) as error:
        draft_records = {}
        errs.append("模板草稿无法读取，无法核对来源覆盖：%s" % error)
    slugs = {g.get("slug") for g in P.chat_groups()}
    n_obj = n_rec = 0
    target_ids = set(message_ids or [])
    for fn, o, bad in load_objs(ldir, target_ids or None):
        if bad:
            errs.append("%s：%s" % (fn, bad))
            unreadable.append(fn)
            continue
        error_start = len(errs)
        mid = o.get("msg_id")
        if target_ids and mid not in target_ids:
            continue
        n_obj += 1
        grp = o.get("group") or ""
        source = (msgs_by_id or {}).get(mid, {})
        if source.get('_slug') and source['_slug'] != grp:
            errs.append('%s：判定群与原始消息来源不一致' % fn)
        encoded = json.dumps({key:o.get(key) for key in ('group','decision','records')},ensure_ascii=False,sort_keys=True)
        if mid in seen and seen[mid] != encoded:
            errs.append('%s：同一消息存在冲突判定' % fn)
        seen[mid] = encoded
        if mid not in known_ids:
            errs.append("%s：msg_id %s 不在落盘消息里（溯源铁律）" % (fn, mid))
        if o.get("decision") not in VALID_DECISION:
            errs.append("%s：decision=%r 非法（records|noise|template）" % (fn, o.get("decision")))
        if grp not in slugs:
            errs.append("%s：group slug %r 不在 chat_sources 里" % (fn, grp))
        basis = o.get("rule_basis") or ""
        if not basis:
            errs.append("%s：缺 rule_basis（判定必须可审计）" % fn)
        else:
            ok, why = spec_index.resolve_basis(grp, basis)
            if not ok:
                errs.append("%s：rule_basis 锚点无效（%s）" % (fn, why))
        records = o.get('records') or []
        if not isinstance(records,list) or (o.get('decision') == 'records' and not records) or (o.get('decision') in ('noise', 'template') and records):
            errs.append('%s：decision与records结构不一致' % fn)
            records = []
        combined = []
        if o.get('decision') == 'records':
            combined = [r for r in records if isinstance(r, dict)]
            if o.get('replace_template') is not True:
                combined += draft_records.get(mid, [])
            missing_pairs = _missing_explicit_pairs(source, combined)
            if missing_pairs:
                detail = ", ".join("%s/%s" % (price, ctype) for price, ctype in missing_pairs)
                errs.append("%s：原文明示价格/柜型组合未在条目中完整出现：%s" % (fn, detail))
        spec = spec_index.spec_for(grp)
        writes = [w for a in spec.get("abbrev", []) for w in a["writes"] if len(w) >= 2]
        formula_caches = []
        for media_id in source.get("img") or []:
            cache_path = os.path.join(work, "ocr_cache", str(media_id).lstrip("@") + ".json")
            try:
                with open(cache_path, encoding="utf-8") as stream:
                    cache = json.load(stream)
                if not isinstance(cache, dict):
                    break
                formula_caches.append(cache)
            except (OSError, ValueError, TypeError):
                break  # Missing/unreadable evidence cannot exempt a missing price.
        for r in records:
            if not isinstance(r, dict):
                errs.append('%s：记录必须是字段对象' % fn)
                continue
            n_rec += 1
            bad_keys = sorted(k for k in FORBID_KEYS if k in r)
            if bad_keys:
                errs.append("%s：填了禁填键 %s（pipeline 强制注入）" % (fn, "/".join(bad_keys)))
            if r.get("_table") not in VALID_TABLE:
                errs.append("%s：_table=%r 非法（rate|space）" % (fn, r.get("_table")))
            missing_priced = _incomplete_priced_rate(source, r, formula_caches)
            if missing_priced:
                errs.append("%s：原文明示金额，但运价记录缺 %s；必须先按相似航线/相近港口已确认价格推断柜型并完整提取，不能只写待核对占位"
                            % (fn, "/".join(missing_priced)))
            missing_space = _incomplete_priced_space(source, r)
            if missing_space:
                errs.append("%s：原文同时出现价格和舱位事实，但舱位记录未提取核心字段；必须重读原文后填写有明确证据的字段，不能提交空占位"
                            % fn)
            for issue in _invalid_space_fact(source, r):
                errs.append("%s：%s" % (fn, issue))
            ct = str(r.get("柜型") or "").strip()
            if ct:
                toks = _ctype_tokens(ct)
                if toks is None:
                    warns.append("%s：柜型 %r 不在允许枚举内（%s）" % (fn, ct, "、".join(CTYPE)))
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
        if o.get('decision') == 'records':
            missing_tables = _missing_dual_tables(source, combined)
            if missing_tables:
                errs.append("%s：带价现舱必须双入运价和舱位，当前缺少 %s 记录"
                            % (fn, " / ".join(missing_tables)))
        if len(errs) > error_start:
            blocked.setdefault(str(mid or ''), []).extend(errs[error_start:])
    uniq = lambda xs: list(dict.fromkeys(xs))
    # 防呆：目录空/不存在时 n_obj=0，绝不能报"全部干净"——那多半是 --shard/--only 与生成时不一致、
    # 判定文件写到了别的目录。用醒目 error 顶出来，逼人去核对路径而不是误判通过。
    if n_obj == 0 and not target_ids:
        errs.append("llm_out 目录为空或不存在（%s）：确认生成判定时的 --shard/--only 与本次一致，"
                    "别把「没判定」当成「全干净」" % ldir)
    if target_ids:
        for mid in sorted(target_ids - set(seen)):
            errs.append("缺少本轮消息判定文件：%s" % mid)
    return {"ok": not errs, "date": date, "n_obj": n_obj, "n_rec": n_rec,
            "blocked_messages":blocked,"unreadable_files":unreadable,"checked_message_ids":list(seen),
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
        _sh = (" --shard %s" % P.SHARD) if P.SHARD else ""
        out.append("#   （另 %d 条可疑，跑 python scripts/check_llm_out.py --date %s%s --json 看全量）"
                   % (rep["n_warn"] - limit, rep["date"], _sh))
    return out


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description="llm_out 判定文件只读自查")
    ap.add_argument("--date", required=True, help="MMDD，与 .work/llm_out_<D> 对应")
    ap.add_argument("--shard", default=None, help="分片大组（A/B）；顶层已预扫描写 FRT_SHARD，此处仅登记以免未知参数报错")
    ap.add_argument("--only", default=None, help="单群手动模式 slug；顶层已预扫描写 FRT_ONLY，决定 llm_out_<D>_<slug>/ 目录")
    ap.add_argument("--message-id", action="append", default=[], help="只检查指定消息；可重复传入")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()
    rep = check(a.date, message_ids=a.message_id)
    print(json.dumps(rep, ensure_ascii=False, indent=1) if a.json
          else "\n".join(summary_lines(rep, limit=999)) or "# 判定自查：全部干净")
    sys.exit(1 if rep["errors"] else 0)
