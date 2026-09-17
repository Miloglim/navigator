# -*- coding: utf-8 -*-
"""draft_gen.py — 心跳低成本流水线核心：模板命中消息零 LLM 生成候选记录。

用法:
  python heartbeat/draft_gen.py --date 0903 [--outdir .work/draft_0903]

输入:
  .work/msgs_<date>/<slug>.json   逐字落盘消息（唯一原文来源）
  .work/ocr_cache/<mediaId>.json  已识图结构化结果（跨群复用，不重复识图）
输出:
  draft_rate.json  / draft_space.json   候选记录（record 为中文字段键 dict，符合台账 /api/pending 契约，带 _meta）
  needs_llm.json                         模板未命中/需人工判断的消息清单
规则口径与 SKILL.md / 台账契约一致：柜型三档、附加费进备注、原文逐字从消息文件反查。
"""
import json, re, sys, os, argparse

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import paths as P
WORK = P.WORK                                    # 运行态 .work 目录（包外，见 paths.py）
from validity_parser import parse as vparse, derive as vderive

# 契约值权威真源已迁到运价台账库（GET /api/master）；本文件不再持有钉钉 fieldId 映射。
# record_builder 只负责从消息文本抽字段、产出【中文字段键】候选记录，归一/校验交给台账 /api/validate。
GROUPS = {"groups": P.chat_groups()}
GMAP = {g["slug"]: g for g in GROUPS["groups"]}


def norm_v(text):
    """有效期解析前的归一化（只补格式不改数值）。"""
    t = (text or "").strip()
    t = re.sub(r"-{2,}", "-", t)
    t = re.sub(r"(\d{1,2}\.\d{1,2})晚(\d{1,2}\.\d{1,2})", r"\1晚开\2", t)
    t = re.sub(r"(\d{1,2})-Sep(?:-(\d{2}))?\s+to\s+(\d{1,2})-Sep(?:-(\d{2}))?",
               lambda m: "%s Sep - %s Sep 20%s" % (m.group(1), m.group(3), m.group(2) or "26"), t, flags=re.I)
    return t


def vdates(text, msg_date):
    """返回 (valid_from, valid_to, etd, ai_default)。ai_default=True 表示用了默认兜底窗（规则17），需打"AI判断"标签。"""
    return vderive(norm_v(text), msg_date)


def compose_text(msg):
    """消息原文：本条逐字全文；若这条是引用回复，把被引用消息的逐字原文附在分隔线后一并传入，
    引用行头带上被引用那条的 openMessageId（`mID=...`）便于精确定位/溯源。
    各段都保持原样不改写；拼法确定（同消息+同引用→同串），可作台账新增/更新判定与内容指纹的稳定键。"""
    body = msg.get("c") or ""
    q = msg.get("q") or {}
    qc = (q.get("c") or "").strip()
    if qc:
        qtime = (q.get("t") or "")[:16]
        qid = (" mID=" + str(q.get("id"))) if q.get("id") else ""
        body = body + "\n\n——引用 " + qtime + " " + (q.get("s") or "") + qid + "——\n" + qc
    return body


def base_cells(table, msg, fields, conf):
    """产出台账契约要求的【中文字段键】候选记录（不再映钉钉 fieldId）。"""
    rec = {}
    g = GMAP[msg["_slug"]]
    for k, v in fields.items():
        if v in (None, ""):
            continue
        rec[k] = v
    rec["来源群"] = g["name"]
    rec["发送人"] = msg["s"]
    rec["消息时间"] = msg["t"][:16]     # agent origin 服务端保留此值，必须带真实群消息时间
    rec["消息原文"] = compose_text(msg)  # 逐字，引用回复附带被引用原文；参与新增/更新判定，别改写
    if msg.get("id"):
        rec["消息ID"] = msg.get("id")   # openMessageId，供溯源/去重
    # 记录状态不填：台账默认 当前生效/当前有效，改价/售罄由 LLM 产新行、旧行看板现算
    return {"record": rec, "_meta": {"table": table, "group": g["slug"], "msg_id": msg["id"],
                                     "msg_t": msg["t"], "conf": conf}}


def rate(msg, pol, pod, route, carrier, ctype, price, validity_text="", etd=None,
         note="", conf="high", extra=None):
    vf, vt, ev, ai = vdates(validity_text, msg["t"])
    if ev and not etd:
        etd = ev
    fields = {"起运港": pol, "目的港": pod, "航线": route, "船司": carrier, "柜型": ctype,
              "海运费USD": price, "有效期船期": validity_text, "有效期起": vf, "有效期止": vt,
              "船期ETD": etd, "备注": note}
    if ai:
        fields["AI判断"] = "是"     # 用了默认兜底窗（规则17），看板打"AI判断"标签
    fields.update(extra or {})
    return base_cells("rate", msg, fields, conf)


def space(msg, pol, pod, route, carrier, vessel, cutoff, etd, boxtext, ctype, qty,
          stype, price=None, note="", conf="high", extra=None):
    vf, vt, ev, ai = vdates(cutoff or etd or "", msg["t"])   # 舱位也必填有效期：有船期→[消息日,开船日]，都没有→兜底窗
    fields = {"起运港": pol, "目的港": pod, "航线": route, "船司": carrier, "船名航次": vessel,
              "船期截关": cutoff, "船期ETD": etd or ev, "箱型箱量": boxtext, "柜型": ctype, "箱量": qty,
              "舱位类型": stype, "价格USD": price, "备注": note,
              "有效期起": vf, "有效期止": vt}
    if ai:
        fields["AI判断"] = "是"
    fields.update(extra or {})
    return base_cells("space", msg, fields, conf)


# ---------------- 模板 ----------------

def t_msk_nor(msg):
    """青岛群 MSK WCSA/ECSA NOR 文字报价。"""
    if msg["_slug"] != "qingdao_latam":
        return None
    m = re.match(r"MSK (WCSA|ECSA) NOR ([\d/\-~]+)", msg["c"])
    if not m:
        return None
    route = {"WCSA": "南美西", "ECSA": "南美东"}[m.group(1)]
    validity = m.group(2)
    tail = "；".join(l.strip() for l in msg["c"].split("\n") if l.strip().startswith("注意") or l.strip().startswith("从PCD"))
    out = []
    for line in msg["c"].split("\n"):
        pm = re.match(r"([A-Z&/]+)[：:]\s*(?:USD)?(\d{3,5})\s*\+?(.*)", line.strip())
        if pm:
            out.append(rate(msg, "青岛", pm.group(1), route, "MSK", "40NOR", int(pm.group(2)),
                            validity, note=(pm.group(3) + ("；" + tail if tail else ""))))
    return out or None


def t_one_special(msg):
    """宁波群 ONE 加班船特价（两档价格串）。"""
    if msg["_slug"] != "ningbo_space" or "加班船特价" not in msg["c"]:
        return None
    ves = re.findall(r"(\d\.\d+)\s+(?:加班船\s+)?([A-Z][A-Z ]*[A-Z])\s+([A-Z0-9]{3,}E)?", msg["c"])
    vessel = " / ".join((v[1].strip() + (" " + v[2] if v[2] else "")) for v in ves)
    etd = "2026-09-08"
    out = []
    for line in msg["c"].split("\n"):
        pm = re.match(r"\*{0,2}([A-Z][A-Z/ ]+?)\*{0,2}\s*\*{0,2}(\d{4})/(\d{4})\*{0,2}\s*\*{0,2}\s*(?:\+(\w+))?", line.strip())
        if pm:
            pods, p20, p40, sur = pm.group(1).strip(), int(pm.group(2)), int(pm.group(3)), pm.group(4) or ""
            route = "墨西哥" if "MANZANILLO" in pods else "中美洲"
            note = "加班船特价（成本价）；船名航次：%s；船期ETD 9.8" % vessel + ("；+%s 附加费" % sur if sur else "")
            out.append(rate(msg, "宁波", pods, route, "ONE", "20GP", p20, "9.8", etd=etd, note=note))
            out.append(rate(msg, "宁波", pods, route, "ONE", "40HQ/HC", p40, "9.8", etd=etd, note=note))
    return out or None


def t_tsl(msg):
    """宁波群 TSL MANZANILLO 现舱带卖价。"""
    if msg["_slug"] != "ningbo_space" or "TSL" not in msg["c"][:20]:
        return None
    out = []
    m = re.search(r"卖价(\d{4})/(\d{4})", msg["c"])
    if m:
        out.append(rate(msg, "宁波", "MANZANILLO", "墨西哥", "TSL", "20GP", int(m.group(1)),
                        note="现舱卖价；21FT 目免"))
        out.append(rate(msg, "宁波", "MANZANILLO", "墨西哥", "TSL", "40HQ/HC", int(m.group(2)),
                        note="现舱卖价；21FT 目免"))
    for dm in re.finditer(r"(\d\.\d+)\s+([\d*/A-ZgpHC ]+?)\s*/?\s*(?:一票一高也有)?", msg["c"]):
        d, body = dm.group(1), dm.group(2)
        q40 = re.search(r"(\d+)\*40HC", body)
        q20 = re.search(r"(\d+)\*20GP", body)
        if q40:
            out.append(space(msg, "宁波", "MANZANILLO", "墨西哥", "TSL", "(未注明船名)", d + "开",
                             None, "4*40HC", "40HQ/HC", int(q40.group(1)), "现舱",
                             note="21FT；一票一高也有；卖价见运价表"))
        if q20:
            out.append(space(msg, "宁波", "MANZANILLO", "墨西哥", "TSL", "(未注明船名)", d + "开",
                             None, "1*20GP", "20GP", int(q20.group(1)), "现舱", note="21FT"))
    if "9.12" in msg["c"]:
        out.append(space(msg, "宁波", "MANZANILLO", "墨西哥", "TSL", "(未注明船名)", "9.12开",
                         None, "少量高箱", "40HQ/HC", None, "现舱", conf="med",
                         note="还有少量高箱现舱，可新订；目前都可以用箱"))
    return out or None


def t_hmm(msg):
    """宁波群 HMM 现舱行。"""
    if msg["_slug"] != "ningbo_space" or not msg["c"].lstrip().startswith("HMM"):
        return None
    out = []
    for line in msg["c"].split("\n"):
        pm = re.match(r"([A-Za-z]+)\s+(\d+)\*(40\w+)\s+([\d.]+)晚开(\d+)\s+(\w+)(.*)", line.strip())
        if pm:
            pod, qty, ctype, cut, _, svc, tail = pm.groups()
            ctype = "40HQ/HC" if "HQ" in ctype else "40NOR"
            pod_disp = {"Guayaquil": "GUAYAQUIL", "PQ": "PQ"}.get(pod, pod.upper())
            route = "南美西" if pod_disp == "GUAYAQUIL" else "中美洲"
            out.append(space(msg, "宁波", pod_disp, route, "HMM", "(未注明船名) %s" % svc,
                             "%s晚开%s" % (cut, pm.group(5)), None,
                             "%s*%s" % (qty, ctype), ctype, int(qty), "现舱",
                             note="可以改港" + ("；" + tail.strip() if tail.strip() else "")))
    return out or None


def t_one_space(msg):
    """宁波群 ONE 现舱块：船期行 + 港点箱量行。"""
    if msg["_slug"] != "ningbo_space" or "ONE  现舱" not in msg["c"]:
        return None
    out = []
    cur = None
    for raw in msg["c"].split("\n"):
        line = raw.replace("**", "").replace("#", "").strip()
        if not line:
            continue
        if "都能接" in line and "加班船" in line:
            out.append(space(msg, "宁波", "(未注明，中美洲都能接)", "中美洲", "ONE", "(未注明船名)",
                             line, None, None, None, None, "加班船",
                             note="新订公告；" + line, conf="med"))
            cur = None
            continue
        vm = re.match(r"^(\d{1,2}\.\d{1,2})(?:晚(\d{1,2}\.\d{1,2}))?\s*(?:(AX\d|NW\d|2水)\s*)?(?:加班船\s*)?([A-Z][A-Z ]*[A-Z])?\s*([A-Z0-9]{3,}E)?\s*(?:预计?晚到\s*(\d{1,2}\.\d{1,2}))?", line)
        if vm and vm.group(4) and not re.search(r"\d+高", line):
            etd = vm.group(1)
            late = ("晚到%s " % vm.group(6)) if vm.group(6) else ""
            extra_flag = "加班船" if "加班船" in line else "现舱"
            vessel = (vm.group(4) or "").strip()
            if vm.group(5):
                vessel = (vessel + " " + vm.group(5)).strip()
            cur = {"etd": etd, "svc": vm.group(3), "vessel": vessel or "(未注明船名)",
                   "cutoff": line, "type": extra_flag, "late": late}
            continue
        podm = re.match(r"^([A-Z][A-Z ]*[A-Z])\s+", line)
        if not podm or not cur:
            continue
        pod = podm.group(1).strip()
        body = line[podm.end():]
        route = {"MANZANILLO": "墨西哥", "CALLAO": "南美西", "LAZARO": "墨西哥",
                 "BUENAVENTURA": "南美西", "PQ": "中美洲", "SAN ANTONIO": "南美西",
                 "ACAJUTLA": "中美洲"}.get(pod, "")
        vname = cur["vessel"] + (" " + cur["svc"] if cur["svc"] else "")
        sb = re.match(r"^(\d+)小(?:(\d+)高)?\s*(.*)$", body)
        b = re.match(r"^(\d+)高\s*(.*)$", body)
        if sb:
            out.append(space(msg, "宁波", pod, route, "ONE", vname, cur["cutoff"], None,
                             "%s小" % sb.group(1), "20GP", int(sb.group(1)), cur["type"],
                             note=cur["late"], conf="high"))
            if sb.group(2):
                out.append(space(msg, "宁波", pod, route, "ONE", vname, cur["cutoff"], None,
                                 "%s高" % sb.group(2), "40HQ/HC", int(sb.group(2)), cur["type"],
                                 note=cur["late"], conf="high"))
        elif b:
            tail = b.group(2).strip()
            out.append(space(msg, "宁波", pod, route, "ONE", vname, cur["cutoff"], None,
                             "%s高" % b.group(1), "40HQ/HC", int(b.group(1)), cur["type"],
                             note=(cur["late"] + tail).strip(),
                             conf="high" if not tail else "med"))
    return out or None


# ---------------- OCR 缓存消费 ----------------

def load_cache(media_id):
    p = os.path.join(P.OCR_CACHE, media_id.lstrip("@") + ".json")
    return json.load(open(p, encoding="utf-8")) if os.path.exists(p) else None


def t_hpl_cache(msg, cache):
    """HPL ECSA 价格表图 → 每行 3 档柜型。缓存结构不全时防御式跳过，不整条崩。"""
    pol = GMAP[msg["_slug"]]["default_pol"]
    vt = "%s-%s" % (cache.get("valid_from", ""), cache.get("valid_to", ""))
    base_note = "；".join(cache.get("footer_notes") or [])
    out = []
    for row in cache.get("rows") or []:
        pod = (row.get("pod") or "") + (" via " + row["via"] if row.get("via") else "")
        sea = row.get("sea") or {}
        mfr = row.get("mfr") or {}
        allin = row.get("allin") or {}
        for ct in ("20GP", "40HQ/HC", "40NOR"):
            if ct not in sea:
                continue  # 该柜型无价，跳过不崩
            note = "MFR %s；ALL IN %s" % (mfr.get({"20GP": "20", "40HQ/HC": "40", "40NOR": "NOR"}[ct]), allin.get(ct))
            if row.get("note"):
                note += "；" + row["note"]
            note += "；表头通用条款：" + base_note[:200]
            out.append(rate(msg, pol, pod, "南美东", "HPL", ct, sea[ct],
                            vt or "Validity from 8-Sep to 14-Sep", note=note, conf="med"))
    return out


def t_msk_addon_cache(msg, cache):
    """MSK 包舱目的港 add on 表：纯公式无绝对价 → 海运费/柜型留空。"""
    out = []
    for row in cache["rows"]:
        note = "MSK 9月包舱（WCSA，9/7-10/4）；在%s基础上 add on：20GP +%s / 40 +%s" % (
            row["base_port"], row["addon_20GP"], row["addon_40GP_40HQ"])
        out.append(rate(msg, "华南基本港", row["dest"], "南美西", "MSK", None, None,
                        "9/7-10/4", note=note, conf="med"))
    return out


def t_cosco_guide_cache(msg, cache):
    """COSCO 华南订舱指南 → 舱位表，按船名航次一行。"""
    type_map = [("订舱已满", "售罄"), ("已满", "舱位紧张"), ("只接拖无忧", "舱位紧张"),
                ("优先拖无忧", "放舱开放"), ("驳船敞开", "放舱开放"),
                ("跳挂", "跳港"), ("omit", "跳港"), ("BLANK", "空班"), ("停航", "空班")]
    out = []
    for row in cache["rows"]:
        st = row["status"] or ""
        # BLANK SAILING = 空班（整班取消），不是撤载改期（甩柜/退舱改期）——2026-09-11 分档
        ctype = "空班" if row["vessel"] == "BLANK SAILING" else next(
            (v for k, v in type_map if k in st), "放舱开放")
        if not st and ctype != "空班":
            continue
        # 航线留空：台账里「拉美」是多条航线的共用 region，route_idx 判歧义丢弃，填它必被打回（space 航线可空）
        # 目的港留空：这表只给服务码，没有目的港；服务码既不是港口也不是船名，写进备注（旧版把
        # "(未注明，WSA2线)" 塞进目的港，台账现在按"含说明文字"打回）
        out.append(space(msg, "华南基本港", "", "", "COSCO",
                         "%s %s" % (row["vessel"], row["voy"] or ""), row["ltd"], None,
                         None, None, None, ctype,
                         note="普货FAK放舱指引：%s；SVVD %s；WEEK %s；服务码 %s（非目的港/非船名）"
                              % (st or "停航", row["svvd"], row["week"], row["svc"]),
                         conf="med"))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", required=True)
    ap.add_argument("--outdir", default=None)
    a = ap.parse_args()
    mdir = os.path.join(WORK, "msgs_%s" % a.date)
    outdir = a.outdir or os.path.join(WORK, "draft_%s" % a.date)
    os.makedirs(outdir, exist_ok=True)

    all_rate, all_space, needs_llm = [], [], []
    for fn in sorted(os.listdir(mdir)):
        if not fn.endswith(".json"):
            continue
        slug = fn[:-5]
        if slug not in GMAP:
            continue  # 非本链路群（历史遗留/手工导入文件），无群画像
        data = json.load(open(os.path.join(mdir, fn), encoding="utf-8"))
        if not isinstance(data, list):
            continue  # 顶层非消息数组（如 all_groups.json），跳过而非崩溃
        for msg in data:
            msg["_slug"] = slug
            recs, reason = None, None
            for tpl in (t_msk_nor, t_one_special, t_tsl, t_hmm, t_one_space):
                try:
                    recs = tpl(msg)
                except Exception as e:
                    recs, reason = None, "template error: %s" % e
                if recs:
                    break
            if recs is None:
                # 图片消息：优先走 OCR 缓存
                img_recs = []
                for mid in msg.get("img") or []:
                    cache = load_cache(mid)
                    if not cache:
                        reason = "图片无缓存，需识图"
                        continue
                    fn_map = {"HPL": t_hpl_cache, "MSK": t_msk_addon_cache}
                    if cache.get("type") == "space_guide":
                        img_recs += t_cosco_guide_cache(msg, cache)
                    elif cache.get("title", "").startswith("HPL"):
                        img_recs += t_hpl_cache(msg, cache)
                    elif "add on" in cache.get("title", ""):
                        img_recs += t_msk_addon_cache(msg, cache)
                    else:
                        reason = "OCR 缓存无对应生成器"
                if img_recs:
                    recs = img_recs
                    reason = None
            if recs:
                needs_llm.append({"group": slug, "t": msg["t"], "s": msg["s"], "id": msg["id"],
                                  "reason": "已模板生成 %d 条，图片/引用部分仍需复核" % len(recs)}) if msg.get("q", {}).get("c") else None
                for r in recs:
                    (all_rate if r["_meta"]["table"] == "rate" else all_space).append(r)
            else:
                needs_llm.append({"group": slug, "t": msg["t"], "s": msg["s"], "id": msg["id"],
                                  "reason": reason or "模板未命中"})

    for name, data in (("draft_rate.json", all_rate), ("draft_space.json", all_space),
                       ("needs_llm.json", needs_llm)):
        with open(os.path.join(outdir, name), "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=1)
    print(json.dumps({"rate": len(all_rate), "space": len(all_space),
                      "needs_llm": len(needs_llm), "outdir": outdir}, ensure_ascii=False))


if __name__ == "__main__":
    main()
