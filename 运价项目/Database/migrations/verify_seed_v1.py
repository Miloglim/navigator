#!/usr/bin/env python3
"""verify_seed_v1.py — 用真实群聊语料反查 seed 分类的证据强度

思路：对 true_alias.csv + enum_norm.csv 每条 (group, alias, kind, target)，
扫业务表 + pending 里的消息文本，看该群消息含此 alias 时 kind 字段的实际入库值分布。
输出 verification.md + verification.csv。
"""
import sys, io, csv, json, re, sqlite3, logging, argparse
from pathlib import Path
from collections import Counter, defaultdict

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')

HERE = Path(__file__).resolve().parent
PROJECT_ROOT = HERE.parent.parent
DB_PATH = PROJECT_ROOT / "Database" / "freight_manual.db"
SEED_DIR = HERE / "seed-v1-out"
SPEC_ROOT = PROJECT_ROOT / "Skill" / "freight-rate-heartbeat" / "references" / "群聊规范"
README_PATH = SPEC_ROOT / "README.md"

log = logging.getLogger("verify-v1")
logging.basicConfig(level=logging.INFO, format="[%(asctime)s] [%(levelname)s] [verify-v1] %(message)s", datefmt="%H:%M:%S")

KIND_TO_FIELD = {
    "pol": "pol", "pod": "pod", "carrier": "carrier", "route": "route",
    "space_type": "space_type", "ctype": "ctype",
}


def load_group_name_map() -> dict:
    """钉钉名 → 目录名（业务表 source_group 用钉钉名，seed.csv 用目录名）"""
    text = README_PATH.read_text(encoding="utf-8")
    m = {}
    for match in re.finditer(r'^\|\s*`([^`]+)`\s*\|\s*([^|]+?)\s*\|\s*`[^`]+`\s*\|', text, re.MULTILINE):
        directory, dingtalk_name = match.group(1), match.group(2).strip()
        m[dingtalk_name] = directory
        m[directory] = directory  # 自身也映射
    return m


def fold(s: str) -> str:
    if not s:
        return ""
    return re.sub(r"[\s.,\-()（）\u3000/、'\"]", "", str(s)).lower()


def _split_aliases(s):
    if not s:
        return []
    return [x.strip() for x in str(s).split(",") if x.strip()]


def _in_snippet(token: str, snippet: str) -> bool:
    """英文 token 用词边界，中文 token 用子串"""
    if not token or not snippet:
        return False
    if re.match(r'^[\x20-\x7e]+$', token):
        pat = re.compile(r'(?<![a-zA-Z0-9])' + re.escape(token) + r'(?![a-zA-Z0-9])', re.IGNORECASE)
        return bool(pat.search(snippet))
    return token in snippet


class VariantIndex:
    """主数据 name → 所有变体 tokens（窗口内匹配用）；支持反向查找"""
    def __init__(self, db_path: Path):
        self.by_name = defaultdict(set)
        self.reverse = defaultdict(set)  # fold(token) → set(name)
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        try:
            for r in conn.execute("SELECT name, name_cn, aliases FROM ports WHERE active=1"):
                self._add(r["name"], r["name"])
                self._add(r["name"], r["name_cn"])
                for a in _split_aliases(r["aliases"]):
                    self._add(r["name"], a)
            for r in conn.execute("SELECT code, name_en, name_cn, aliases FROM carriers WHERE active=1"):
                self._add(r["code"], r["code"])
                self._add(r["code"], r["name_en"])
                self._add(r["code"], r["name_cn"])
                for a in _split_aliases(r["aliases"]):
                    self._add(r["code"], a)
            for r in conn.execute("SELECT name, aliases FROM routes WHERE active=1"):
                self._add(r["name"], r["name"])
                for a in _split_aliases(r["aliases"]):
                    self._add(r["name"], a)
            for r in conn.execute("SELECT name, label FROM port_groups WHERE active=1"):
                self._add(r["name"], r["name"])
                self._add(r["name"], r["label"])
                for tok in re.split(r'[/、（(]', r["label"] or ""):
                    tok = tok.strip().rstrip(')）')
                    if tok:
                        self._add(r["name"], tok)
            for name, vs in list(self.by_name.items()):
                for v in list(vs):
                    if re.match(r'^[\x20-\x7e]+$', v):
                        self._add(name, v.upper())
                        self._add(name, v.lower())
        finally:
            conn.close()
        # 反向索引
        for name, vs in self.by_name.items():
            for v in vs | {name}:
                f = fold(v)
                if f:
                    self.reverse[f].add(name)
        log.info(
            f"变体索引装载: names={len(self.by_name)} total_variants={sum(len(v) for v in self.by_name.values())} reverse_keys={len(self.reverse)}"
        )

    def _add(self, key, val):
        if key and val and isinstance(val, str):
            self.by_name[key].add(val)

    def variants(self, target: str) -> set:
        """target 可以是主数据 name，也可以是它的任一 alias/中文名；返回该实体的全部变体"""
        if not target:
            return set()
        out = {target}
        if target in self.by_name:
            out |= self.by_name[target]
        else:
            names = self.reverse.get(fold(target), set())
            for n in names:
                out.add(n)
                out |= self.by_name[n]
        return {v for v in out if v and len(v) >= 2}

    def is_canonical_name(self, target: str) -> bool:
        return target in self.by_name


def build_matcher(alias: str):
    """返回 positions(text) → [(start_pos, alias_len), ...]；供窗口匹配定位所有出现"""
    is_ascii = bool(re.match(r'^[\x20-\x7e]+$', alias))
    if is_ascii:
        pat = re.compile(r'(?<![a-zA-Z0-9])' + re.escape(alias) + r'(?![a-zA-Z0-9])', re.IGNORECASE)
        def positions(text):
            return [(m.start(), len(alias)) for m in pat.finditer(text or "")]
        return positions
    def positions(text):
        text = text or ""
        out = []
        i = 0
        while True:
            j = text.find(alias, i)
            if j < 0:
                return out
            out.append((j, len(alias)))
            i = j + 1
    return positions


def load_corpus(name_map: dict) -> list:
    """业务表 + pending.payload 合并成统一语料：[{group, pol, pod, carrier, route, ctype, space_type, message_text}]"""
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    corpus = []
    # 业务 rates（freight_rates 无 space_type 列）
    for r in conn.execute("SELECT pol, pod, carrier, route, container_type, source_group, message_text FROM freight_rates"):
        if not r["message_text"]:
            continue
        corpus.append({
            "group": name_map.get(r["source_group"], r["source_group"] or ""),
            "pol": r["pol"] or "",
            "pod": r["pod"] or "",
            "carrier": r["carrier"] or "",
            "route": r["route"] or "",
            "ctype": r["container_type"] or "",
            "space_type": "",
            "message_text": r["message_text"],
            "src": "freight_rates",
        })
    # 业务 space
    for r in conn.execute("SELECT pol, pod, carrier, route, container_type, space_type, source_group, message_text FROM space_records"):
        if not r["message_text"]:
            continue
        corpus.append({
            "group": name_map.get(r["source_group"], r["source_group"] or ""),
            "pol": r["pol"] or "",
            "pod": r["pod"] or "",
            "carrier": r["carrier"] or "",
            "route": r["route"] or "",
            "ctype": r["container_type"] or "",
            "space_type": r["space_type"] or "",
            "message_text": r["message_text"],
            "src": "space_records",
        })
    # pending.payload（全表，包含 awaiting）
    for r in conn.execute("SELECT kind, payload, review_status FROM pending_records"):
        try:
            p = json.loads(r["payload"])
        except Exception:
            continue
        mt = p.get("消息原文") or ""
        if not mt:
            continue
        corpus.append({
            "group": name_map.get(p.get("来源群") or "", p.get("来源群") or ""),
            "pol": p.get("起运港") or "",
            "pod": p.get("目的港") or "",
            "carrier": p.get("船司") or "",
            "route": p.get("航线") or "",
            "ctype": p.get("柜型") or "",
            "space_type": p.get("舱位类型") or "",
            "message_text": mt,
            "src": f"pending:{r['review_status']}",
        })
    conn.close()
    log.info(f"语料装载: {len(corpus)} 条")
    return corpus


WINDOW = 40  # alias 附近多少字符内出现 target 变体才算"窗口印证"（辅助置信信号，非硬门槛）


def verify_one(cand: dict, corpus: list, variants_idx: "VariantIndex") -> dict:
    alias = cand.get("alias") or ""
    kind = cand.get("kind") or ""
    group = cand.get("group") or ""
    target = cand.get("target") or ""
    field = KIND_TO_FIELD.get(kind)
    if not (alias and kind and group and target and field):
        return {**cand, "verdict": "跳过", "in_group_n": 0, "in_group_top3": "", "cross_group_n": 0, "cross_group_top3": "", "cross_warn": "", "near_rate": ""}
    if fold(alias) == fold(target) and variants_idx.is_canonical_name(target):
        return {
            **cand, "verdict": "自映射（应剔除）",
            "in_group_n": 0,
            "in_group_top3": f"{target} 本身就是主数据规范名，直接匹配即可",
            "cross_group_n": 0, "cross_group_top3": "", "cross_warn": "", "near_rate": "",
        }
    variants = variants_idx.variants(target)
    variants_folded = {fold(v) for v in variants if v}
    positions_fn = build_matcher(alias)
    in_all, near_in = [], 0
    cross_all, near_cross = [], 0
    for c in corpus:
        mt = c["message_text"]
        ps = positions_fn(mt)
        if not ps:
            continue
        v = c.get(field) or ""
        if not v:
            continue
        # 窗口检查（辅助置信信号）
        is_near = False
        for pos, alen in ps:
            snip = mt[max(0, pos - WINDOW): pos + alen + WINDOW]
            if any(_in_snippet(tok, snip) for tok in variants):
                is_near = True
                break
        if c["group"] == group:
            in_all.append(v)
            if is_near:
                near_in += 1
        elif c["group"]:
            cross_all.append(v)
            if is_near:
                near_cross += 1
    in_dist = Counter(in_all)
    cross_dist = Counter(cross_all)
    in_top = in_dist.most_common(1)[0] if in_dist else ("", 0)
    # 判定 target 命中：众数的 fold 是否在 target 变体折叠集里
    hit_target = fold(in_top[0]) in variants_folded if in_top[0] else False
    n = len(in_all)
    near_rate = (near_in / n) if n else 0.0
    if n == 0:
        verdict = "无样本"
    elif hit_target and in_top[1] / n >= 0.8 and n >= 5 and near_rate >= 0.5:
        verdict = "强支持（窗口印证）"
    elif hit_target and in_top[1] / n >= 0.8:
        verdict = "强支持" if n >= 5 else "支持"
    elif hit_target:
        verdict = "支持（众数非唯一）"
    else:
        verdict = "⚠️ 反例（众数≠target）"
    cross_warn = ""
    if len(cross_all) >= 3:
        cross_top = cross_dist.most_common(1)[0]
        if fold(cross_top[0]) not in variants_folded and cross_top[1] / len(cross_all) >= 0.7:
            cross_warn = f"跨群歧义：其他群 {len(cross_all)} 样本众数={cross_top[0]}（{cross_top[1]}/{len(cross_all)}）"
    return {
        **cand,
        "in_group_n": n,
        "in_group_top3": "; ".join(f"{k}×{v}" for k, v in in_dist.most_common(3)),
        "near_in": near_in,
        "near_rate": f"{near_rate:.0%}",
        "cross_group_n": len(cross_all),
        "cross_group_top3": "; ".join(f"{k}×{v}" for k, v in cross_dist.most_common(3)),
        "verdict": verdict,
        "cross_warn": cross_warn,
    }


def load_seed(name: str) -> list:
    path = SEED_DIR / name
    if not path.exists():
        return []
    with open(path, encoding="utf-8") as f:
        return list(csv.DictReader(f))


def write_report(results: list, out_dir: Path):
    out_dir.mkdir(parents=True, exist_ok=True)
    keys = ["group", "alias", "kind", "target", "alias_kind", "in_group_n", "in_group_top3", "near_in", "near_rate", "cross_group_n", "cross_group_top3", "verdict", "cross_warn"]
    with open(out_dir / "verification.csv", "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=keys, extrasaction="ignore")
        w.writeheader()
        for r in results:
            w.writerow(r)
    by_verdict = defaultdict(list)
    for r in results:
        by_verdict[r["verdict"]].append(r)
    n = len(results)
    lines = [f"# Seed 验证报告（{n} 条候选）", ""]
    lines.append("## 结论分布")
    for v in ("强支持（窗口印证）", "强支持", "支持", "支持（众数非唯一）", "⚠️ 反例（众数≠target）", "无样本", "自映射（应剔除）", "跳过"):
        items = by_verdict.get(v, [])
        if items:
            lines.append(f"- **{v}**：{len(items)} 条")
    lines.append("")

    def dump_section(title, verdict):
        items = by_verdict.get(verdict, [])
        if not items:
            return
        lines.append(f"## {title}（{len(items)}）")
        lines.append("")
        for r in items:
            warn = f" | {r['cross_warn']}" if r.get("cross_warn") else ""
            nr = f" 窗口印证 {r.get('near_in','')}/{r['in_group_n']}={r.get('near_rate','')}" if r.get('in_group_n') else ""
            lines.append(
                f"- `[{r['group']}] {r['alias']} → {r['target']}` "
                f"({r['kind']}, {r['alias_kind']}) "
                f"本群样本 **{r['in_group_n']}**{nr}，Top3: `{r['in_group_top3'] or '—'}` "
                f"跨群样本 {r['cross_group_n']}{warn}"
            )
        lines.append("")

    dump_section("⚠️ 反例：入库众数 ≠ seed target（需要人工裁定或 seed 有误）", "⚠️ 反例（众数≠target）")
    warned = [r for r in results if r.get("cross_warn")]
    if warned:
        lines.append(f"## 跨群歧义清单（{len(warned)} 条）")
        for r in warned:
            lines.append(f"- `[{r['group']}] {r['alias']} → {r['target']}` ({r['kind']}) {r['cross_warn']}")
        lines.append("")
    dump_section("强支持（窗口印证）：>=5 样本 + 众数>=80% + 窗口内 target 变体覆盖 >=50%", "强支持（窗口印证）")
    dump_section("强支持：>=5 样本 + 众数>=80%（无窗口印证）", "强支持")
    dump_section("支持：>=1 样本 + 众数=target", "支持")
    dump_section("支持（众数非唯一）", "支持（众数非唯一）")
    dump_section("无样本：本群消息里从没写过该 alias", "无样本")
    dump_section("自映射：alias 与 target 相同且 target 就是主数据规范名（alias_scope 冗余，建议剔除）", "自映射（应剔除）")
    lines.append("---")
    lines.append(f"生成：verify_seed_v1.py（窗口={WINDOW} 字符；变体源=主数据 name + name_cn + aliases + port_groups.label + 大小写；反向索引 fold→name）")
    (out_dir / "verification.md").write_text("\n".join(lines), encoding="utf-8")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()
    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    name_map = load_group_name_map()
    variants_idx = VariantIndex(DB_PATH)
    corpus = load_corpus(name_map)

    results = []
    for f in ("true_alias.csv", "enum_norm.csv"):
        rows = load_seed(f)
        log.info(f"验证 {f}：{len(rows)} 条")
        for row in rows:
            results.append(verify_one(row, corpus, variants_idx))

    write_report(results, SEED_DIR)
    dist = Counter(r["verdict"] for r in results)
    log.info(f"完成。verdict 分布: {dict(dist)}")
    log.info(f"报告: {SEED_DIR/'verification.md'}")


if __name__ == "__main__":
    main()
