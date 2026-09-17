#!/usr/bin/env python3
"""price_sanitize.py — 价格向量纠错（从便携版 ocr_logic.py 移植核心逻辑）

用途：AI/record_builder 生成 pending 时，若某条海运费明显超合理区间（如把 40HQ/HC
一档写成 136000 而不是 13600），删一位数字能得到与同群/同批次其他价高度一致的
唯一高置信候选 → 只给提示、不自动改写（memory 铁律：审核动作可追溯、原文不改）。

三个函数：
  single_digit_deletion_candidates(price)  枚举删一位数字的所有候选
  suggest_price_correction(price, peers)   返回 {suggested, confidence, reason, auto_apply=False}
  annotate_batch(records, price_key)       批内互相比对，就地给超区间的 rec 打 _price_suggestion
"""
from collections import defaultdict
from typing import Any, Dict, List, Optional
import sys
try:
    sys.stdout.reconfigure(encoding='utf-8')
except Exception:
    pass

USD_PRICE_MAX = 30000
REL_THRESHOLD = 0.25      # 相对中位数差 <=25% 才算候选
MIN_CONFIDENCE = 0.70     # 审核参考门槛；不达则 suggested=None 避免误导
RUNNER_GAP = 0.15         # 与亚军差距 <15% 时 reason 加"谨慎参考"提示，不阻断建议


def single_digit_deletion_candidates(price: int) -> List[int]:
    """把 price 每一位数字各删一次，去重保序返回。136000 → [36000, 16000, 13000, 13600]"""
    if price is None:
        return []
    s = str(abs(int(price)))
    if len(s) < 2:
        return []
    out, seen = [], set()
    for i in range(len(s)):
        cand = s[:i] + s[i + 1:]
        if not cand:
            continue
        v = int(cand)
        if v > 0 and v not in seen:
            seen.add(v)
            out.append(v)
    return out


def median(values: List[int]) -> Optional[float]:
    vs = sorted(v for v in values if isinstance(v, int) and v > 0)
    n = len(vs)
    if n == 0:
        return None
    if n % 2 == 1:
        return float(vs[n // 2])
    return (vs[n // 2 - 1] + vs[n // 2]) / 2.0


def suggest_price_correction(price: Any, peer_prices: List[int],
                             usd_max: int = USD_PRICE_MAX,
                             rel_threshold: float = REL_THRESHOLD,
                             min_confidence: float = MIN_CONFIDENCE,
                             runner_gap: float = RUNNER_GAP) -> Dict[str, Any]:
    """单个价 → 建议纠错。返回 {suggested, confidence, reason, auto_apply}。
    auto_apply 恒 False（memory 铁律：不覆盖原图/原文证据，人工审核时参考）。"""
    try:
        p = int(price)
    except (TypeError, ValueError):
        return {"suggested": None, "confidence": 0, "reason": "非整数，交给闸门", "auto_apply": False}
    if p <= usd_max:
        return {"suggested": None, "confidence": 0, "reason": "正常区间", "auto_apply": False}
    peers = [x for x in peer_prices
             if isinstance(x, int) and 0 < x <= usd_max and x != p]
    if not peers:
        return {"suggested": None, "confidence": 0,
                "reason": f"超区间 {p}>{usd_max}，但同批次无相邻价可参照", "auto_apply": False}
    ref = median(peers)
    if ref is None:
        return {"suggested": None, "confidence": 0, "reason": "中位数无有效值", "auto_apply": False}
    cands = single_digit_deletion_candidates(p)
    scored = []
    for c in cands:
        rel = abs(c - ref) / max(ref, 1)
        if rel <= rel_threshold:
            conf = max(0.0, 1.0 - rel / rel_threshold)
            scored.append((c, conf, rel))
    if not scored:
        return {"suggested": None, "confidence": 0,
                "reason": f"删一位仍与中位数 {int(ref)} 相对差 > {rel_threshold:.0%}",
                "auto_apply": False}
    scored.sort(key=lambda x: -x[1])
    best, best_conf, best_rel = scored[0]
    runner = scored[1] if len(scored) > 1 else None
    if best_conf < min_confidence:
        return {"suggested": None, "confidence": best_conf,
                "reason": f"候选 {best} 置信度 {best_conf:.2f} < 门槛 {min_confidence:.2f}，不给建议（避免误导审核员）",
                "auto_apply": False}
    hint = ""
    if runner and (best_conf - runner[1]) < runner_gap:
        hint = f"（与亚军 {runner[0]} 差距 {best_conf - runner[1]:.2f} < {runner_gap}，谨慎参考）"
    return {"suggested": best, "confidence": best_conf,
            "reason": f"疑似多打一位（{p} → {best}，中位数 {int(ref)}，相对差 {best_rel*100:.1f}%）{hint}",
            "auto_apply": False}


def annotate_batch(records: List[Dict[str, Any]],
                   price_key: str = "海运费USD",
                   group_key: str = "来源群") -> int:
    """就地给 records 里超区间的价挂 _price_suggestion 字段。
    参照集 = 同批次同 group_key 下所有 <= USD_PRICE_MAX 的正整数。
    返回被打上建议的条数。"""
    from collections import defaultdict
    peers_by_group: Dict[str, List[int]] = defaultdict(list)
    for r in records:
        try:
            p = int(r.get(price_key))
        except (TypeError, ValueError):
            continue
        if 0 < p <= USD_PRICE_MAX:
            peers_by_group[str(r.get(group_key) or "")].append(p)
    hit = 0
    for r in records:
        try:
            p = int(r.get(price_key))
        except (TypeError, ValueError):
            continue
        if p <= USD_PRICE_MAX:
            continue
        peers = peers_by_group.get(str(r.get(group_key) or ""), [])
        s = suggest_price_correction(p, peers)
        if s["suggested"] is not None or "超区间" in s["reason"]:
            r["_price_suggestion"] = s
            hit += 1
    return hit


if __name__ == "__main__":
    # 自测：便携版经典案例
    print("--- suggest_price_correction 单测 ---")
    cases = [
        # (原值, 邻居, 期望 suggested)
        (136000, [13400, 13600, 13400], 13600),
        (34000,  [3400, 3600, 3500],    3400),
        (120000, [12000, 12500, 13000], 12000),
        (99999,  [8000, 8500, 9000],    None),  # 删一位仍差太远
        (15000,  [],                     None),  # 无邻居
        (1200,   [1200, 1300],           None),  # 正常区间
    ]
    for price, peers, want in cases:
        s = suggest_price_correction(price, peers)
        got = s["suggested"]
        mark = "✓" if got == want else "✗"
        print(f"  {mark} price={price} peers={peers} → suggested={got} want={want}  reason={s['reason']}")

    print()
    print("--- annotate_batch 集成例 ---")
    batch = [
        {"来源群": "土耳其价格更新", "海运费USD": 13400, "柜型": "20GP"},
        {"来源群": "土耳其价格更新", "海运费USD": 13600, "柜型": "40HQ/HC"},
        {"来源群": "土耳其价格更新", "海运费USD": 136000, "柜型": "40NOR"},  # 异常
    ]
    n = annotate_batch(batch)
    print(f"  命中 {n} 条建议")
    for r in batch:
        sug = r.get("_price_suggestion")
        print(f"  {r['海运费USD']:>7} {'→ 建议 ' + str(sug['suggested']) + ' / 置信 ' + format(sug['confidence'], '.2f') if sug else '正常'}")
