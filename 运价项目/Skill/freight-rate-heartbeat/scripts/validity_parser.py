# -*- coding: utf-8 -*-
"""
parse_validity.py —— 运价有效期/船期自由文本 → 结构化日期

统一入口，freight-rate-heartbeat 全流程只认这一个实现，避免各处正则口径漂移。

parse(text, msg_date) -> dict(valid_from, valid_to, etd)
  值为 "YYYY-MM-DD" 或 None。

三类：
  区间型  9.1-9.7 / 9/1-9/7 / 9.1~9.7 / 2026/09/01~09/07 / 1 Sep - 7 Sep
          -> valid_from + valid_to，etd 留空
  开船型  9/7开 / 9.12 ETD / 8.31托9.8 / 9/6晚开9/9 / 9.5托班到9.11
          -> 只填 etd，有效期留空
  不可解析 case by case / 锁8月底运价+500 / 空 -> 三者皆 None

GRI 生效日、附加费日期（如 "9.08 GRI"、"9.5起收LWS"）不算有效期，交给备注。
"""
import re
from datetime import date, timedelta

MON = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun",
     "jul", "aug", "sep", "oct", "nov", "dec"], 1)}

# 先剔除会误伤的片段
_STRIP = [
    re.compile(r'\d{1,2}[./]\d{1,2}\s*(?:起)?\s*GRI', re.I),
    re.compile(r'(?:自|从)?\s*\d{1,2}[./]\d{1,2}\s*起\s*(?:收|收取|加|另加)', re.I),
]
# 区间：可选年份前缀
_RANGE = re.compile(
    r'(?:(\d{4})[-/])?(\d{1,2})[./\-](\d{1,2})\s*(?:[-~–—]|至|到)\s*'
    r'(?:(\d{4})[-/])?(\d{1,2})[./\-]?(\d{1,2})?')
# 英文区间：1 Sep - 7 Sep / 01st Sep 2026
_RANGE_EN = re.compile(
    r'(\d{1,2})\s*(?:st|nd|rd|th)?\s*([A-Za-z]{3})[a-z]*\s*(?:[-–—~]|至|到)\s*'
    r'(\d{1,2})\s*(?:st|nd|rd|th)?\s*([A-Za-z]{3})', re.I)
# 拖班：8.31托9.8 / 9.5托9.7 / 9.10拖9.13 / 9.5托班到9.11（"托""拖"混用，都算）
_TUO = re.compile(r'(\d{1,2})[./](\d{1,2})\s*[托拖](?:班)?(?:到|至)?\s*(\d{1,2})[./](\d{1,2})')
# 晚开：9/6晚开9/9  -> ETD 取前一个
_LATE = re.compile(r'(\d{1,2})[/./](\d{1,2})\s*晚\s*开\s*(\d{1,2})[/./](\d{1,2})')
# 单开船：9/7开 / 9.4开 / 9月7日开
_SAIL = re.compile(r'(\d{1,2})[/./](\d{1,2})\s*(?:晚)?开')
# ETD 显式：ETD: 4 SEP / 9.12 ETD / ETD 9/12 / ETD：9.8（全角冒号）
_ETD_A = re.compile(r'ETD[:：\s]*?(\d{1,2})[/.\s](\d{1,2})', re.I)
_ETD_B = re.compile(r'(\d{1,2})[/.\s](\d{1,2})\s*ETD', re.I)
# 同月区间：9.1-14（= 9/1 至 9/14），右侧不带月份
_RANGE_SAMEM = re.compile(r'(?:(\d{4})[-/])?(\d{1,2})[./](\d{1,2})\s*(?:[-~–—]|至|到)\s*(\d{1,2})(?![\d./\-])')
# 裸英文月日：4 SEP
_MD_EN = re.compile(r'\b(\d{1,2})[/.\s]+([A-Za-z]{3})[a-z]*\b', re.I)


def _ymd(y, m, d, msg):
    try:
        return date(y, m, d).isoformat()
    except ValueError:
        return None


def _year_for(m, msg):
    """默认取消息年份；起月比消息月大超 6 个月视为上一年。"""
    if msg is None:
        return msg_year_default(m)
    if m - msg.month > 6:
        return msg.year - 1
    return msg.year


def msg_year_default(m):
    return None


def parse(text, msg_date=None):
    out = {"valid_from": None, "valid_to": None, "etd": None}
    if not text or not str(text).strip():
        return out
    if isinstance(msg_date, str):
        msg_date = date.fromisoformat(msg_date[:10])

    s = str(text)
    for rx in _STRIP:
        s = rx.sub(' ', s)

    # 1) 英文区间优先（VALID: 1 Sep - 7 Sep）
    m = _RANGE_EN.search(s)
    if m:
        a, b = MON.get(m.group(2).lower()), MON.get(m.group(4).lower())
        if a and b:
            ya = _year_for(a, msg_date) or (msg_date.year if msg_date else 2026)
            yb = ya if b >= a else ya + 1
            out["valid_from"], out["valid_to"] = _ymd(ya, a, int(m.group(1)), msg_date), \
                                                 _ymd(yb, b, int(m.group(3)), msg_date)
            return out

    # 2) 数字区间
    m = _RANGE.search(s)
    if m:
        sm, sd = int(m.group(2)), int(m.group(3))
        em, ed = int(m.group(5)), int(m.group(6) or m.group(3))
        sy = int(m.group(1)) if m.group(1) else (_year_for(sm, msg_date) or 2026)
        ey = int(m.group(4)) if m.group(4) else (sy if em >= sm else sy + 1)
        out["valid_from"] = _ymd(sy, sm, sd, msg_date)
        out["valid_to"] = _ymd(ey, em, ed, msg_date)
        if out["valid_from"] and out["valid_to"]:
            return out

    # 2b) 同月区间：9.1-14 -> 9/1 至 9/14
    m = _RANGE_SAMEM.search(s)
    if m:
        sy = int(m.group(1)) if m.group(1) else (_year_for(int(m.group(2)), msg_date) or 2026)
        sm, sd, ed = int(m.group(2)), int(m.group(3)), int(m.group(4))
        if sm <= 12 and ed <= 31 and sd <= 31 and ed > sd:
            out["valid_from"] = _ymd(sy, sm, sd, msg_date)
            out["valid_to"] = _ymd(sy, sm, ed, msg_date)
            if out["valid_from"] and out["valid_to"]:
                return out

    # 3) 拖班 -> ETD 取拖到的那天
    m = _TUO.search(s)
    if m:
        em, ed = int(m.group(3)), int(m.group(4))
        out["etd"] = _ymd(_year_for(em, msg_date) or 2026, em, ed, msg_date)
        return out

    # 4) 晚开 -> ETD 取前面开船日
    m = _LATE.search(s)
    if m:
        sm, sd = int(m.group(1)), int(m.group(2))
        out["etd"] = _ymd(_year_for(sm, msg_date) or 2026, sm, sd, msg_date)
        return out

    # 5) 显式 ETD
    for rx in (_ETD_A, _ETD_B):
        m = rx.search(s)
        if m:
            a, b = int(m.group(1)), int(m.group(2))
            out["etd"] = _ymd(_year_for(a, msg_date) or 2026, a, b, msg_date)
            return out

    # 6) 裸英文月日（ETD: 4 SEP 已被 5 捕获，这里兜 "4 SEP"）
    m = _MD_EN.search(s)
    if m and m.group(2).lower() in MON:
        mo, d = MON[m.group(2).lower()], int(m.group(1))
        out["etd"] = _ymd(_year_for(mo, msg_date) or 2026, mo, d, msg_date)
        return out

    # 7) 单开船
    m = _SAIL.search(s)
    if m:
        a, b = int(m.group(1)), int(m.group(2))
        out["etd"] = _ymd(_year_for(a, msg_date) or 2026, a, b, msg_date)
        return out

    return out


def derive(text, msg_date=None):
    """有效期兜底窗（2026-09-10 规则 17）。返回 (valid_from, valid_to, etd, ai_default)。

    运价必填有效期，报价常只给船期不给区间，按三级补：
      1) 解析出区间            -> 直接用，ai_default=False
      2) 无区间但有 ETD        -> [消息日期, ETD]，ai_default=False
      3) 既无区间又无 ETD      -> [消息日期, 消息日期+2天]，ai_default=True（看板打"AI判断"标签）
    "当前时间"取 msg_date（消息日期），保证可重放/确定性；缺省用今天。
    """
    r = parse(text, msg_date)
    if isinstance(msg_date, str):
        anchor = date.fromisoformat(msg_date[:10])
    elif isinstance(msg_date, date):
        anchor = msg_date
    else:
        anchor = date.today()
    anchor_s = anchor.isoformat()
    vf, vt, etd = r["valid_from"], r["valid_to"], r["etd"]
    if vf and vt:
        return vf, vt, etd, False
    # 只有裸日期（"9/16"、"9.16"、"2026-09-16"）时，parse 认不出是船期；这里当作开船时间兜成 [消息日期, 该日]
    if not etd:
        s = str(text or "").strip()
        m_iso = re.fullmatch(r'(\d{4})-(\d{1,2})-(\d{1,2})', s)
        m_md = re.fullmatch(r'(\d{1,2})\s*[/.\-]\s*(\d{1,2})', s)
        cand = None
        if m_iso:
            cand = _ymd(int(m_iso.group(1)), int(m_iso.group(2)), int(m_iso.group(3)), anchor)
        elif m_md:
            mo, dd = int(m_md.group(1)), int(m_md.group(2))
            cand = _ymd(_year_for(mo, anchor) or anchor.year, mo, dd, anchor)
        if cand and cand >= anchor_s:
            return anchor_s, cand, cand, False
    if etd and etd >= anchor_s:
        return anchor_s, etd, etd, False
    return anchor_s, (anchor + timedelta(days=2)).isoformat(), etd, True


if __name__ == "__main__":
    import sys
    sys.stdout.reconfigure(encoding="utf-8")
    cases = [
        "9.1-9.7", "9/1-9/7", "9.8-9.14", "2026/09/01~09/07", "9.1~9.7",
        "VALID: 1 Sep - 7 Sep", "Validity 1 Sep – 7 Sep",
        "9/7开", "9.4开", "9.12 ETD", "ETD: 4 SEP", "8.31托9.8", "9.5托9.7",
        "9/6晚开9/9", "8.25 晚开 9.4", "9.5托班到9.11", "9/6 ETD",
        "case by case", "锁8月底运价+500", "",
        "9.08 GRI  1,000/1,000/1,000",
        "9.1-9.7 南美东 注意从9.5起 Manaus 另加LWS",
        "ETD：9.8", "9.1-14", "9.8-14", "Validity 8 SEP- 14 SEP 2026",
        "ETD 9.5", "ETD 6/SEP", "9.5", "ETD: 4 SEP", "9.5托班到9.11",
        "0903 CSCL SPRING E068 ETD SHEKOU 6/SEP",
    ]
    for c in cases:
        print("%-42s -> %s" % (repr(c), parse(c, "2026-08-31")))
