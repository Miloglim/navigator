#!/usr/bin/env python3
"""seed_alias_scope_v1.py — 从 11 份群规范 markdown 提取 alias_scope 候选

用途：dry-run 分四类输出草稿清单，交人工 review 后再决定写库与 DDL 迁移。
不写数据库；不改现有表；不改现有代码。

分类：
  - true_alias   写法 → 规范名，target 能在 ports.name / carriers.code / routes.name 命中
  - enum_norm    写法 → 枚举成员（舱位类型 11 档、柜型 3 档、路线 25 条 等）
  - disposal     "拆两条/进备注/剥掉/不自算/留空" 等指令，不是 alias→target 映射
  - unclassified 多 token 但目标不匹配、无法解析、待补 — 需人工裁定

用法：
  python seed_alias_scope_v1.py                     # dry-run
  python seed_alias_scope_v1.py --out DIR           # 换输出目录
"""
import sys, io, csv, json, re, sqlite3, logging, argparse
from pathlib import Path
from collections import defaultdict

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')

# ---- 路径常量 ----
HERE = Path(__file__).resolve().parent
PROJECT_ROOT = HERE.parent.parent                       # 运价项目/
DB_PATH = PROJECT_ROOT / "Database" / "freight_manual.db"
SPEC_ROOT = PROJECT_ROOT / "Skill" / "freight-rate-heartbeat" / "references" / "群聊规范"
README_PATH = SPEC_ROOT / "README.md"
DEFAULT_OUT = HERE / "seed-v1-out"

# ---- 业务枚举（与 manualdb.py 保持同步） ----
KIND_MAP = {
    "起运港": "pol",
    "目的港": "pod",
    "航线": "route",
    "船司": "carrier",
    "舱位类型": "space_type",
    "柜型": "ctype",
    "价格": "price",
    "备注": "remark",
    "船期": "schedule",
}
# 现有 DDL CHECK 仅允许 4 值；这里 8 值是目标态，DDL 迁移见方案文档
KINDS_IN_DDL = {"pol", "pod", "carrier", "route"}
KINDS_NEED_MIGRATION = {"space_type", "ctype", "price", "remark", "schedule"}

SPACE_TYPE_ENUM = {
    "现舱", "加班船", "放舱开放", "售罄", "舱位紧张",
    "截关截单", "撤载改期", "箱子动态", "空班", "跳港", "首航",
}
# 规范档名三档（对齐 manualdb.py:273 CTYPE_ENUM）—— 40HQ/HC 就是一个档名，不是复合值
CTYPE_ENUM = {"20GP", "40HQ/HC", "40NOR"}
# 用户可写的合法形态（会归到规范档），来自 manualdb.py:280 CTYPE_FORMS
CTYPE_FORMS = {"20GP", "20", "40HQ/HC", "40HQ", "40HC", "40HQ-HC",
               "40NOR", "40N/HC", "40NOR/HC", "40N"}
CTYPE_CANON = {
    "20GP": "20GP", "20": "20GP",
    "40HQ/HC": "40HQ/HC", "40HQ": "40HQ/HC", "40HC": "40HQ/HC", "40HQ-HC": "40HQ/HC",
    "40NOR": "40NOR", "40N": "40NOR", "40N/HC": "40NOR", "40NOR/HC": "40NOR",
}

DISPOSAL_KEYWORDS = [
    "拆", "分两条", "进备注", "剥", "不自算", "留空",
    "交台账", "交人工", "传串", "不判重", "另存", "写进",
    "不是港口", "不是船名", "不是航线", "不是别名",
    "顺序待补", "待补", "先跟航管确认",
]

# ---- 正则 ----
BACKTICK_TOKEN = re.compile(r'`([^`]+)`')
MD_TABLE_ROW = re.compile(r'^\|(.+)\|\s*$')
MD_TABLE_SEP = re.compile(r'^\|[-:\s|]+\|\s*$')
SECTION_HEAD = re.compile(r'^##\s+(.+?)\s*$', re.MULTILINE)
# "同「华南-拉美」" / "同 华南-拉美"
INHERIT_RE = re.compile(r'^\s*同\s*[「""]?([^」""\s|]+?)[」""]?\s*$')

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] [%(levelname)s] [seed-v1] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("seed-v1")


def read_groups_from_readme(path: Path) -> dict:
    """从 README 表解析 目录名 → {群名, slug, default_pol}"""
    text = path.read_text(encoding="utf-8")
    result = {}
    for m in re.finditer(r'^\|\s*`([^`]+)`\s*\|\s*([^|]+?)\s*\|\s*`([^`]+)`\s*\|\s*([^|]+?)\s*\|\s*$', text, re.MULTILINE):
        directory, dingtalk_name, slug, default_pol = m.groups()
        result[directory] = {
            "dingtalk_name": dingtalk_name.strip(),
            "slug": slug.strip(),
            "default_pol": default_pol.strip(),
        }
    log.info(f"README 表解析得 {len(result)} 群")
    return result


def extract_section(text: str, heading: str) -> str:
    """取 '## <heading>' 到下一个 '## ' 之间的正文"""
    heads = list(SECTION_HEAD.finditer(text))
    for i, h in enumerate(heads):
        if heading in h.group(1):
            start = h.end()
            end = heads[i + 1].start() if i + 1 < len(heads) else len(text)
            return text[start:end].strip()
    return ""


def parse_md_table(md: str) -> list:
    """markdown 表格 → list[list[str]]，跳过分隔行；返回列值 strip 后的原始行"""
    rows = []
    for line in md.splitlines():
        line = line.strip()
        if not line.startswith("|"):
            continue
        if MD_TABLE_SEP.match(line):
            continue
        m = MD_TABLE_ROW.match(line)
        if not m:
            continue
        cells = [c.strip() for c in m.group(1).split("|")]
        rows.append(cells)
    if rows and rows[0][:2] == ["写法", "本群含义"]:
        rows = rows[1:]
    return rows


def tokens_from_alias_cell(cell: str) -> list:
    """写法列 → [alias_token, ...]"""
    toks = [t.strip() for t in BACKTICK_TOKEN.findall(cell)]
    if toks:
        return toks
    # 无反引号时把整格当一个 alias（"同「华南-拉美」" 等）
    stripped = cell.strip()
    if stripped and stripped != "—":
        return [stripped]
    return []


TYPED_TOKEN_RE = re.compile(r'(?:舱位类型|柜型|航线|船司|目的港|起运港)\s*`([^`]+)`')
SLASH_OR_DUN = re.compile(r'\s*[/、]\s*')

def tokens_from_target_cell(cell: str) -> list:
    """归一目标列 → 反引号 token 列表；无反引号时按 '/' 顿号 '、' 拆中文"""
    toks = [t.strip() for t in BACKTICK_TOKEN.findall(cell)]
    if toks:
        return toks
    # 无 ` 的中文说明，按 '/' 或 '、' 拆
    parts = re.split(r'[/、]', cell)
    parts = [p.strip() for p in parts if p.strip() and p.strip() != "—"]
    return parts


def tokens_from_typed_pattern(cell: str) -> list:
    """从'舱位类型 `X`'/'柜型 `X/Y`' 这类带类型前缀的表达里抠 X。
    注意：40HQ/HC 是**规范档名**（三档之一），整体保留、不能按斜杠拆；
    其他带斜杠的（如 20GP/40HQ 表并列多档）按斜杠/顿号拆。"""
    out = []
    for m in TYPED_TOKEN_RE.finditer(cell):
        inner = m.group(1).strip()
        if inner.upper() in {x.upper() for x in CTYPE_FORMS} | {x.upper() for x in CTYPE_ENUM}:
            out.append(inner)
            continue
        for sub in SLASH_OR_DUN.split(inner):
            sub = sub.strip()
            if sub:
                out.append(sub)
    return out


def meaning_tokens(cell: str) -> list:
    """从"本群含义"列按 '/' 拆（宁波 `NB/SH/TJ/DL/XM` 那行 = '宁波 / 上海 / 天津 / 大连 / 厦门'）"""
    parts = SLASH_OR_DUN.split(cell)
    return [p.strip() for p in parts if p.strip() and p.strip() not in ("—", "")]


def looks_like_disposal_pattern(cell: str) -> bool:
    """额外的处置指令特征"""
    if not cell:
        return False
    # "船期ETD=2026-09-15 + 原文进 备注" 或 "`船期ETD=...`" 反引号包裹
    if 'ETD=' in cell:
        return True
    # "+ENS / 含OWS → 备注" 这类"某前缀 → 某字段"，本质是清洗规则
    if re.match(r'^\s*[+＋]', cell.strip('`')):
        return True
    if re.match(r'^\s*含', cell.strip('`')):
        return True
    # 整表"待补"
    if cell.strip() in ("待补", "—", "-", "— / — / —"):
        return True
    return False


def map_kind(scope_text: str) -> str:
    """作用域列 → kind 字符串（'unknown' 若没匹配）"""
    t = scope_text.strip().strip('—-')
    if not t:
        return "unknown"
    for cn, en in KIND_MAP.items():
        if cn in t:
            return en
    return "unknown"


def is_disposal(target_cell: str, meaning_cell: str) -> bool:
    """目标/含义列含处置关键词"""
    text = target_cell + " " + meaning_cell
    for kw in DISPOSAL_KEYWORDS:
        if kw in text:
            return True
    return False


FUZZY_TARGET_RE = re.compile(r'对应规范名|待核|待补|已登记别名|规范名$|^\*\*|^\s*$')
DISPOSAL_IN_ALIAS_RE = re.compile(r'^\s*[+＋]|^\s*含')


class LookupIndex:
    """用主数据表判 target 是否命中；含 name / name_cn / aliases 三层映射"""
    def __init__(self, db_path: Path):
        if not db_path.exists():
            log.warning(f"DB 不存在 {db_path}，target 命中检查将全部 miss")
            self.ports = self.carriers = self.routes = self.port_groups = set()
            self.cn_to_en = {}
            self.alias_index = {}
            return
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        self.cn_to_en = {}
        self.alias_index = {}  # fold(token) → (label, name)
        try:
            # ports
            self.ports = set()
            for r in conn.execute("SELECT name, name_cn, aliases FROM ports WHERE active=1"):
                self.ports.add(r["name"])
                if r["name_cn"]:
                    self.cn_to_en[r["name_cn"]] = r["name"]
                    self._add(r["name_cn"], "port", r["name"])
                self._add(r["name"], "port", r["name"])
                for a in _split_aliases(r["aliases"]):
                    self._add(a, "port", r["name"])
            # carriers
            self.carriers = set()
            for r in conn.execute("SELECT code, name_en, name_cn, aliases FROM carriers WHERE active=1"):
                self.carriers.add(r["code"])
                self._add(r["code"], "carrier", r["code"])
                if r["name_en"]: self._add(r["name_en"], "carrier", r["code"])
                if r["name_cn"]: self._add(r["name_cn"], "carrier", r["code"])
                for a in _split_aliases(r["aliases"]):
                    self._add(a, "carrier", r["code"])
            # routes
            self.routes = set()
            for r in conn.execute("SELECT name, aliases FROM routes WHERE active=1"):
                self.routes.add(r["name"])
                self._add(r["name"], "route", r["name"])
                for a in _split_aliases(r["aliases"]):
                    self._add(a, "route", r["name"])
            # port_groups（无 aliases 列，只有 name/label/members/specials/kind/active/note）
            self.port_groups = set()
            for r in conn.execute("SELECT name, label FROM port_groups WHERE active=1"):
                self.port_groups.add(r["name"])
                self._add(r["name"], "port_group", r["name"])
                if r["label"]: self._add(r["label"], "port_group", r["name"])
        finally:
            conn.close()
        log.info(
            f"索引装载: ports={len(self.ports)} carriers={len(self.carriers)} routes={len(self.routes)} "
            f"port_groups={len(self.port_groups)} alias_index={len(self.alias_index)}"
        )

    def _add(self, token: str, label: str, name: str):
        if not token:
            return
        f = _fold(token)
        if not f:
            return
        # 保留所有候选（fold 冲突由 match 按 kind 挑选）
        bucket = self.alias_index.setdefault(f, [])
        if (label, name) not in bucket:
            bucket.append((label, name))

    def resolve_chinese(self, s: str) -> str:
        return self.cn_to_en.get(s, s)

    def match(self, kind: str, target: str) -> str:
        """返回命中的类型标签 'port'/'carrier'/'route'/'port_group'/''"""
        if not target:
            return ""
        bucket = self.alias_index.get(_fold(target)) or []
        if not bucket:
            return ""
        labels = [lbl for lbl, _ in bucket]
        if kind in ("pol", "pod"):
            for prefer in ("port_group", "port"):
                if prefer in labels:
                    return prefer
            return ""
        if kind in ("carrier", "route") and kind in labels:
            return kind
        if kind in ("unknown", ""):
            return labels[0]
        return ""

    def find_by_alias(self, kind: str, alias: str) -> str:
        """target 无法确定时，看 alias 本身能否在主数据里找到规范名"""
        bucket = self.alias_index.get(_fold(alias)) or []
        if not bucket:
            return ""
        if kind in ("pol", "pod"):
            for prefer in ("port_group", "port"):
                for lbl, name in bucket:
                    if lbl == prefer:
                        return name
            return ""
        if kind in ("carrier", "route"):
            for lbl, name in bucket:
                if lbl == kind:
                    return name
            return ""
        # remark / schedule / price / unknown → 任一候选
        return bucket[0][1]

    def is_canonical_name(self, target: str) -> bool:
        """target 是不是主数据里某个实体的规范名（不是 alias）"""
        f = _fold(target)
        if not f:
            return False
        for _lbl, name in self.alias_index.get(f, []):
            if _fold(name) == f:
                return True
        return False


def _split_aliases(s):
    if not s:
        return []
    return [x.strip() for x in str(s).split(",") if x.strip()]


def _fold(s: str) -> str:
    """简化版 manualdb.fold：小写 + 去空白 + 去常见标点。生产代码用 manualdb.fold"""
    if s is None:
        return ""
    return re.sub(r"[\s.,\-()（）\u3000/、'\"]", "", str(s)).lower()


def classify_row(row_cells: list, group_dir: str, idx: LookupIndex, source_line_hint: str = "") -> list:
    """
    一行表格 → 若干条候选 dict
    row_cells 长度期望 >= 4: [写法, 含义, 归一目标, 作用域]
    """
    if len(row_cells) < 4:
        return [{
            "group": group_dir,
            "alias": " | ".join(row_cells),
            "kind": "unknown",
            "alias_kind": "unclassified",
            "target": "",
            "note": f"列数不足（{len(row_cells)}），需人工裁定",
            "source": source_line_hint,
        }]
    alias_cell, meaning_cell, target_cell, scope_cell = row_cells[0], row_cells[1], row_cells[2], row_cells[3]
    kind = map_kind(scope_cell)
    aliases = tokens_from_alias_cell(alias_cell)
    targets = tokens_from_target_cell(target_cell)

    if not aliases:
        return []

    # "同 XX 群" 继承整行
    if len(aliases) == 1:
        m = INHERIT_RE.match(aliases[0])
        if m:
            return [{
                "group": group_dir,
                "alias": aliases[0],
                "kind": kind,
                "alias_kind": "inherit",
                "target": m.group(1),
                "note": f"引用继承自 {m.group(1)}，seed 需按引用目标群规范合并",
                "source": source_line_hint,
            }]

    # 处置指令优先判定（含新正则）
    if (is_disposal(target_cell, meaning_cell)
            or looks_like_disposal_pattern(target_cell)
            or any(DISPOSAL_IN_ALIAS_RE.match(a) for a in aliases)):
        return [{
            "group": group_dir,
            "alias": aliases[0] if len(aliases) == 1 else " | ".join(aliases),
            "kind": kind,
            "alias_kind": "disposal",
            "target": target_cell,
            "note": "非 alias→target，属拆解/清洗/兜底指令",
            "source": source_line_hint,
        }]

    # 枚举 kind：先尝试 typed_pattern 抠真 target，再回落到"写法=目标"
    if kind in ("space_type", "ctype"):
        typed = tokens_from_typed_pattern(target_cell)
        if kind == "ctype":
            # 合法形态（40HQ/40HC/40N 等）归一到规范档名（40HQ/HC / 40NOR）
            typed = [CTYPE_CANON.get(t, t) for t in typed]
        enum_set = SPACE_TYPE_ENUM if kind == "space_type" else CTYPE_ENUM
        out = []
        for a in aliases:
            target_for_this = None
            if typed:
                if len(typed) == len(aliases):
                    target_for_this = typed[aliases.index(a)]
                else:
                    for t in typed:
                        if t in enum_set:
                            target_for_this = t
                            break
                    if not target_for_this:
                        target_for_this = typed[0]
            else:
                # alias 自身归一（如 20→20GP、40HQ→40HQ/HC）
                target_for_this = CTYPE_CANON.get(a, a) if kind == "ctype" else a
            in_enum = target_for_this in enum_set
            out.append({
                "group": group_dir,
                "alias": a,
                "kind": kind,
                "alias_kind": "enum_norm" if in_enum else "unclassified",
                "target": target_for_this,
                "note": "target 由 typed_pattern 抠出+归一" if (typed and kind == "ctype") else ("target 由 typed_pattern 抠出" if typed else ("" if in_enum else "写法不在枚举且无法从目标列提取")),
                "source": source_line_hint,
            })
        return out

    # pol/pod/route/carrier：一一对应尝试
    # 目标数 > 别名数：
    #   a) 单 alias + 多 target 且 target/含义列含"两条/合并/每行存"等信号 + 每 target 都命中主数据同类实体 → "一片多港群"展开为多条
    #   b) 其他情况 → 按别名数截取，尾部视为附加说明（如"（航线 X）"这种括号说明）
    EXPAND_SIGNAL = re.compile(r'两条|三条|四条|多港|多群|合并|每行存|每行各')
    if len(targets) > len(aliases):
        if (
            len(aliases) == 1
            and EXPAND_SIGNAL.search(target_cell + " " + meaning_cell)
            and all(idx.match(kind, t) for t in targets)
        ):
            # 一条记录、target 用顿号拼多值串（主键 (alias,kind,scope_group) 唯一，展开多值交给 expand_cn）
            only_alias = aliases[0]
            matches = [idx.match(kind, t) for t in targets]
            resolved_targets = [
                idx.resolve_chinese(t) if re.search(r'[一-鿿]', t) else t
                for t in targets
            ]
            return [{
                "group": group_dir,
                "alias": only_alias,
                "kind": kind,
                "alias_kind": "true_alias",
                "target": "、".join(resolved_targets),
                "note": f"一片多港群：{only_alias} → 多值串 {resolved_targets}，expand_cn 消费时按顿号拆行",
                "source": source_line_hint,
            }]
        extras = targets[len(aliases):]
        targets = targets[:len(aliases)]
        log.debug(f"{group_dir}: 目标 {len(extras)} 项作为附加说明剔除: {extras}")

    if len(aliases) > 1 and len(targets) != len(aliases):
        # 用"含义"列按 / 拆补 target（宁波 NB/SH/TJ/DL/XM 那行 = 宁波/上海/天津/大连/厦门）
        m_tokens = meaning_tokens(meaning_cell)
        if len(m_tokens) == len(aliases):
            targets = m_tokens
            log.debug(f"{group_dir}: 从含义列按 / 拆对齐 {len(aliases)} target: {targets}")

    # 多 alias + 单 target 且 target 有效命中 → 展开为多条 alias→同一 target
    if len(aliases) > 1 and len(targets) == 1:
        only_target = targets[0]
        match = idx.match(kind, only_target)
        if match:
            out = []
            for a in aliases:
                out.append({
                    "group": group_dir,
                    "alias": a,
                    "kind": kind,
                    "alias_kind": "true_alias",
                    "target": idx.resolve_chinese(only_target) if re.search(r'[一-鿿]', only_target) else only_target,
                    "note": f"多写法展开：{len(aliases)} 写法均指向 {only_target}（{match}）",
                    "source": source_line_hint,
                })
            return out
        # target 是 fuzzy（"对应规范名"/"待补"等）→ 逐个 alias 用 find_by_alias 展开
        if FUZZY_TARGET_RE.search(only_target):
            out = []
            unresolved = []
            for a in aliases:
                resolved_name = idx.find_by_alias(kind, a)
                if resolved_name:
                    out.append({
                        "group": group_dir, "alias": a, "kind": kind,
                        "alias_kind": "true_alias", "target": resolved_name,
                        "note": f"target fuzzy（{only_target}），alias 在主数据自命中 {resolved_name}",
                        "source": source_line_hint,
                    })
                else:
                    unresolved.append(a)
            for a in unresolved:
                out.append({
                    "group": group_dir, "alias": a, "kind": kind,
                    "alias_kind": "unclassified", "target": "",
                    "note": f"target fuzzy（{only_target}）且 alias 未在主数据命中，需人工补",
                    "source": source_line_hint,
                })
            return out

    # 多 alias + 多 target 数量对齐 → 走 zip；一 alias 一 target 走单分支
    if len(aliases) == 1 and len(targets) >= 1:
        alias = aliases[0]
        primary = targets[0]
        match = idx.match(kind, primary)
        # fuzzy target（"对应规范名"、"待核"、"待补"）→ 用 alias 自己在主数据找
        if not match and FUZZY_TARGET_RE.search(primary):
            resolved_name = idx.find_by_alias(kind, alias)
            if resolved_name:
                return [{
                    "group": group_dir, "alias": alias, "kind": kind,
                    "alias_kind": "true_alias", "target": resolved_name,
                    "note": f"target 描述模糊（{primary}），alias 在主数据自命中 {resolved_name}",
                    "source": source_line_hint,
                }]
        alias_kind = "true_alias" if match else "unclassified"
        # 中文 target → 转英文 name
        resolved_target = idx.resolve_chinese(primary) if re.search(r'[一-鿿]', primary) else primary
        note = f"target 命中 {match}" if match else "target 未在主数据命中，需人工裁定 target"
        return [{
            "group": group_dir,
            "alias": alias,
            "kind": kind,
            "alias_kind": alias_kind,
            "target": resolved_target,
            "note": note + (f"；备选 targets={targets[1:]}" if len(targets) > 1 else ""),
            "source": source_line_hint,
        }]

    if len(aliases) > 1 and len(targets) == len(aliases):
        out = []
        for a, t in zip(aliases, targets):
            match = idx.match(kind, t)
            resolved_t = idx.resolve_chinese(t) if re.search(r'[一-鿿]', t) else t
            # fuzzy target 兜底
            if not match and FUZZY_TARGET_RE.search(t):
                resolved_name = idx.find_by_alias(kind, a)
                if resolved_name:
                    out.append({
                        "group": group_dir, "alias": a, "kind": kind,
                        "alias_kind": "true_alias", "target": resolved_name,
                        "note": f"target 描述模糊（{t}），alias 在主数据自命中 {resolved_name}",
                        "source": source_line_hint,
                    })
                    continue
            out.append({
                "group": group_dir,
                "alias": a,
                "kind": kind,
                "alias_kind": "true_alias" if match else "unclassified",
                "target": resolved_t,
                "note": f"target 命中 {match}" if match else "target 未在主数据命中",
                "source": source_line_hint,
            })
        return out

    # 多 alias + 少 target 或 target 含解释文字 → 拆出能确定的、其余进 unclassified
    out = []
    if targets:
        out.append({
            "group": group_dir,
            "alias": aliases[0],
            "kind": kind,
            "alias_kind": "unclassified",
            "target": targets[0],
            "note": f"写法 {len(aliases)} 项 / 目标 {len(targets)} 项，只能人工一一对应",
            "source": source_line_hint,
        })
        for a in aliases[1:]:
            out.append({
                "group": group_dir,
                "alias": a,
                "kind": kind,
                "alias_kind": "unclassified",
                "target": "",
                "note": "同上一行，target 缺失需人工补",
                "source": source_line_hint,
            })
    else:
        for a in aliases:
            out.append({
                "group": group_dir,
                "alias": a,
                "kind": kind,
                "alias_kind": "unclassified",
                "target": "",
                "note": "目标列无可提取 token",
                "source": source_line_hint,
            })
    return out


def process_one_spec(path: Path, group_dir: str, idx: LookupIndex) -> list:
    text = path.read_text(encoding="utf-8")
    section = extract_section(text, "缩写表")
    if not section:
        return [{
            "group": group_dir,
            "alias": "",
            "kind": "unknown",
            "alias_kind": "unclassified",
            "target": "",
            "note": "本群规范无'缩写表'节或节为空",
            "source": str(path),
        }]
    rows = parse_md_table(section)
    out = []
    for i, r in enumerate(rows, 1):
        out.extend(classify_row(r, group_dir, idx, source_line_hint=f"{path.name}:缩写表第{i}行"))
    return out


def write_outputs(cands: list, out_dir: Path, groups_meta: dict, idx: LookupIndex):
    out_dir.mkdir(parents=True, exist_ok=True)

    def dump(name, subset):
        path = out_dir / name
        if not subset:
            path.write_text("(空)\n", encoding="utf-8")
            return
        keys = ["group", "alias", "kind", "alias_kind", "target", "note", "source"]
        with open(path, "w", encoding="utf-8", newline="") as f:
            w = csv.DictWriter(f, fieldnames=keys)
            w.writeheader()
            for row in subset:
                w.writerow({k: row.get(k, "") for k in keys})

    # 后处理：从 true_alias 里分流自映射（fold(alias)==fold(target) 且 target 是主数据 name）
    self_map = []
    kept_true_alias = []
    for c in cands:
        if c["alias_kind"] != "true_alias":
            continue  # 其他类别走 by_kind，不参与自映射分流
        if _fold(c["alias"]) == _fold(c["target"]) and idx.is_canonical_name(c["target"]):
            c2 = dict(c)
            c2["alias_kind"] = "self_map"
            c2["note"] = f"自映射：{c['target']} 本身就是主数据规范名，alias_scope 冗余"
            self_map.append(c2)
        else:
            kept_true_alias.append(c)

    by_kind = defaultdict(list)
    for c in cands:
        if c["alias_kind"] == "true_alias":
            continue  # 已分流
        by_kind[c["alias_kind"]].append(c)
    by_kind["self_map"] = self_map

    dump("true_alias.csv", kept_true_alias)
    dump("enum_norm.csv", by_kind["enum_norm"])
    dump("disposal.csv", by_kind["disposal"])
    dump("unclassified.csv", by_kind["unclassified"])
    dump("inherit.csv", by_kind["inherit"])
    dump("self_map.csv", by_kind["self_map"])

    # 统计 + DDL 缺口（不含 self_map；它们是冗余、不入库）
    real_candidates = kept_true_alias + by_kind["enum_norm"]
    need_ddl = [c for c in real_candidates if c["kind"] in KINDS_NEED_MIGRATION]
    in_ddl = [c for c in real_candidates if c["kind"] in KINDS_IN_DDL]

    stats = {
        "true_alias": len(kept_true_alias),
        "enum_norm": len(by_kind["enum_norm"]),
        "disposal": len(by_kind["disposal"]),
        "unclassified": len(by_kind["unclassified"]),
        "inherit": len(by_kind["inherit"]),
        "self_map": len(self_map),
        "total": len(cands),
        "groups_covered": len({c["group"] for c in cands if c["group"]}),
        "ddl_ready": len(in_ddl),
        "ddl_pending": len(need_ddl),
    }
    (out_dir / "summary.json").write_text(json.dumps(stats, ensure_ascii=False, indent=2), encoding="utf-8")

    log.info(
        f"输出统计: true_alias={stats['true_alias']} enum_norm={stats['enum_norm']} "
        f"self_map={stats['self_map']}（冗余不入库） disposal={stats['disposal']} "
        f"unclassified={stats['unclassified']} inherit={stats['inherit']} | "
        f"DDL 支持={stats['ddl_ready']} DDL 待扩={stats['ddl_pending']}"
    )


def write_to_db(seed_dir: Path, groups_meta: dict, conn) -> dict:
    """把 true_alias + enum_norm + inherit(展开) 写入 alias_scope。scope_group 用钉钉群名。"""
    cur = conn.cursor()
    stats = {"inserted": 0, "skipped_no_group": 0, "inherit_expanded": 0, "by_group": {}}

    def to_dingtalk_name(directory: str) -> str:
        meta = groups_meta.get(directory) or {}
        return meta.get("dingtalk_name") or directory  # 找不到映射就退回目录名

    def load_csv(name):
        p = seed_dir / name
        if not p.exists():
            return []
        with open(p, encoding="utf-8") as f:
            return list(csv.DictReader(f))

    rows = load_csv("true_alias.csv") + load_csv("enum_norm.csv")

    # 先删掉 source='seed_v1' 的旧行（幂等重跑）
    cur.execute("DELETE FROM alias_scope WHERE source='seed_v1'")
    deleted = cur.rowcount

    # 按钉钉群名分组统计
    for r in rows:
        directory = r.get("group", "")
        alias = r.get("alias", "").strip()
        kind = r.get("kind", "").strip()
        target = r.get("target", "").strip()
        note = r.get("note", "").strip()
        if not (alias and kind and target):
            continue
        if not directory:
            stats["skipped_no_group"] += 1
            continue
        group_name = to_dingtalk_name(directory)
        cur.execute(
            "INSERT OR REPLACE INTO alias_scope (alias, kind, target, scope_group, evidence_count, source, note, created_at) "
            "VALUES (?, ?, ?, ?, 0, 'seed_v1', ?, ?)",
            (alias, kind, target, group_name, note[:400], now_cn_str())
        )
        stats["inserted"] += 1
        stats["by_group"][group_name] = stats["by_group"].get(group_name, 0) + 1

    # inherit：ONE 群"同华南-拉美" → 把华南-拉美的所有 true_alias 行复制一份给 ONE 群
    for r in load_csv("inherit.csv"):
        target_dir = r.get("target", "").strip()  # "华南-拉美"
        source_dir = r.get("group", "").strip()    # "ONE+ZIM+WHL+HPL+CSP交流群"
        # 从 true_alias 里找 target_dir 的所有条目，给 source_dir 复制
        src_name = to_dingtalk_name(target_dir)
        dst_name = to_dingtalk_name(source_dir)
        cur.execute(
            "SELECT alias, kind, target, note FROM alias_scope WHERE scope_group=? AND source='seed_v1'",
            (src_name,)
        )
        for src_row in cur.fetchall():
            note = f"inherited from {src_name}；{src_row[3] or ''}".strip().rstrip('；')
            cur.execute(
                "INSERT OR REPLACE INTO alias_scope (alias, kind, target, scope_group, evidence_count, source, note, created_at) "
                "VALUES (?, ?, ?, ?, 0, 'seed_v1', ?, ?)",
                (src_row[0], src_row[1], src_row[2], dst_name, note[:400], now_cn_str())
            )
            stats["inherit_expanded"] += 1
            stats["by_group"][dst_name] = stats["by_group"].get(dst_name, 0) + 1

    conn.commit()
    stats["deleted_prev"] = deleted
    return stats


def now_cn_str():
    from datetime import datetime, timezone, timedelta
    return datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M:%S")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", default=str(DEFAULT_OUT), help="输出目录（默认 Database/migrations/seed-v1-out）")
    ap.add_argument("--verbose", action="store_true")
    ap.add_argument("--write", action="store_true",
                    help="写完 dry-run 后再把 true_alias + enum_norm + inherit 展开灌入 alias_scope 表（source='seed_v1'，可幂等重跑）")
    args = ap.parse_args()
    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    out_dir = Path(args.out)
    log.info(f"读群清单 README: {README_PATH}")
    groups = read_groups_from_readme(README_PATH)
    idx = LookupIndex(DB_PATH)

    all_cands = []
    for directory in sorted(groups.keys()):
        spec_path = SPEC_ROOT / directory / "规范.md"
        if not spec_path.exists():
            log.warning(f"规范文件缺失: {spec_path}")
            all_cands.append({
                "group": directory,
                "alias": "",
                "kind": "unknown",
                "alias_kind": "unclassified",
                "target": "",
                "note": f"规范文件不存在: {spec_path.name}",
                "source": str(spec_path),
            })
            continue
        cands = process_one_spec(spec_path, directory, idx)
        all_cands.extend(cands)
        log.debug(f"{directory}: 提取 {len(cands)} 条")

    write_outputs(all_cands, out_dir, groups, idx)
    log.info(f"[OK] dry-run 输出到 {out_dir}")

    if args.write:
        import sqlite3 as _sq
        conn = _sq.connect(str(DB_PATH))
        try:
            stats = write_to_db(out_dir, groups, conn)
            log.info(
                f"[WRITE] 清旧 {stats['deleted_prev']} → 插入 {stats['inserted']} "
                f"（inherit 展开 {stats['inherit_expanded']}），覆盖 {len(stats['by_group'])} 群"
            )
            for g, n in sorted(stats["by_group"].items(), key=lambda x: -x[1]):
                log.info(f"    {g}: {n} 条")
            # 写后校验
            total = conn.execute("SELECT COUNT(*) FROM alias_scope").fetchone()[0]
            seed_total = conn.execute("SELECT COUNT(*) FROM alias_scope WHERE source='seed_v1'").fetchone()[0]
            kinds = dict(conn.execute("SELECT kind, COUNT(*) FROM alias_scope WHERE source='seed_v1' GROUP BY kind").fetchall())
            log.info(f"[VERIFY] 库内 alias_scope 总 {total}（seed_v1 {seed_total}）kind 分布 {kinds}")
        finally:
            conn.close()


if __name__ == "__main__":
    main()
