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
规则口径与 SKILL.md / 台账契约一致：柜型四档、附加费进备注、原文逐字从消息文件反查。
"""
import json, re, sys, os, argparse

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import paths as P
WORK = P.WORK                                    # 运行态 .work 目录（包外，见 paths.py）
from validity_parser import parse as vparse, derive as vderive

# 契约值权威真源在运价台账库（GET /api/master）；本文件只做模板与推断，不持有字段映射。
# record_builder 只负责从消息文本抽字段、产出【中文字段键】候选记录，归一/校验交给台账 /api/validate。
GROUPS = {"groups": P.chat_groups()}
GMAP = {g["slug"]: g for g in GROUPS["groups"]}

TEMPLATE_MAX_PORT_CHANGES = 3
TEMPLATE_MAX_RECORD_CHANGES = 3


class TemplateFingerprintMismatch(ValueError):
    """A claimed stable template drifted beyond its declared structural baseline."""


def check_template_fingerprint(name, rows, expected_pods, expected_records,
                               max_port_changes=TEMPLATE_MAX_PORT_CHANGES,
                               max_record_changes=TEMPLATE_MAX_RECORD_CHANGES):
    """Check stable POD names/count, record count and duplicate product identities.

    Tolerances are template-specific. Stable fixed layouts should pass zero; templates whose
    source genuinely varies may declare a bounded 2-3 item allowance. Any mismatch routes the
    whole message to Agent parsing instead of accepting partial template output.
    """
    records = [row.get("record") or {} for row in (rows or [])]
    expected_pods = set(expected_pods or [])
    actual_pods = {str(record.get("目的港") or "").strip() for record in records}
    actual_pods.discard("")
    port_changes = len(expected_pods.symmetric_difference(actual_pods))
    record_changes = abs(int(expected_records) - len(records))
    identities = [(record.get("目的港"), record.get("柜型"), record.get("船名航次"),
                   record.get("航线服务")) for record in records]
    duplicate_records = len(identities) - len(set(identities))
    errors = []
    if port_changes > max_port_changes:
        errors.append("港口指纹变化%d>%d（预期%s，实际%s）" % (
            port_changes, max_port_changes, sorted(expected_pods), sorted(actual_pods)))
    if record_changes > max_record_changes:
        errors.append("条目数量变化%d>%d（预期%d，实际%d）" % (
            record_changes, max_record_changes, expected_records, len(records)))
    if duplicate_records:
        errors.append("重复产品%d条" % duplicate_records)
    if errors:
        raise TemplateFingerprintMismatch("%s：%s" % (name, "；".join(errors)))
    fingerprint = {"name": name, "pods": sorted(actual_pods), "pod_count": len(actual_pods),
                   "record_count": len(records), "expected_record_count": int(expected_records),
                   "port_changes": port_changes, "record_changes": record_changes}
    for row in rows:
        row.setdefault("_meta", {})["template_fingerprint"] = fingerprint
    return rows


def norm_v(text):
    """有效期解析前的归一化（只补格式不改数值）。"""
    t = (text or "").strip()
    t = re.sub(r"-{2,}", "-", t)
    t = re.sub(r"(\d{1,2}\.\d{1,2})晚(\d{1,2}\.\d{1,2})", r"\1晚开\2", t)
    t = re.sub(r"(\d{1,2})-Sep(?:-(\d{2}))?\s+to\s+(\d{1,2})-Sep(?:-(\d{2}))?",
               lambda m: "%s Sep - %s Sep 20%s" % (m.group(1), m.group(3), m.group(2) or "26"), t, flags=re.I)
    return t


def vdates(text, msg_date, group=None):
    """返回 (valid_from, valid_to, etd, ai_default)。ai_default=True 表示用了默认兜底窗（规则17），需打"AI判断"标签。"""
    # 周号是跨群通用的有效期表达；仅保留 validity_parser 的显式开关以兼容直接调用方。
    return vderive(norm_v(text), msg_date, week_as_validity=True)


def has_quoted_content(msg):
    """DWS may emit a truthy quote object whose fields are all null; only quote text changes semantics."""
    return bool(str(((msg.get("q") or {}).get("c") or "")).strip())


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
    """产出台账契约要求的【中文字段键】候选记录。"""
    rec = {}
    g = GMAP[msg["_slug"]]
    for k, v in fields.items():
        if v in (None, ""):
            continue
        rec[k] = v
    rec["来源群"] = g["name"]
    rec["发送人"] = msg["s"]
    rec["来源群ID"] = g.get("cid") or ""
    rec["发送人ID"] = msg.get("senderId") or ""
    rec["发送人OpenID"] = msg.get("senderOpenDingTalkId") or ""
    rec["消息时间"] = msg["t"][:16]     # agent origin 服务端保留此值，必须带真实群消息时间
    rec["消息原文"] = compose_text(msg)  # 逐字，引用回复附带被引用原文；参与新增/更新判定，别改写
    if msg.get("id"):
        rec["消息ID"] = msg.get("id")   # openMessageId，供溯源/去重
    # 记录状态不填：台账默认 当前生效/当前有效，改价/售罄由 LLM 产新行、旧行看板现算
    return {"record": rec, "_meta": {"table": table, "group": g["slug"], "msg_id": msg["id"],
                                     "msg_t": msg["t"], "conf": conf}}


def rate(msg, pol, pod, route, carrier, ctype, price, validity_text="", etd=None,
         note="", conf="high", extra=None):
    vf, vt, ev, ai = vdates(validity_text, msg["t"], msg.get("_slug"))
    if ev and not etd:
        etd = ev
    fields = {"起运港": pol, "目的港": pod, "航线": route, "船司": carrier, "柜型": ctype,
              "海运费USD": price, "币种": "USD", "有效期船期": validity_text, "有效期起": vf, "有效期止": vt,
              "船期ETD": etd, "备注": note}
    if ai:
        fields["AI判断"] = "是"     # 用了默认兜底窗（规则17），看板打"AI判断"标签
    fields.update(extra or {})
    return base_cells("rate", msg, fields, conf)


def space(msg, pol, pod, route, carrier, vessel, cutoff, etd, boxtext, ctype, qty,
          stype, price=None, note="", conf="high", extra=None):
    vf, vt, ev, ai = vdates(cutoff or etd or "", msg["t"], msg.get("_slug"))   # 舱位也必填有效期
    fields = {"起运港": pol, "目的港": pod, "航线": route, "船司": carrier, "船名航次": vessel,
              "船期截关": cutoff, "船期ETD": etd or ev, "箱型箱量": boxtext, "柜型": ctype, "箱量": qty,
              "舱位类型": stype, "备注": note, "有效期起": vf, "有效期止": vt}
    if price is not None:
        fields["价格USD"] = price
    if ai:
        fields["AI判断"] = "是"
    fields.update(extra or {})
    return base_cells("space", msg, fields, conf)


# ---------------- 模板 ----------------

MSC_PRICE_COLUMNS = ('20GP', '40HQ/HC', '40NOR')
CMA_FAK_PRICE_COLUMNS = ('20GP', '40HQ/HC', '40NOR')


def t_emc_wsa2_guidance(msg):
    """华南-拉美长荣 WSA2 指导卖价：每个航次按 WCSA、MEX、Puerto Quetzal 和两档柜型展开。"""
    text = msg.get('c') or ''
    if msg.get('_slug') != 'huanan_latam' or has_quoted_content(msg):
        return None
    block = re.compile(
        r'WSA2\s*/\s*([A-Z0-9-]+)\s*\(ETA\s+CNXHK\s*:\s*(\d{1,2}/\d{1,2})\)\s*'
        r'WCSA\s*/\s*MX\s*:\s*USD\s*(\d+)\s*/\s*(\d+)\s*\(([^\r\n)]*)\)\s*'
        r'GTZNJ\s*:\s*USD\s*(\d+)\s*/\s*(\d+)\s*\(([^\r\n)]*)\)', re.I)
    signature_counts = {
        "WSA2": len(re.findall(r'\bWSA2\s*/', text, re.I)),
        "WCSA/MX": len(re.findall(r'\bWCSA\s*/\s*MX\s*:', text, re.I)),
        "GTZNJ": len(re.findall(r'\bGTZNJ\s*:', text, re.I)),
    }
    if sum(value > 0 for value in signature_counts.values()) < 2:
        return None
    matches = list(block.finditer(text))
    if not matches or len(set(signature_counts.values())) != 1 or len(matches) != signature_counts["WSA2"]:
        raise TemplateFingerprintMismatch("emc_wsa2_guidance：源结构段数不一致%s，转Agent完整解析" % signature_counts)
    # 首个报价块前若另有金额，则不是纯标题，拒绝套用以免吞掉其它报价。
    if re.search(r'\bUSD\s*\d+|\d{3,}', text[:matches[0].start()], re.I):
        raise TemplateFingerprintMismatch("emc_wsa2_guidance：首个模板块前出现额外金额")
    # 有未被模板覆盖的含价行时整条交Agent复核，不能静默漏掉变化的港口。
    covered = ''.join(match.group(0) for match in matches)
    covered_prices = len(re.findall(r'\bUSD\s*\d+\s*/\s*\d+', covered, re.I))
    source_prices = len(re.findall(r'\bUSD\s*\d+\s*/\s*\d+', text, re.I))
    if covered_prices != source_prices:
        raise TemplateFingerprintMismatch("emc_wsa2_guidance：含价行覆盖%d/%d，存在模板外报价" % (covered_prices, source_prices))
    out = []
    destinations = (
        ('WCSA', '南美西', 3, 4, 5),
        ('MEX', '墨西哥', 3, 4, 5),
        ('Puerto Quetzal', '中美洲', 6, 7, 8),
    )
    for match in matches:
        voyage, eta = match.group(1).upper(), match.group(2)
        for pod, route, small_i, large_i, terms_i in destinations:
            note = ('ETA CNXHK %s；%s；柜型按本群同模板历史稳定价序20GP/40HQ补全'
                    % (eta, match.group(terms_i).strip()))
            extra = {'船名航次': voyage, '航线服务': 'WSA2'}
            out.append(rate(msg, GMAP[msg['_slug']]['default_pol'], pod, route, 'EMC',
                            '20GP', int(match.group(small_i)), note=note, extra=extra))
            out.append(rate(msg, GMAP[msg['_slug']]['default_pol'], pod, route, 'EMC',
                            '40HQ/HC', int(match.group(large_i)), note=note, extra=extra))
    return check_template_fingerprint(
        "emc_wsa2_guidance", out, {"WCSA", "MEX", "Puerto Quetzal"}, len(matches) * 6,
        max_port_changes=0, max_record_changes=0)


def t_cma_ecsa_fak(msg):
    """本群达飞南美东三档 FAK 裸价；加价公式保留备注，不生成空报价。"""
    text = msg.get('c') or ''
    if (msg.get('_slug') != 'cma_emc_pil' or has_quoted_content(msg)
            or not text.lstrip().startswith('达飞南美东') or 'FAK' not in text):
        return None
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    date_lines = [line for line in lines if vparse(line, msg['t'])['valid_from']
                  and vparse(line, msg['t'])['valid_to']]
    if len(date_lines) != 1:
        return None
    validity = date_lines[0]
    terms = '；'.join(line for line in lines if 'FAK' in line or '利润' in line)
    quoted, addons = [], []
    base_port = None
    for line in lines:
        quote = re.fullmatch(
            r'(ECSA\s+BP|NEB|BRVIX\s*/\s*BRVCO)\s*[-：:]\s*USD\s*'
            r'(\d+)\s*/\s*(\d+)\s*/\s*(\d+)\s*NOR', line, re.I)
        if quote:
            ports = quote[1].upper().replace(' ', '')
            base_port = 'ECSA' if ports == 'ECSABP' else ports
            for pod in base_port.split('/'):
                for container, price in zip(CMA_FAK_PRICE_COLUMNS, quote.groups()[1:]):
                    quoted.append(rate(msg, GMAP[msg['_slug']]['default_pol'], pod, '南美东',
                                       'CMA', container, int(price), validity, note=terms))
            continue
        addon = re.fullmatch(r'(RIO\s+GRANDE|ASUNCION)\s+addon\s+(.+)', line, re.I)
        pecem = re.fullmatch(r'[(（]?\s*(PECEM)\s*[:：]\s*(\+.+?)[)）]?', line, re.I)
        if addon or pecem:
            if base_port not in ('ECSA', 'NEB'):
                return None
            addons.append('在 %s 基础上另加，原文：%s' % (base_port, line))
            continue
        # 有报价形态但没有完整解析时，整条交语义复核，不能静默漏掉一段。
        if re.search(r'\d\s*/\s*\d|\baddon\b|/TEU', line, re.I):
            return None
    if not quoted:
        return None
    exceptions = '；'.join(addons)
    for row in quoted:
        if exceptions:
            row['record']['备注'] += '；特殊港加价说明：' + exceptions
    return check_template_fingerprint(
        "cma_ecsa_fak", quoted, {"ECSA", "NEB", "BRVIX", "BRVCO"}, 12,
        max_port_changes=0, max_record_changes=0)


def t_msc_dalian_services(msg):
    """Explicit MSC port lists and per-service prices; reject partial parses."""
    text = msg.get('c') or ''
    if msg.get('_slug') != 'dalian_latam' or has_quoted_content(msg) or not re.match(r'MSC\s+南美东', text):
        return None
    definitions = {}
    for line in text.splitlines():
        match = re.match(r'\s*(Base Port|NE Port|PY Port)\s*[:：]\s*(?!USD)(.+)$', line, re.I)
        if match:
            definitions[match[1].lower()] = [p.strip() for p in match[2].split('/') if p.strip()]
    if 'base port' not in definitions:
        return None
    header = text.splitlines()[0]
    valid = re.search(r'\d{1,2}\.\d{1,2}\s*[-~至]\s*\d{1,2}\.\d{1,2}', header)
    if not valid:
        return None
    records = []
    current = None
    for line in text.splitlines()[1:]:
        if 'USD' not in line.upper():
            continue
        match = re.fullmatch(r'\s*(?:(.+?)\s*[:：]\s*)?USD\s*(\d+)\s*/\s*(\d+)(?:\s*/\s*NOR\s*(\d+))?\s*(.*)', line, re.I)
        if not match:
            return None
        label, small, large, nor, terms = match.groups()
        if label:
            current = label.strip()
        if not current:
            return None
        ports = definitions.get(current.lower(), [p.strip() for p in current.split('/')])
        service = re.search(r'\bVia\s+([A-Za-z][A-Za-z0-9_-]*)', terms, re.I)
        # Base prices without an explicit service cannot be safely disambiguated.
        if current.lower() == 'base port' and not service:
            return None
        for destination in ports:
            terminal = ''
            pod = destination
            if current.lower() == 'py port':
                terminal = destination
                city = re.search(r'\b(ASUNCION|PILAR|VILLETA)\b', destination, re.I)
                if not city:
                    return None
                pod = city.group(1)
            # This source's spelling is documented in its group specification.
            if pod.upper() == 'VILLOA DO CONDE':
                pod = 'Vila do Conde'
            for container, price in zip(MSC_PRICE_COLUMNS, (small, large, nor)):
                if price is None:
                    continue
                records.append(rate(msg, GMAP[msg['_slug']]['default_pol'], pod, '南美东', 'MSC', container,
                                    int(price), valid.group(), note=terms.strip(),
                                    extra={'航线服务': service.group(1) if service else '', '目的码头': terminal}))
    return records or None

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
    if not os.path.exists(p):
        return None
    with open(p, encoding="utf-8") as stream:
        return json.load(stream)


HPL_POD_CLASSIFICATION = re.compile(r"\b(?:base\s+port|port\s+group)\b|\bargentina\s+\d+\b", re.I)


def hpl_pod_fields(row, definitions=None):
    """Keep HPL commercial classifications and via details out of the POD field."""
    raw_pod = str(row.get("pod") or "").strip()
    via = str(row.get("via") or "").strip()
    notes = []
    pod = raw_pod
    members = next((values for name, values in (definitions or {}).items()
                    if str(name).strip().rstrip(":").casefold() == raw_pod.rstrip(":").casefold()), None)
    if isinstance(members, list) and members and all(isinstance(value, str) and value.strip() for value in members):
        pod = "/".join(value.strip() for value in members)
        notes.append("表内目的地分类：%s；按本图底部定义：%s" % (raw_pod, pod))
    elif HPL_POD_CLASSIFICATION.search(raw_pod):
        pod = ""
        notes.append("表内目的地分类：%s（本图定义未确认，待人工确认）" % raw_pod)
    if via:
        notes.append("经由/服务说明：%s" % via)
    return pod, "；".join(notes)


def t_hpl_cache(msg, cache):
    """HPL ECSA 价格表图 → 每行 3 档柜型。缓存结构不全时防御式跳过，不整条崩。"""
    pol = GMAP[msg["_slug"]]["default_pol"]
    vt = "%s-%s" % (cache.get("valid_from", ""), cache.get("valid_to", ""))
    base_note = "；".join(cache.get("footer_notes") or [])
    out = []
    for row in cache.get("rows") or []:
        if not isinstance(row, dict):
            return []  # 缓存结构不符（如通用表格行），按声明防御式跳过，不整条崩
        pod, pod_note = hpl_pod_fields(row, cache.get("pod_definitions"))
        sea = row.get("sea") or {}
        mfr = row.get("mfr") or {}
        allin = row.get("allin") or {}
        for ct in ("20GP", "40HQ/HC", "40NOR"):
            if ct not in sea:
                continue  # 该柜型无价，跳过不崩
            note = "MFR %s；ALL IN %s" % (mfr.get({"20GP": "20", "40HQ/HC": "40", "40NOR": "NOR"}[ct]), allin.get(ct))
            if row.get("note"):
                note += "；" + row["note"]
            if pod_note:
                note += "；" + pod_note
            note += "；表头通用条款：" + base_note
            if cache.get("pol_definition"):
                note += "；原图起运范围：" + str(cache["pol_definition"]) + "；本记录按来源群约定展示华南起运，未虚构其他起运港"
            service = str(row.get("via") or "").strip() or "原表未列经由"
            if not pod:
                service += "；目的分类：" + str(row.get("pod") or "")
            out.append(rate(msg, pol, pod, "南美东", "HPL", ct, sea[ct],
                            vt or "Validity from 8-Sep to 14-Sep", note=note, conf="med", extra={"航线服务": service}))
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


# ---------------- turkey_price 三类图生成器（2026-09-20 新增，零 LLM 展开） ----------------
# 约定 ocr_cache 的 type 字段驱动分发；结构见 llm_output_contract.md「图片识别输出」。
# 三类都只把结构化表格机械展开成候选，归一/校验仍交台账；拿不准的字段（如价卡无 POL）留空交人工。

def _num(s):
    m = re.search(r"(\d{3,5})", str(s if s is not None else ""))
    return int(m.group(1)) if m else None


def _ctype(s):
    """归一化 20GP、40HQ/HC、40NOR、45HQ/HC；识别不出返回 None。"""
    u = str(s or "").upper().replace("'", "").replace(" ", "")
    if "45" in u and ("HQ" in u or "HC" in u):
        return "45HQ/HC"
    if "NOR" in u:
        return "40NOR"
    if "40" in u and ("HQ" in u or "HC" in u):
        return "40HQ/HC"
    if "20" in u:
        return "20GP"
    return None


def _vol_split(v):
    """'1X40HC'→(40HQ/HC,1)；'20X40HC'→(40HQ/HC,20)；'20GP'→(20GP,1)；识别不出→(None,None)。"""
    s = str(v or "")
    m = re.search(r"(\d+)?\s*[X*]\s*(\d{2}[A-Za-z]*)", s)
    if m:
        qty = int(m.group(1)) if m.group(1) else 1
        return _ctype(m.group(2)), qty
    ct = _ctype(s)
    return (ct, 1) if ct else (None, None)


def t_space_table_cache(msg, cache):
    """现舱表图：带价行同时产 rate + space；无价行只产 space。"""
    default_pol = GMAP[msg["_slug"]]["default_pol"]
    carrier, route, gnote = cache.get("carrier") or "", cache.get("route") or "", cache.get("notes") or ""
    out = []
    for row in cache.get("rows") or []:
        if not isinstance(row, dict):
            continue
        pol, pod, etd = row.get("pol") or default_pol, row.get("pod"), str(row.get("etd") or "")
        vessel = row.get("vessel")
        ctype, qty = _vol_split(row.get("vol"))
        price = _num(row.get("rate"))
        common_bits = [x for x in (("免用箱 %s" % row["ft"]) if row.get("ft") else "",
                            ("退关费 %s" % row["退关费"]) if row.get("退关费") else "",
                            row.get("remark"), gnote) if x]
        if price is not None:
            extra = {"船名航次": vessel} if vessel else None
            rate_note = "；".join((["报价 %s" % row["rate"]] if row.get("rate") else []) + common_bits)
            out.append(rate(msg, pol, pod, route, carrier, ctype, price,
                            validity_text=etd or "", etd=etd, note=rate_note,
                            conf="med", extra=extra))
        if ctype and qty:
            space_note = "；".join(common_bits + (["价格见同消息运价记录"] if price is not None else []))
            out.append(space(msg, pol, pod, route, carrier, vessel or "", "", etd,
                             str(row.get("vol") or ""), ctype, qty, "现舱",
                             price=None, note=space_note, conf="med"))
    return out or None


PRICE_COLUMN_ALIASES = {
    "20GP": "20GP", "20DC": "20GP", "20": "20GP",
    "40HQ/HC": "40HQ/HC", "40HQ": "40HQ/HC", "40HC": "40HQ/HC",
    "40DV/HC": "40HQ/HC",
    "40NOR": "40NOR", "40N": "40NOR", "NOR": "40NOR",
    "45HQ/HC": "45HQ/HC", "45HQ": "45HQ/HC", "45HC": "45HQ/HC",
}
PRICE_COLUMN_ORDER = ("20GP", "40HQ/HC", "40NOR", "45HQ/HC")
STANDARD_40_COLUMNS = frozenset({"40GP", "40DC"})
EMPTY_TABLE_PRICES = frozenset({"", "/", "-", "—", "N/A", "NA", "SUSPEND"})


def _table_price(value):
    if value is None:
        return None
    text = str(value).strip()
    if text.upper() in EMPTY_TABLE_PRICES:
        return None
    return int(text) if text.isdigit() else text


def _price_columns(row):
    """Return every price with its header; never guess across conflicting columns."""
    buckets, standard, other = {}, [], []
    for label, value in row.items():
        header = re.sub(r"[\s'’′\"]", "", str(label).upper())
        price = _table_price(value)
        if price is None:
            continue
        canonical = PRICE_COLUMN_ALIASES.get(header)
        if canonical:
            buckets.setdefault(canonical, []).append((header, price))
        elif header in STANDARD_40_COLUMNS:
            standard.append((header, price))
        elif re.fullmatch(r"\d{2}(?:GP|DC|HQ|HC|NOR)", header):
            other.append((header, price))
    quotes, notes = [], []
    for canonical in PRICE_COLUMN_ORDER:
        columns = buckets.get(canonical, [])
        values = list(dict.fromkeys(value for _, value in columns))
        if len(values) == 1:
            quotes.append((canonical, values[0], "med"))
        elif values:
            detail = " / ".join("%s=%s" % item for item in columns)
            notes.append("同档柜型列价格冲突：%s，需确认，不自动选价" % detail)
            quotes.append(("", "/".join(map(str, values)), "low"))
    high_cube = [price for ct, price, _ in quotes if ct == "40HQ/HC"]
    for header, price in standard:
        if high_cube == [price]:
            continue  # The image explicitly gives equal 40GP and 40HQ prices.
        notes.append("%s=%s 与40HQ/HC未确认同价，保留原柜型待确认" % (header, price))
        quotes.append((header, price, "low"))
    for header, price in other:
        notes.append("图示%s报价%s，按45HQ/HC独立柜型保留" % (header, price))
    if not quotes and other:
        # Preserve a priced row even if it only has unsupported equipment.
        quotes.append(("", "/".join(str(value) for _, value in other), "low"))
    return quotes, notes


def _image_validity(row, parent, cache, msg):
    """Cell/row image dates override image-wide dates, then source text and ETD."""
    for source in (row, parent, cache):
        if source.get("valid_from") or source.get("valid_to"):
            return "%s~%s" % (source.get("valid_from") or source.get("valid_to"),
                              source.get("valid_to") or source.get("valid_from"))
        for key in ("validity", "validity_text", "有效期", "Validity"):
            if source.get(key):
                return str(source[key])
    text = msg.get("c") or ""
    parsed = vparse(text, msg["t"], week_as_validity=True)
    if parsed.get("valid_from") or parsed.get("valid_to"):
        return text
    return str(row.get("etd") or parent.get("etd") or cache.get("etd") or "")


def _table_rates(msg, cache, row, parent, pol, extra_notes=(), skipped=0):
    quotes, price_notes = _price_columns(row)
    if not quotes:
        return []
    validity = _image_validity(row, parent, cache, msg)
    extra, notes = {}, []
    for source in (row, parent, cache):
        if source.get("free_days") not in (None, ""):
            extra["目免"] = source["free_days"]
            break
    for note in (*extra_notes, row.get("tag"), row.get("remark"), row.get("notes"),
                 parent.get("tag"), parent.get("remark"), parent.get("notes"),
                 cache.get("notes"), cache.get("footer_notes"), *price_notes):
        if note and str(note) not in notes:
            notes.append(str(note))
    if skipped:
        notes.append("本图另有%s行无报价/暂停服务，未生成运价；原图保留供核对" % skipped)
    parsed = vparse(validity, msg["t"], week_as_validity=True)
    invalid_date = bool(validity and not any(parsed.values()))
    if invalid_date:
        notes.append("图示有效期无法解析：%s，需确认" % validity)
    out = []
    for ctype, price, confidence in quotes:
        out.append(rate(msg, pol, row.get("pod") or parent.get("pod"),
                        row.get("route") or parent.get("route") or cache.get("route") or "", cache.get("carrier") or "", ctype, price,
                        validity, etd=row.get("etd") or parent.get("etd"),
                        note="；".join(notes), conf="low" if invalid_date else confidence,
                        extra=extra))
    return out


def t_rate_matrix_cache(msg, cache):
    """Nested and flat price matrices share equipment/date handling with price cards."""
    from freight_images.services.completeness import matrix_errors, is_sailing_matrix
    if matrix_errors(cache, msg.get("_slug") or ""):
        return None
    if is_sailing_matrix(cache, msg.get("_slug") or ""):
        return t_sailing_matrix_cache(msg, cache)
    default_pol = GMAP[msg["_slug"]]["default_pol"]
    out = []
    cells = [(parent, cell) for parent in cache.get("rows") or [] if isinstance(parent, dict)
             for cell in (parent.get("cells") or [parent]) if isinstance(cell, dict)]
    skipped = sum(not _price_columns(cell)[0] for _, cell in cells)
    for parent, cell in cells:
        notes = [("矩阵价 %s" % cell["raw"]) if cell.get("raw") else "",
                 ("船名航次 %s" % cache["vessel"]) if cache.get("vessel") else ""]
        out.extend(_table_rates(msg, cache, cell, parent, parent.get("pol") or default_pol,
                                notes, skipped))
    return out or None


def t_price_card_cache(msg, cache):
    """POD price cards preserve all supported equipment, image dates and row clauses."""
    rows = [row for row in cache.get("rows") or [] if isinstance(row, dict)]
    skipped = sum(not _price_columns(row)[0] for row in rows)
    out = []
    for row in rows:
        generated = _table_rates(msg, cache, row, {}, row.get("pol") or cache.get("pol") or "", skipped=skipped)
        if row.get("price_scope_unresolved"):
            for item in generated:
                item["record"].pop("起运港", None)
                item["record"].pop("海运费USD", None)
                item["record"]["备注"] = "待核字段：%s；%s" % (row["price_scope_unresolved"], item["record"].get("备注") or "")
                item["_meta"]["conf"] = "low"
        out.extend(generated)
    return out or None


def t_formula_card_cache(msg, cache):
    """Keep explicit FAK prices; retain cost adjustments as notes only."""
    formula = str(cache.get("cost_formula") or "").strip()
    if not formula or formula not in compose_text(msg):
        return None
    rows = t_price_card_cache(msg, cache) or []
    source_text = compose_text(msg)
    cache_text = " ".join(str(cache.get(key) or "") for key in
                           ("price_basis", "title", "notes", "footer_notes", "cost_formula"))
    has_explicit_fak_prices = "FAK" in (source_text + " " + cache_text).upper() and any(
        _price_columns(row)[0] for row in (cache.get("rows") or []) if isinstance(row, dict)
    )
    for row in rows:
        record = row["record"]
        if "海运费USD" not in record:
            continue
        explicit_fak = cache.get("price_basis") == "FAK" or has_explicit_fak_prices
        if explicit_fak:
            reference = record.get("海运费USD")
            record["备注"] = "计价口径：原图FAK基准海运费USD%s，非折后合约成本；成本调整原文：%s；不代算折后价或叠加附加费。%s" % (
                reference, formula, record.get("备注") or "")
        else:
            reference = record.pop("海运费USD", None)
            record["备注"] = "图示参考FAK USD%s；实际成本公式：%s；不自动计算裸运价，须核实金额。%s" % (
                reference, formula, record.get("备注") or "")
            row["_meta"]["conf"] = "low"
    return rows or None


# OCR 缓存 type → 生成器注册表（新增图类型只在这里挂一行）
def t_sailing_matrix_cache(msg, cache):
    """Priced sailing rows emit one rate + one space per POL/POD/equipment/sailing."""
    from freight_images.services.completeness import matrix_errors, source_iso_date
    if matrix_errors(cache, msg.get("_slug") or ""):
        return None
    out = []
    for parent in cache.get("rows") or []:
        etd = source_iso_date(parent.get("etd"), msg["t"])
        if not etd:
            return None
        for cell in parent.get("cells") or []:
            quotes, price_notes = _price_columns(cell)
            notes = [str(note) for note in (cell.get("remark"), cache.get("notes"), *price_notes) if note]
            notes += [str(note) for note in (cache.get("footer_notes") or [])]
            if cell.get("eta"):
                notes.append("ETA " + source_iso_date(cell["eta"], msg["t"]))
            for ctype, price, confidence in quotes:
                rate_item = rate(msg, parent["pol"], cell["pod"], cache.get("route") or "",
                                 cache.get("carrier") or "", ctype, price, validity_text=etd,
                                 etd=etd, note="；".join(["报价 %s" % price] + notes),
                                 conf=confidence, extra={"船名航次": cache["vessel"]})
                space_item = space(msg, parent["pol"], cell["pod"], cache.get("route") or "",
                                   cache.get("carrier") or "", cache["vessel"], "", etd, "", ctype,
                                   None, "现舱", price=None,
                                   note="；".join(notes + ["价格见同消息运价记录"]), conf=confidence,
                                   extra={"航线服务": cache.get("service") or ""})
                if etd < str(msg["t"])[:10]:
                    for item in (rate_item, space_item):
                        item["record"].update({"有效期起": etd, "有效期止": etd, "AI判断": "是"})
                        item["record"]["备注"] += "；船期早于消息日期；有效期按船期标记历史候选，须人工核对"
                        item["_meta"]["conf"] = "low"
                out.extend((rate_item, space_item))
    return out or None


CACHE_TPL = {
    "sailing_matrix": t_sailing_matrix_cache,
    "space_guide": t_cosco_guide_cache,
    "space_table": t_space_table_cache,
    "rate_matrix": t_rate_matrix_cache,
    "price_card": t_price_card_cache,
    "formula_card": t_formula_card_cache,
}


def _quoted_image_needs_review(msg, cache):
    """Block ordinary quote replies, but reparse explicit revision notices."""
    if not ((msg.get("q") or {}).get("c") and cache.get("source_message") != msg.get("id")):
        return False
    current = str(msg.get("c") or "")
    revision = re.search(
        r"(?i)(?:week\s*\d+|第\s*\d+\s*周).{0,12}|"
        r"(?:week\s*\d+|第\s*\d+\s*周|周次|有效期|价格|运价|价卡).{0,12}"
        r"(?:改|更正|修正|更新|调整|变更)|"
        r"(?:改|更正|修正|更新|调整|变更).{0,12}"
        r"(?:week\s*\d+|第\s*\d+\s*周|周次|有效期|价格|运价|价卡)",
        current,
    )
    return revision is None


from freight_identity.runtime import snapshot as publisher_snapshot, decision_for as publisher_decision


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", required=True)
    ap.add_argument("--outdir", default=None)
    ap.add_argument("--message-id", action="append", default=[],
                    help="只解析指定消息；可重复传入")
    a = ap.parse_args()
    target_ids = set(a.message_id or [])
    mdir = os.path.join(WORK, "msgs_%s%s" % (a.date, P.sfx()))
    outdir = a.outdir or os.path.join(WORK, "draft_%s%s" % (a.date, P.sfx()))
    os.makedirs(outdir, exist_ok=True)

    identity = publisher_snapshot(WORK, groups=list(GMAP.values()))
    all_rate, all_space, needs_llm = [], [], []
    for fn in sorted(os.listdir(mdir)):
        if not fn.endswith(".json"):
            continue
        slug = fn[:-5]
        if slug not in GMAP:
            continue  # 非本链路群（历史遗留/手工导入文件），无群画像
        with open(os.path.join(mdir, fn), encoding="utf-8") as stream:
            data = json.load(stream)
        if not isinstance(data, list):
            continue  # 顶层非消息数组（如 all_groups.json），跳过而非崩溃
        for msg in data:
            if target_ids and msg.get("id") not in target_ids:
                continue
            if publisher_decision(identity, slug, msg)["action"] == "exclude":
                continue  # Explicit group/account exclusion; original cache is retained.
            msg["_slug"] = slug
            recs, reason = None, None
            for tpl in (t_emc_wsa2_guidance, t_cma_ecsa_fak, t_msc_dalian_services, t_msk_nor, t_one_special, t_tsl, t_hmm, t_one_space):
                try:
                    recs = tpl(msg)
                except TemplateFingerprintMismatch as e:
                    recs, reason = None, "模板指纹校验失败：%s" % e
                    break
                except Exception as e:
                    recs, reason = None, "template error: %s" % e
                if recs:
                    break
            if recs is None:
                # 图片消息：优先走 OCR 缓存
                img_recs = []
                for mid in msg.get("img") or []:
                    if not mid:
                        continue
                    cache = load_cache(mid)
                    if not cache:
                        reason = "图片无缓存，需识图"
                        continue
                    if _quoted_image_needs_review(msg, cache):
                        reason = "引用回复复用报价图片，需先判断询价/调价/状态，禁止直接复制为新报价"
                        continue
                    from freight_images.services.completeness import matrix_errors
                    cache_errors = matrix_errors(cache, slug)
                    if cache_errors:
                        reason = "图片维度不完整：" + "；".join(cache_errors)
                        continue
                    gen = CACHE_TPL.get(cache.get("type"))
                    if gen:
                        img_recs += gen(msg, cache) or []
                    elif cache.get("title", "").startswith("HPL"):
                        img_recs += t_hpl_cache(msg, cache) or []
                    elif "add on" in cache.get("title", ""):
                        img_recs += t_msk_addon_cache(msg, cache) or []
                    else:
                        reason = "OCR 缓存无对应生成器"
                if img_recs:
                    recs = img_recs
            if recs:
                if reason:
                    needs_llm.append({"group": slug, "t": msg["t"], "s": msg["s"], "id": msg["id"], "reason": reason})
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
