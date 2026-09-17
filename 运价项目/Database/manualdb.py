# -*- coding: utf-8 -*-
"""人工维护台账（局域网版）数据层：完全自包含的 SQLite 库 + 主数据归一 + 录入与留痕。

本文件不 import 任何其它看板的代码，库也是独立的 freight_manual.db。
业务口径：港口/船司/航线走主数据归一（规范名为英文官方港名），柜型三档写入即收敛，
价格类必须纯整数，人工录入的时间由服务端盖（客户端传什么都不认），
内容指纹幂等、改价改期走留痕（追加新行 + 旧行标已被覆盖 + 现算变动摘要），
目的港/起运港支持用户自维护的「港群」（库里存群名，查询时双向展开到成员港）。
"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import time
import unicodedata
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

HERE = Path(__file__).resolve().parent
DEFAULT_DB = HERE / "freight_manual.db"

# ---------------- 字段映射与表定义 ----------------

RATES_MAP: Dict[str, str] = {
    "起运港": "pol", "目的港": "pod", "航线": "route", "船司": "carrier",
    "柜型": "container_type", "海运费USD": "freight_usd", "有效期船期": "validity_raw",
    "目免": "free_days", "亏舱费": "dead_freight", "备注": "remark",
    "来源群": "source_group", "发送人": "sender", "消息时间": "message_time",
    "消息原文": "message_text", "运价图片": "image_url", "有效期起": "valid_from",
    "有效期止": "valid_to", "船期ETD": "etd", "被覆盖消息时间": "overridden_time",
    "记录状态": "status", "消息ID": "mid", "图片名": "image_file", "T/T航程": "transit_days",
    "AI判断": "ai_flag",
}
SPACE_MAP: Dict[str, str] = {
    "起运港": "pol", "目的港": "pod", "航线": "route", "船司": "carrier",
    "船名航次": "vessel_voyage", "船期截关": "cutoff_raw", "船期ETD": "etd",
    "箱型箱量": "box_desc", "柜型": "container_type", "箱量": "box_qty",
    "舱位类型": "space_type", "价格USD": "price_usd", "备注": "remark",
    "来源群": "source_group", "发送人": "sender", "消息时间": "message_time",
    "消息原文": "message_text", "舱位图": "image_url", "记录状态": "status", "消息ID": "mid",
    "图片名": "image_file", "T/T航程": "transit_days",
    "有效期船期": "validity_raw", "有效期起": "valid_from", "有效期止": "valid_to",
}
# 状态类与图片不参与内容指纹：状态会被就地改写，图片引用每次导入都可能变
KEY_EXCLUDE = {"image_url", "image_file", "status", "overridden_time", "synced_at", "ai_flag"}
UPDATE_BLOCKED = {"image_url", "image_file", "synced_at"}
INT_COLS: Dict[str, Dict[str, str]] = {
    "rates": {"freight_usd": "海运费USD", "dead_freight": "亏舱费",
              "free_days": "Freetime", "transit_days": "T/T航程"},
    "space": {"price_usd": "价格USD", "transit_days": "T/T航程", "box_qty": "箱量"},
}
DEFAULT_STATUS = {"rates": "当前生效", "space": "当前有效"}
# 「同一条报价/舱位」的身份键（判“是不是更新”用）：产品维度，【不含消息原文】。
# 一条转发原文常一次拆出多柜型/多ETD/多船，若拿“原文”当身份，同港不同柜型会互相误判成更新而彼此覆盖→
# 出现假“已被覆盖/无有效价”。故身份只认产品字段；完全重复仍由 content_key 指纹幂等。
IDENTITY_COLS: Dict[str, List[str]] = {
    "rates": ["carrier", "pol", "pod", "container_type", "etd"],
    "space": ["carrier", "pol", "pod", "vessel_voyage", "space_type", "etd"],
}
COVERED = "已被覆盖"
FILTERS = {"pol": "pol", "pod": "pod", "route": "route", "carrier": "carrier",
           "ctype": "container_type", "group": "source_group", "status": "status",
           "stype": "space_type", "origin": "origin"}
GROUP_FIELDS = ("carrier", "route", "pol", "pod", "etd", "message_time", "sender", "source_group",
                "validity_raw", "valid_from", "valid_to", "status", "remark", "message_text",
                "image_url", "image_file", "free_days", "dead_freight", "transit_days", "synced_at",
                "content_key", "supersedes", "ai_flag")

# 标准化闸门：核心字段只能落在主数据预设值上（起运港/目的港/船司/航线），杜绝脏值污染。
CORE_MATCH = ("起运港", "目的港", "船司", "航线")
# "不得为空"分库：运价要目的港/柜型/价格；舱位动态常无目的港，改要船名航次 + 舱位类型。
# 有效期两库都必填（见 VALIDITY_REQUIRED_KINDS）：报价没写时由 skill 按兜底规则补（ETD→[消息日,开船日]，都没有→[消息日,+2天]+AI判断）。
REQUIRED_BY_KIND: Dict[str, Tuple[str, ...]] = {
    "rates": ("起运港", "目的港", "船司", "柜型", "海运费USD"),
    "space": ("起运港", "船司", "船名航次", "舱位类型"),
}
VALIDITY_REQUIRED_KINDS = {"rates", "space"}   # 运价、舱位都强制有效期非空

TABLES: Dict[str, Dict[str, Any]] = {
    "rates": {"table": "freight_rates", "map": RATES_MAP,
              "key": sorted({c for c in RATES_MAP.values()} - KEY_EXCLUDE)},
    "space": {"table": "space_records", "map": SPACE_MAP,
              "key": sorted({c for c in SPACE_MAP.values()} - KEY_EXCLUDE)},
}

DDL: Dict[str, str] = {
    # 价格/天数为 INTEGER：既能数值排序（TEXT 会按字典序把 8444 排到 900 前面），
    # 又有 CHECK 挡住脏值；有效期起止是解析后的 ISO 日期，同样 CHECK 兜底；
    # origin 恒非空、默认 manual（本库就是人工维护台账，heartbeat 是云端旧版遗留值）。
    "freight_rates": """CREATE TABLE IF NOT EXISTS freight_rates (
        id INTEGER PRIMARY KEY AUTOINCREMENT, content_key TEXT NOT NULL UNIQUE,
        pol TEXT, pod TEXT, route TEXT, carrier TEXT, container_type TEXT, freight_usd INTEGER,
        validity_raw TEXT, valid_from TEXT, valid_to TEXT, etd TEXT, free_days INTEGER,
        dead_freight INTEGER, transit_days INTEGER, remark TEXT, source_group TEXT, sender TEXT,
        message_time TEXT, message_text TEXT, image_url TEXT, status TEXT, overridden_time TEXT,
        synced_at TEXT, mid TEXT, image_file TEXT, supersedes TEXT,
        origin TEXT NOT NULL DEFAULT 'manual',
        ai_flag TEXT,
        CHECK (freight_usd   IS NULL OR (typeof(freight_usd)   = 'integer' AND freight_usd   >= 0)),
        CHECK (free_days     IS NULL OR (typeof(free_days)     = 'integer' AND free_days     >= 0)),
        CHECK (dead_freight  IS NULL OR (typeof(dead_freight)  = 'integer' AND dead_freight  >= 0)),
        CHECK (transit_days  IS NULL OR (typeof(transit_days)  = 'integer' AND transit_days  >= 0)),
        CHECK (valid_from IS NULL OR valid_from = '' OR
               valid_from GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'),
        CHECK (valid_to   IS NULL OR valid_to   = '' OR
               valid_to   GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'))""",
    "space_records": """CREATE TABLE IF NOT EXISTS space_records (
        id INTEGER PRIMARY KEY AUTOINCREMENT, content_key TEXT NOT NULL UNIQUE,
        pol TEXT, pod TEXT, route TEXT, carrier TEXT, vessel_voyage TEXT, cutoff_raw TEXT,
        etd TEXT, box_desc TEXT, container_type TEXT, box_qty INTEGER, space_type TEXT,
        price_usd INTEGER, transit_days INTEGER, remark TEXT, source_group TEXT, sender TEXT,
        message_time TEXT, message_text TEXT, image_url TEXT, status TEXT, synced_at TEXT,
        mid TEXT, image_file TEXT, supersedes TEXT,
        validity_raw TEXT, valid_from TEXT, valid_to TEXT,
        origin TEXT NOT NULL DEFAULT 'manual',
        CHECK (price_usd    IS NULL OR (typeof(price_usd)    = 'integer' AND price_usd    >= 0)),
        CHECK (transit_days IS NULL OR (typeof(transit_days) = 'integer' AND transit_days >= 0)),
        CHECK (box_qty      IS NULL OR (typeof(box_qty)      = 'integer' AND box_qty      >= 0)),
        CHECK (valid_from IS NULL OR valid_from = '' OR
               valid_from GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'),
        CHECK (valid_to   IS NULL OR valid_to   = '' OR
               valid_to   GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'))""",
    "operators": """CREATE TABLE IF NOT EXISTS operators (
        id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL UNIQUE, title TEXT,
        routes TEXT, groups TEXT, match_mode TEXT DEFAULT 'any', active INTEGER DEFAULT 1,
        note TEXT, created_at TEXT)""",
    "chat_groups": """CREATE TABLE IF NOT EXISTS chat_groups (
        id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL UNIQUE, cid TEXT,
        default_pol TEXT, region TEXT, owner TEXT, active INTEGER DEFAULT 1, note TEXT,
        source TEXT DEFAULT 'manual',
        created_at TEXT)""",
    "ports": """CREATE TABLE IF NOT EXISTS ports (
        name TEXT PRIMARY KEY, country TEXT, city TEXT, city_pinyin TEXT, name_en TEXT,
        name_cn TEXT, aliases TEXT, kind TEXT DEFAULT 'pod', route TEXT, source TEXT, locode_ref TEXT,
        active INTEGER DEFAULT 1, note TEXT, created_at TEXT)""",
    "carriers": """CREATE TABLE IF NOT EXISTS carriers (
        code TEXT PRIMARY KEY, name_en TEXT, name_cn TEXT, scac TEXT, sort INTEGER DEFAULT 900,
        aliases TEXT, active INTEGER DEFAULT 1, note TEXT, created_at TEXT)""",
    "routes": """CREATE TABLE IF NOT EXISTS routes (
        name TEXT PRIMARY KEY, region TEXT, sort INTEGER DEFAULT 900, aliases TEXT,
        active INTEGER DEFAULT 1, note TEXT, created_at TEXT)""",
    "senders": """CREATE TABLE IF NOT EXISTS senders (
        name TEXT PRIMARY KEY, active INTEGER DEFAULT 1, note TEXT, source TEXT DEFAULT 'manual',
        created_at TEXT)""",
    "port_groups": """CREATE TABLE IF NOT EXISTS port_groups (
        name TEXT PRIMARY KEY, label TEXT, members TEXT, specials TEXT,
        kind TEXT DEFAULT 'pod', active INTEGER DEFAULT 1, note TEXT, created_at TEXT)""",
    # 作用域别名：同一个缩写在不同来源群/不同 kind 可能指不同实体
    # （实例：NB 在起运港列 98 次全是宁波，与"北巴"无关）。scope_group='*' 表示不限群。
    # 2026-09-15 扩容：kind 4→9 值（加 space_type/ctype/price/remark/schedule），加 evidence_count/source 列，
    # 主键保持 (alias, kind, scope_group)——kind 本身已表达"列"作用域，不再加 scope_column。
    "alias_scope": """CREATE TABLE IF NOT EXISTS alias_scope (
        alias TEXT NOT NULL, kind TEXT NOT NULL, target TEXT NOT NULL,
        scope_group TEXT NOT NULL DEFAULT '*',
        evidence_count INTEGER DEFAULT 0,
        source TEXT NOT NULL DEFAULT 'manual',
        note TEXT, created_at TEXT,
        PRIMARY KEY (alias, kind, scope_group),
        CHECK (kind IN ('pol','pod','carrier','route','space_type','ctype','price','remark','schedule')),
        CHECK (source IN ('manual','seed_v1','candidate_approved','imported')))""",
    "pending_records": """CREATE TABLE IF NOT EXISTS pending_records (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        kind TEXT NOT NULL,
        payload TEXT NOT NULL,
        origin TEXT NOT NULL DEFAULT 'agent',
        resolved TEXT,
        action TEXT,
        target_key TEXT,
        check_status TEXT NOT NULL DEFAULT 'pending',
        errors TEXT,
        review_status TEXT NOT NULL DEFAULT 'awaiting',
        final_key TEXT,
        reviewed_by TEXT, reviewed_at TEXT, note TEXT, created_at TEXT)""",
    # P2 三模式灰度：影子重放表。同一 pending 用不同 mode 重跑 expand/validate 落这里，
    # 与主 pending_records 的差异写在 diff_summary，人工看是否要采纳新规则。
    "pending_shadow": """CREATE TABLE IF NOT EXISTS pending_shadow (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        source_pending_id INTEGER NOT NULL,
        mode TEXT NOT NULL,
        kind TEXT NOT NULL,
        payload TEXT NOT NULL,
        resolved TEXT,
        errors TEXT,
        diff_summary TEXT,
        note TEXT,
        created_at TEXT,
        UNIQUE (source_pending_id, mode))""",
    # 规则候选：由 difflib 扫 failed pending、agent 观察、审核动作反向沉淀 三条数据流生成，
    # 人工在看板"候选管理"页签点批准后按 target_layer 写入 alias_scope 或主数据 aliases
    "rule_candidates": """CREATE TABLE IF NOT EXISTS rule_candidates (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        scope_kind TEXT NOT NULL,
        scope_key TEXT NOT NULL,
        target_layer TEXT NOT NULL,
        pattern_type TEXT NOT NULL,
        alias TEXT NOT NULL,
        kind TEXT NOT NULL,
        suggested_target TEXT NOT NULL,
        reason TEXT,
        confidence REAL DEFAULT 0,
        evidence_count INTEGER DEFAULT 0,
        evidence_refs TEXT,
        source TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'pending',
        reviewed_by TEXT, reviewed_at TEXT, reviewer_note TEXT,
        created_at TEXT DEFAULT CURRENT_TIMESTAMP,
        UNIQUE (scope_kind, scope_key, alias, kind, suggested_target),
        CHECK (scope_kind IN ('sender+carrier+group','sender+group','carrier+group','group+column','group')),
        CHECK (target_layer IN ('alias_scope','master_aliases','field_default')),
        CHECK (pattern_type IN ('alias','default','correction')),
        CHECK (source IN ('difflib_scan','agent_observation','audit_correction','manual')),
        CHECK (status IN ('pending','approved','rejected','expired')))""",
    # P1 字段默认值：审核动作反向沉淀"该 sender+carrier+group 维度下某字段反复被补同一个值"
    # （如 张三+CMA+北美群 的 ETD 反复填 21）→ 批准后落这里，审核界面提示用，不自动改数据
    "field_defaults": """CREATE TABLE IF NOT EXISTS field_defaults (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        scope_key TEXT NOT NULL,
        field TEXT NOT NULL,
        default_value TEXT NOT NULL,
        evidence_count INTEGER DEFAULT 0,
        source TEXT NOT NULL DEFAULT 'audit_scan',
        status TEXT NOT NULL DEFAULT 'active',
        note TEXT,
        created_at TEXT,
        UNIQUE (scope_key, field, default_value),
        CHECK (status IN ('active','retired')))""",
}
INDEX_PLAN = {
    "freight_rates": ["pol", "pod", "carrier", "route", "container_type", "source_group",
                      "message_time", "status", "origin", "supersedes"],
    "space_records": ["pol", "pod", "carrier", "route", "space_type", "source_group",
                      "message_time", "status", "origin", "supersedes"],
    "pending_records": ["review_status", "check_status", "kind", "created_at"],
}
MASTER_TABLES: Dict[str, Dict[str, str]] = {
    "ports": {"table": "ports", "pk": "name"},
    "carriers": {"table": "carriers", "pk": "code"},
    "routes": {"table": "routes", "pk": "name"},
    "senders": {"table": "senders", "pk": "name"},
    "port_groups": {"table": "port_groups", "pk": "name"},
}
MASTER_COLS: Dict[str, List[str]] = {
    "ports": ["name", "country", "city", "city_pinyin", "name_en", "name_cn", "aliases",
              "kind", "route", "source", "locode_ref", "active", "note"],
    "carriers": ["code", "name_en", "name_cn", "scac", "sort", "aliases", "active", "note"],
    "routes": ["name", "region", "sort", "aliases", "active", "note"],
    "senders": ["name", "active", "note"],
    "port_groups": ["name", "label", "members", "specials", "kind", "active", "note"],
}


def connect(db_path: str | Path = DEFAULT_DB) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    for ddl in DDL.values():
        conn.execute(ddl)
    _migrate(conn)
    for table, cols in INDEX_PLAN.items():
        for c in cols:
            conn.execute(f'CREATE INDEX IF NOT EXISTS idx_{table}_{c} ON {table}({c})')
    conn.commit()
    return conn


def _migrate(conn: sqlite3.Connection) -> None:
    """幂等列迁移：给存量库补新列（CREATE TABLE IF NOT EXISTS 不会给已建表加列）。"""
    have = {r[1] for r in conn.execute("PRAGMA table_info(freight_rates)")}
    if "ai_flag" not in have:
        conn.execute("ALTER TABLE freight_rates ADD COLUMN ai_flag TEXT")
    pcols = {r[1] for r in conn.execute("PRAGMA table_info(ports)")}
    if "route" not in pcols:          # 航线是港口的子属性（可逗号多值），存量库补列
        conn.execute("ALTER TABLE ports ADD COLUMN route TEXT")
    have_s = {r[1] for r in conn.execute("PRAGMA table_info(space_records)")}
    for col in ("validity_raw", "valid_from", "valid_to"):
        if col not in have_s:
            conn.execute(f"ALTER TABLE space_records ADD COLUMN {col} TEXT")
    # 2026-09-17 P0 审计地基：pending_records 加原始快照 + 修改日志（审核动作反向沉淀的数据源）
    have_p = {r[1] for r in conn.execute("PRAGMA table_info(pending_records)")}
    if "orig_payload" not in have_p:
        conn.execute("ALTER TABLE pending_records ADD COLUMN orig_payload TEXT")
    if "edit_log" not in have_p:
        conn.execute("ALTER TABLE pending_records ADD COLUMN edit_log TEXT")
    # 箱量治理：空表且 box_qty 仍是 TEXT 时，按新 DDL（INTEGER + 非负 CHECK）重建；有数据则留原样（应用层已挡住负数）
    bt = conn.execute("SELECT type FROM pragma_table_info('space_records') WHERE name='box_qty'").fetchone()
    if bt and (bt[0] or "").upper() != "INTEGER":
        n = conn.execute("SELECT COUNT(*) FROM space_records").fetchone()[0]
        if n == 0:
            conn.execute("DROP TABLE space_records")
            conn.execute(DDL["space_records"])
    # 字典库来源标记列（system-auto vs manual）：存量库补列，CREATE IF NOT EXISTS 不给已建表加列
    for tbl in ("chat_groups", "senders"):
        cols = {r[1] for r in conn.execute(f"PRAGMA table_info({tbl})")}
        if "source" not in cols:
            conn.execute(f"ALTER TABLE {tbl} ADD COLUMN source TEXT DEFAULT 'manual'")
    # 2026-09-15 alias_scope 扩容：kind 4→9 值 + evidence_count + source；SQLite CHECK 不能 ALTER，
    # 老表若存在且缺 evidence_count → DROP + CREATE。生产当前 0 条数据（写入口缺失），安全。
    # 若老表已有数据，宁可抛错让人工处理也不静默丢。
    acols = {r[1] for r in conn.execute("PRAGMA table_info(alias_scope)")}
    if acols and "evidence_count" not in acols:
        n = conn.execute("SELECT COUNT(*) FROM alias_scope").fetchone()[0]
        if n > 0:
            raise RuntimeError(
                f"alias_scope 老 DDL 但已存 {n} 条数据；请先 GET /api/export 保存快照，"
                f"再手工迁移到新表结构（新 DDL 见 manualdb.py:153）。"
            )
        conn.execute("DROP TABLE alias_scope")
        conn.execute(DDL["alias_scope"])
    # 2026-09-17 rule_candidates target_layer CHECK 扩 'field_default'：老表 0 行则重建
    rc = conn.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='rule_candidates'").fetchone()
    if rc and "field_default" not in (rc[0] or ""):
        n = conn.execute("SELECT COUNT(*) FROM rule_candidates").fetchone()[0]
        if n > 0:
            raise RuntimeError(f"rule_candidates 老 CHECK 但已存 {n} 条；请人工迁移后再启动")
        conn.execute("DROP TABLE rule_candidates")
        conn.execute(DDL["rule_candidates"])


def now_cn() -> str:
    return datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M:%S")


def today_cn() -> str:
    return now_cn()[:10]


# ---------------- 归一化基础件 ----------------

COUNTRIES = {"venezuela", "brazil", "argentina", "chile", "peru", "colombia", "uruguay",
             "paraguay", "bolivia", "ecuador", "guatemala", "honduras", "el salvador",
             "costa rica", "panama", "panamá", "cuba", "haiti", "dominican republic",
             "republica dominicana", "mexico"}
PORT_SEP = re.compile(r"[/、]")

# ---- 多值展开 / 脏值硬闸 / 主数据兜底（三条入口共用，见 expand_cn 与 resolve_core）----
CARRIER_SEP = re.compile(r"[/、,，;；]+")          # 联盟/共舱写法 ONE/HPL/MSC/HMM；逗号在船司列不作港国分隔用
PRICE_MULTI = re.compile(r"[/、,，;；\s]+")
CTYPE_ENUM = ("20GP", "40HQ/HC", "40NOR")         # 柜型只三档
# 舱位类型十一档（2026-09-11 由八档扩）：空班=整班取消、跳港=船开但不挂这港、首航=新航线首班。
# 这三类过去被就近塞进「撤载改期」（本意是甩柜/退舱改期），语义不同且销售端查不到，故单独立档
SPACE_TYPE_ENUM = ("现舱", "加班船", "放舱开放", "售罄", "舱位紧张", "截关截单",
                   "撤载改期", "箱子动态", "空班", "跳港", "首航")
# "40HQ/HC" 本身含斜杠，柜型多值不能裸拆 → 先贪婪匹配已知写法
_CTYPE_FORM = re.compile(r"40HQ/HC|40NOR/HC|40N/HC|40HQ-HC|20GP|40HQ|40HC|40NOR|40N|20|40", re.I)
CTYPE_FORMS = ("20GP", "20", "40HQ/HC", "40HQ", "40HC", "40HQ-HC",
               "40NOR", "40N", "40NOR/HC", "40N/HC")      # 认得的写法白名单（45GP/40OT 不在内）
MAX_EXPAND = 60                                    # 单条消息展开上限：多维笛卡尔积炸行一律交人工
# 目的港里出现这些就是"说明文字/服务码/占位符"，不许部分命中就放过（只去X/Y、Base port via Z、(未注明…)）
POD_JUNK = re.compile(r"[()（）]|未注明|只去|via\b|base ?port|port group|待定|未拆|多港", re.I)
# 判脏之前先剥掉结尾的两位国家码消歧后缀（Cartagena(CO)/Manzanillo(MX)），否则会被误报成"含说明文字"
COUNTRY_SUFFIX = re.compile(r"\([A-Za-z]{2}\)\s*$")
PLACEHOLDER = re.compile(r"^\s*[（(].*[)）]\s*$|未注明|待定|^\s*无\s*$", re.I)
CARRIER_SHAPE = re.compile(r"^[A-Z][A-Z0-9&\-]{1,5}$")   # 只有像船司缩写的未知 token 才允许自动补主数据


def fold(s: Any) -> str:
    """匹配用归一：去音标、转小写、压掉 . , ' - ( ) —— Antwerpen / Göteborg / Pecém 都对得上。"""
    s = unicodedata.normalize("NFKD", str(s or "").strip())
    s = "".join(c for c in s if not unicodedata.combining(c))
    return re.sub(r"\s+", " ", re.sub(r"[.,'’\-()]", " ", s.lower())).strip()


def split_list(v: Any) -> List[str]:
    out: List[str] = []
    for p in re.split(r"[,，、;；]", str(v or "")):
        p = p.strip()
        if p and p not in out:
            out.append(p)
    return out


def coerce(val: Any) -> Optional[str]:
    """单元格值 → 可存文本；复合结构取可读标识，不写 Python 字面量。"""
    if val is None:
        return None
    if isinstance(val, str):
        return val.strip() or None
    if isinstance(val, (list, tuple)):
        parts = [p for p in (coerce(v) for v in val) if p]
        return " / ".join(parts) if parts else None
    if isinstance(val, dict):
        for key in ("name", "fileName", "fileToken", "url", "text"):
            if val.get(key):
                return str(val[key]).strip() or None
        return None
    return str(val).strip() or None


def norm_ctype(ct: Any) -> str:
    c = str(ct or "").upper()
    if c in ("20GP", "20"):
        return "20GP"
    if c in ("40NOR", "40N", "40NOR/HC"):
        return "40NOR"
    if c.startswith("40"):
        return "40HQ/HC"
    return str(ct or "")


def ctype_list(raw: Any) -> List[str]:
    """"20GP/40HQ/HC/40NOR" → ["20GP","40HQ/HC","40NOR"]；识别不了就返回 [] 表示不拆。
    规范档名 40HQ/HC 自带斜杠，所以只能贪婪匹配已知写法，不能按分隔符裸切。"""
    s = str(raw or "").strip()
    if not s:
        return []
    toks = _CTYPE_FORM.findall(s)
    if not toks:
        return []
    if _CTYPE_FORM.sub("", s).strip(" /、,，;；\t"):
        return []                                   # 还有认不出的内容 → 不猜，交给闸门判脏
    out = [norm_ctype(t) for t in toks]
    return out if all(o in CTYPE_ENUM for o in out) else []


def ctype_ok(raw: Any) -> bool:
    """柜型写法白名单：norm_ctype 会把任何 40 开头的都归成 40HQ/HC，这里挡住 45GP/40OT 这类脏值。"""
    return fold(raw) in {fold(f) for f in CTYPE_FORMS}


def build_index(pairs: List[Tuple[Any, str]]) -> Dict[str, str]:
    """别名 → 规范值。一个别名指向多个对象就整条丢弃：宁可不归一，也别归错港。"""
    seen: Dict[str, Optional[str]] = {}
    for alias, canon in pairs:
        k = fold(alias)
        if not k:
            continue
        if k in seen and seen[k] != canon:
            seen[k] = None
        else:
            seen.setdefault(k, canon)
    return {k: v for k, v in seen.items() if v}


def _split_port(v: Any) -> List[str]:
    if not v:
        return [""]
    parts = [p.strip() for p in PORT_SEP.split(str(v)) if p.strip()]
    return parts or [""]


class Master:
    """主数据快照 + 索引（港口 / 船司 / 航线 / 发送人 / 港群）。"""

    def __init__(self, conn: sqlite3.Connection):
        self.unmatched: List[Dict[str, str]] = []
        q = lambda sql: [dict(r) for r in conn.execute(sql)]
        self.ports = q("SELECT * FROM ports WHERE active=1 ORDER BY name")
        self.carriers = q("SELECT * FROM carriers WHERE active=1 ORDER BY COALESCE(sort,900), code")
        self.routes = q("SELECT * FROM routes WHERE active=1 ORDER BY COALESCE(sort,900), name")
        self.senders = [r["name"] for r in conn.execute(
            "SELECT name FROM senders WHERE active=1 ORDER BY name")]
        self.group_rows = q("SELECT * FROM port_groups WHERE active=1 ORDER BY name")
        # 作用域别名：(fold(别名), kind, 来源群) → 规范名；'*' 为不限群的全局覆盖
        self.scope_idx: Dict[tuple, str] = {}
        for r in q("SELECT * FROM alias_scope"):
            self.scope_idx[(fold(r["alias"]), r["kind"], r["scope_group"] or "*")] = r["target"]

        def pairs(rows, pk, cols):
            out: List[Tuple[str, str]] = []
            for r in rows:
                canon = r.get(pk)
                if not canon:
                    continue
                for c in cols:
                    v = r.get(c)
                    if not v:
                        continue
                    out += ([(v, canon)] if c != "aliases"
                            else [(x, canon) for x in split_list(v)])
            return out

        # 规范名主键唯一，先按它精确匹配；跨国同名只在别名索引里有歧义，不该因此匹配不上
        self.name_idx = {fold(p["name"]): p["name"] for p in self.ports}
        # 键源不含 country：两位国家码每个港都有，必然指向几十个港被歧义规则丢弃（2026-09-12 体检实测），
        # 国家只用于筛选与展示，不参与归一
        self.port_idx = build_index(pairs(self.ports, "name", [
            "name", "name_cn", "name_en", "city", "aliases"]))
        self.carrier_idx = build_index(pairs(self.carriers, "code",
                                            ["code", "name_en", "name_cn", "aliases"]))
        self.route_idx = build_index(pairs(self.routes, "name", ["name", "aliases"]))
        # 港口→航线子线（航线是港口的子属性；族从 routes.region 现取，不在这里存）
        self.port_route = {p["name"]: str(p.get("route") or "").strip()
                           for p in self.ports if str(p.get("route") or "").strip()}

        self.group_by_name: Dict[str, Dict[str, Any]] = {}
        self.group_alias: Dict[str, str] = {}
        self.member_to_groups: Dict[str, List[str]] = {}
        for g in self.group_rows:
            members, specials = split_list(g.get("members")), split_list(g.get("specials"))
            info = {"name": g["name"], "label": g.get("label") or "", "members": members,
                    "specials": specials, "kind": g.get("kind") or "pod",
                    "note": g.get("note") or ""}
            self.group_by_name[g["name"]] = info
            # 「北巴（巴西东北）」这种标签，输入「北巴」也要能命中：全角括号不在 fold 的清理范围里
            short = re.split(r"[（(]", info["label"])[0].strip()
            for k in filter(None, (g["name"], info["label"], short)):
                fk = fold(k)
                if fk not in self.group_alias:
                    self.group_alias[fk] = g["name"]
            for m in members:
                self.member_to_groups.setdefault(m, []).append(g["name"])
        self.group_pol = {r["name"]: r["default_pol"] for r in conn.execute(
            "SELECT name, default_pol FROM chat_groups WHERE active=1 AND default_pol IS NOT NULL")}

    # ---- 单值归一 ----
    def scoped(self, kind: str, alias: Any, group: str = "") -> str:
        """作用域别名：先查本群专属，再查 scope_group='*' 的全局覆盖；都没有返回空。"""
        fk = fold(alias)
        if not fk:
            return ""
        return self.scope_idx.get((fk, kind, group or "*")) or self.scope_idx.get((fk, kind, "*")) or ""

    def lookup_port(self, v: Any, kind: str = "pod", group: str = "") -> Tuple[str, bool]:
        s = str(v or "").strip()
        if not s:
            return "", False
        hit = self.scoped(kind, s, group)                 # 本群专属写法优先于全局别名
        if hit:
            return hit, True
        for cand in (s, s.split(",")[0]):
            got = self.name_idx.get(fold(cand)) or self.port_idx.get(fold(cand))
            if got:
                return got, True
        return s, False

    def lookup(self, v: Any, idx: Dict[str, str]) -> str:
        s = str(v or "").strip()
        if not s:
            return ""
        return idx.get(fold(s)) or idx.get(fold(s.split(",")[0])) or s

    def group_for(self, v: Any, side: str) -> str:
        """填的是港群名（或中文标签）就返回群名；用作起运港要看 kind 允许不允许。"""
        s = str(v or "").strip()
        name = self.group_alias.get(fold(s)) if s else None
        if not name:
            return ""
        kind = self.group_by_name[name]["kind"]
        if side == "pol" and kind not in ("pol", "both"):
            return ""
        return name

    def derive_route(self, pod: str) -> str:
        """按目的港取航线子线；传港群名则看成员航线是否共同一致，不一致返回空（不猜）。"""
        pod = str(pod or "").strip()
        if not pod:
            return ""
        r = self.port_route.get(pod)
        if r:
            return r.split(",")[0].strip()
        g = self.group_by_name.get(pod)
        if g:
            lanes = {self.port_route.get(m, "").split(",")[0].strip() for m in g["members"]}
            lanes.discard("")
            return lanes.pop() if len(lanes) == 1 else ""
        return ""

    def split_carriers(self, v: Any, conn: Optional[sqlite3.Connection] = None,
                       allow_add: bool = False, src: Optional[Dict[str, Any]] = None,
                       group: str = "") -> List[str]:
        """船司列多值（联盟/共舱 ONE/HPL/MSC/HMM）拆 token 并逐个归一到 carriers 主键。
        未命中且形态像船司缩写 → 写入侧自动补主数据（note 带溯源，人工可否决）；否则原样留给闸门打回。"""
        toks = [t.strip() for t in CARRIER_SEP.split(str(v or "")) if t.strip()]
        if not toks:
            return [""]
        out: List[str] = []
        for t in toks:
            hit = self.scoped("carrier", t, group) or self.carrier_idx.get(fold(t)) \
                or self.carrier_idx.get(fold(t.split(",")[0]))
            if not hit and allow_add and conn is not None and CARRIER_SHAPE.match(t.upper()):
                if ensure_carrier(conn, t, src):
                    self.rebuild_carrier_idx(conn)
                    hit = self.carrier_idx.get(fold(t))
            out.append(hit or t)
        seen, uniq = set(), []
        for c in out:
            if c and c not in seen:
                seen.add(c)
                uniq.append(c)
        return uniq or [""]

    def rebuild_carrier_idx(self, conn: sqlite3.Connection) -> None:
        """自动补了船司主数据后就地刷新快照与索引（缓存的 Master 不必等 TTL）。"""
        self.carriers = [dict(r) for r in conn.execute(
            "SELECT * FROM carriers WHERE active=1 ORDER BY COALESCE(sort,900), code")]
        self.carrier_idx = build_index(
            [(k, r["code"]) for r in self.carriers
             for k in [r.get("code"), r.get("name_en"), r.get("name_cn")] + split_list(r.get("aliases")) if k])

    def expand_cn(self, rec: Dict[str, Any], conn: Optional[sqlite3.Connection] = None,
                  allow_carrier_add: bool = False, src: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
        """中文字段记录 → 拆分 + 归一 + 批内去重。群价按群名存一条，不拆成员。
        conn/allow_carrier_add 只在写入侧传（agent 暂存）：允许把"未命中但形态像船司"的新船司补进主数据；
        只读预检 /api/validate 一律不传，绝不因预检写主数据。"""
        grp = str(rec.get("来源群") or "").strip()
        dp = self.group_pol.get(grp, "")
        pod_group = self.group_for(rec.get("目的港"), "pod")
        pods: List[str] = []
        if pod_group:
            # group_for 命中的是港群名，一般单值；仍走 split_list 兼容 alias_scope 未来多值串
            pods = split_list(pod_group) or [pod_group]
        else:
            for p in _split_port(rec.get("目的港")):
                if fold(p.split(",")[0]) in COUNTRIES:
                    continue                       # 纯国家名丢弃
                np, ok = self.lookup_port(p, "pod", grp)
                if not np:
                    continue
                if p.strip() and not ok:
                    self.unmatched.append({"field": "目的港", "input": p})
                # 只在 alias_scope 命中时拆多值串（"WCSA、MEX"→两条 rates）；
                # 未命中保留原值交给 POD_JUNK 判脏，不误拆含中文逗号的说明性输入
                if ok:
                    parts = split_list(np)
                    pods.extend(parts if parts else [np])
                else:
                    pods.append(np)
        if not pods:
            pods = [""]
        pol_group = self.group_for(rec.get("起运港"), "pol")
        pols: List[str] = []
        if pol_group:
            pols = split_list(pol_group) or [pol_group]
        else:
            for x in _split_port(rec.get("起运港")):
                got, ok = self.lookup_port(x, "pol", grp)
                if x.strip() and not ok and got != dp:
                    self.unmatched.append({"field": "起运港", "input": x})
                final = got or dp or x
                if ok:
                    parts = split_list(final)
                    pols.extend(parts if parts else [final])
                else:
                    pols.append(final)
        ctype_raw = str(rec.get("柜型") or "").strip()
        carriers = self.split_carriers(rec.get("船司"), conn, allow_carrier_add, src, grp)
        route = self.lookup(rec.get("航线"), self.route_idx)
        # 柜型多档：先整体归一（"40HQ/HC" 自带斜杠，不是多值）；确为多档则价格必须逐一对应，
        # 配不上就把价格留空交给闸门按「海运费USD不得为空」逐条打回——绝不把一个价复制给三种柜型
        price_cn = "海运费USD" if "海运费USD" in rec else "价格USD"
        price_raw = str(rec.get(price_cn) or "").strip()
        ctypes = ctype_list(ctype_raw)
        prices = [p.strip() for p in PRICE_MULTI.split(price_raw) if p.strip()]
        if len(ctypes) > 1 and len(set(ctypes)) == len(ctypes):
            cts = list(zip(ctypes, prices)) if len(prices) == len(ctypes) else [(n, "") for n in ctypes]
        else:
            # 白名单内才归一：norm_ctype 的 startswith("40") 会把 40GP 悄悄变成 40HQ/HC（普柜≠高柜），
            # 悄悄改值比打回更糟——原样留着让柜型硬闸报出来
            cts = [(norm_ctype(ctype_raw) if ctype_ok(ctype_raw) else ctype_raw, price_raw)]
        out, seen = [], set()
        for po in pods:
            for ol in pols:
                for cr in carriers:
                    for ct, pr in cts:
                        e = dict(rec)
                        e.update({"目的港": po, "起运港": ol, "柜型": ct,
                                  "船司": cr or (rec.get("船司") or ""),
                                  # 航线只补空、绝不覆盖已明示值：实测按 ports.source 派生覆盖会把
                                  # 617 条正确航线改错（Ambarli 地东→地中海、Gdansk 波罗的海→北欧等）
                                  "航线": route or (rec.get("航线") or "") or self.derive_route(po)})
                        e[price_cn] = pr
                        key = (e.get("消息ID") or "", cr, ct, po, pr)
                        if key in seen:
                            continue
                        seen.add(key)
                        out.append(e)
        return out

    def port_filter(self, col: str, val: str) -> Tuple[str, List[Any]]:
        """筛选时群名与成员港双向展开：查成员港带出群价，查群名带出成员精确价。"""
        list_ = [val]
        g = self.group_by_name.get(val)
        if g:
            list_ += g["members"]
        else:
            list_ += self.member_to_groups.get(val, [])
        uniq = [v for v in dict.fromkeys(list_) if v]
        return f"{col} IN ({','.join('?' * len(uniq))})", uniq


_CACHE: Dict[str, Any] = {"master": None, "at": 0.0}


def get_master(conn: sqlite3.Connection, ttl: float = 300.0) -> Master:
    if _CACHE["master"] is None or (time.time() - _CACHE["at"]) > ttl:
        m = Master(conn)
        if m.ports:
            _CACHE["master"], _CACHE["at"] = m, time.time()
        else:
            _CACHE["master"], _CACHE["at"] = m, 0.0   # 读空不缓存，下一请求重新拉
    return _CACHE["master"]


def invalidate_master() -> None:
    _CACHE["master"], _CACHE["at"] = None, 0.0


def ensure_carrier(conn: sqlite3.Connection, code: str,
                   src: Optional[Dict[str, Any]] = None) -> str:
    """未知但形态像船司的 token → 补一条 carriers 主键（只补不覆盖）。
    note 里留溯源与「待人工复核」标记：发现是别家笔误时人工合并/停用即可，闸门不猜。"""
    code = str(code or "").strip().upper()
    if not code or not CARRIER_SHAPE.match(code):
        return ""
    if conn.execute("SELECT 1 FROM carriers WHERE UPPER(code)=?", (code,)).fetchone():
        return code
    trace = "auto：待人工复核"
    if src:
        frag = re.sub(r"\s+", " ", str(src.get("消息原文") or ""))[:60]
        trace += "｜来源群=%s｜发送人=%s｜消息ID=%s｜原文=%s" % (
            src.get("来源群") or "", src.get("发送人") or "", src.get("消息ID") or "", frag)
    conn.execute("INSERT INTO carriers(code,name_en,name_cn,scac,aliases,active,note,created_at)"
                 " VALUES(?,?,?,?,'',1,?,?)",
                 (code, "", "", "", trace, now_cn()))
    conn.commit()
    invalidate_master()
    return code


# ---------------- 内容指纹与写入 ----------------

_NUM_RE = re.compile(r"^\d+(?:\.\d+)?$")
_DT_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})[T ](\d{2}):(\d{2})(?::\d{2})?(?:\+\d{2}:\d{2})?$")


def canon_for_key(val: str) -> str:
    """8444 / 8444.0 / 8444.00 同值；2026-08-31 与 2026-08-31 00:00 同值，真实时分保留。"""
    if _NUM_RE.match(val):
        try:
            return "%g" % float(val)
        except ValueError:
            return val
    m = _DT_RE.match(val)
    if m:
        day, hh, mm = m.groups()
        return day if (hh, mm) == ("00", "00") else f"{day} {hh}:{mm}"
    return val


def normalize(cn_record: Dict[str, Any], kind: str, now: str, origin: str) -> Dict[str, Optional[str]]:
    spec = TABLES[kind]
    row: Dict[str, Optional[str]] = {c: None for c in spec["map"].values()}
    for cn, col in spec["map"].items():
        v = coerce(cn_record.get(cn))
        if v is not None:
            row[col] = v
    if not row.get("status"):
        row["status"] = DEFAULT_STATUS[kind]
    if origin == "manual":
        row["message_time"] = now          # 录入时间：客户端传什么都不认
    for col, label in INT_COLS.get(kind, {}).items():
        v = row.get(col)
        if v not in (None, "") and not re.fullmatch(r"\d+", str(v).strip()):
            raise ValueError(f"{label} 必须是非负整数（收到 \"{v}\"）")
    basis = "\x1f".join(canon_for_key(str(row.get(f) or "")) for f in spec["key"])
    row["content_key"] = hashlib.sha256(basis.encode("utf-8")).hexdigest()
    row["synced_at"] = now
    row["origin"] = origin
    return row


def upsert_rows(conn: sqlite3.Connection, kind: str, rows: List[Dict[str, Optional[str]]],
                commit: bool = True) -> int:
    if not rows:
        return 0
    table = TABLES[kind]["table"]
    cols = sorted({c for r in rows for c in r})
    ph = ",".join("?" * len(cols))
    upd = ",".join(f"{c}=excluded.{c}" for c in cols if c != "content_key")
    sql = (f'INSERT INTO {table} ({",".join(cols)}) VALUES ({ph}) '
           f'ON CONFLICT(content_key) DO UPDATE SET {upd}')
    for r in rows:
        conn.execute(sql, [r.get(c) for c in cols])
    if commit:
        conn.commit()
    return len(rows)


# ---------------- 标准化校验 / 新增更新判定 / 待审核暂存 ----------------

_ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def resolve_core(master: "Master", kind: str, rec: Dict[str, Any]) -> Tuple[Dict[str, str], List[Dict[str, str]]]:
    """把核心字段解析成主数据规范值（群名/成员港/别名/中英/拼音/LOCODE 全走 Master，唯一真源）。
    多港写法（A/B）逐个解析。命不中即记 error，并保留原值供展示。"""
    errors: List[Dict[str, str]] = []
    resolved: Dict[str, str] = {}
    grp = str(rec.get("来源群") or "").strip()      # 作用域：同一缩写在不同群/列可指不同实体

    def port_side(cn: str, side: str) -> str:
        raw = str(rec.get(cn) or "").strip()
        if not raw:
            return ""
        outs = []
        for tok in _split_port(raw):
            g = master.group_for(tok, side)                 # 港群名（含中文标签）
            if g:
                outs.append(g); continue
            name, ok = master.lookup_port(tok, side, grp)   # 专属别名 → 港口别名/中英/拼音/LOCODE
            if ok:
                outs.append(name); continue                 # 命中即放过：Caldera(CR)/Cartagena(CO) 是合法规范名
            if POD_JUNK.search(COUNTRY_SUFFIX.sub("", tok)):   # 剥掉 (CO)/(MX) 这类消歧后缀再判脏
                errors.append({"field": cn, "input": tok,
                               "msg": f"{cn}「{tok}」含说明文字或服务码，只可填港名/港群名，说明写进备注"})
            else:
                errors.append({"field": cn, "input": tok, "msg": f"{cn}「{tok}」不在港口/港群主数据里"})
            outs.append(tok)
        return "/".join(outs)

    def master_side(cn: str, idx: Dict[str, str], multi: bool = False, scope_kind: str = "") -> str:
        raw = str(rec.get(cn) or "").strip()
        if not raw:
            return ""
        if multi:            # 船司列：ONE/HPL/MSC/HMM 这类共舱串逐个匹配，全命中才放过（预检不写主数据）
            hits = []
            for t in [x.strip() for x in CARRIER_SEP.split(raw) if x.strip()]:
                h = (master.scoped(scope_kind, t, grp) if scope_kind else "") \
                    or idx.get(fold(t)) or idx.get(fold(t.split(",")[0]))
                if not h:
                    errors.append({"field": cn, "input": t, "msg": f"{cn}「{t}」不在主数据里"})
                hits.append(h or t)
            return "/".join(dict.fromkeys(hits))
        hit = (master.scoped(scope_kind, raw, grp) if scope_kind else "") \
            or idx.get(fold(raw)) or idx.get(fold(raw.split(",")[0]))
        if hit:
            return hit
        errors.append({"field": cn, "input": raw, "msg": f"{cn}「{raw}」不在主数据里"})
        return raw

    resolved["起运港"] = port_side("起运港", "pol")
    resolved["目的港"] = port_side("目的港", "pod")
    resolved["船司"] = master_side("船司", master.carrier_idx, multi=True, scope_kind="carrier")
    resolved["航线"] = master_side("航线", master.route_idx, scope_kind="route")
    if not resolved["航线"]:      # 航线是港口的子属性：为空时按目的港补（只补空，不覆盖明示值）
        resolved["航线"] = master.derive_route(resolved["目的港"])

    if kind == "space":      # 占位符船名按未填处理：帖子里没写船名就别造一个填进去
        vv = str(rec.get("船名航次") or "").strip()
        if vv and PLACEHOLDER.search(vv):
            errors.append({"field": "船名航次", "input": vv,
                           "msg": "船名航次是占位符，按未填处理（原图/原文没写就留空交人工）"})

    for cn in REQUIRED_BY_KIND.get(kind, ()):
        if not str(rec.get(cn) or "").strip():
            errors.append({"field": cn, "input": "", "msg": f"{cn}不得为空"})
    if kind in VALIDITY_REQUIRED_KINDS:
        vf = str(rec.get("有效期起") or "").strip()
        vt = str(rec.get("有效期止") or "").strip()
        if not (vf or vt):
            errors.append({"field": "有效期", "input": "", "msg": "有效期不得为空"})
        for cn, v in (("有效期起", vf), ("有效期止", vt)):
            if v and not _ISO_DATE.match(v):
                errors.append({"field": cn, "input": v, "msg": f"{cn}必须是 ISO 日期 YYYY-MM-DD"})
    return resolved, errors


def validate_record(conn: sqlite3.Connection, master: "Master", kind: str,
                    rec: Dict[str, Any]) -> Dict[str, Any]:
    """机械校验闸门（预检 / 全库体检 / 待审核 / 录入 共用）。返回 {ok, resolved, errors}。"""
    resolved, errors = resolve_core(master, kind, rec)
    inv = {v: k for k, v in TABLES[kind]["map"].items()}
    cv = str(rec.get("柜型") or "").strip()
    if cv and not ctype_ok(cv):
        errors.append({"field": "柜型", "input": cv, "msg": "柜型只三档：20GP / 40HQ/HC / 40NOR"})
    st = str(rec.get("舱位类型") or "").strip()
    if kind == "space" and st and st not in SPACE_TYPE_ENUM:
        errors.append({"field": "舱位类型", "input": st,
                       "msg": "舱位类型只十一档：" + " / ".join(SPACE_TYPE_ENUM)})
    for col, label in INT_COLS.get(kind, {}).items():
        cn = inv.get(col)
        v = str(rec.get(cn) or "").strip() if cn else ""
        if v and not re.fullmatch(r"\d+", v):
            errors.append({"field": cn or col, "input": v, "msg": f"{label} 必须是非负整数"})
    return {"ok": not errors, "resolved": resolved, "errors": errors}


def _find_current_by_identity(conn: sqlite3.Connection, kind: str,
                              row: Dict[str, Any]) -> Optional[sqlite3.Row]:
    """按产品身份键（IDENTITY_COLS：承运人+起运+目的+柜型+ETD[+船名/舱位类型]，不含原文）找当前生效行。"""
    table = TABLES[kind]["table"]
    cols = IDENTITY_COLS[kind]
    where = " AND ".join(f"COALESCE({c},'')=?" for c in cols)
    args = [("" if row.get(c) is None else str(row.get(c))) for c in cols]
    return conn.execute(
        f"SELECT content_key FROM {table} WHERE {where} AND status=? "
        f"ORDER BY message_time DESC, id DESC LIMIT 1", args + [DEFAULT_STATUS[kind]]).fetchone()


_QHDR = re.compile(r"\n+——引用\s*(?P<hdr>[^—]*?)——\s*\n(?P<body>.+)\Z", re.S)


def split_quote(text: Any) -> Tuple[str, Optional[str], Optional[str]]:
    """把带引用的消息原文拆成 (本条正文, 被引用正文, 被引用 mID|None)；无引用则后两项 None。
    方向铁律：只有『引用方』能指向它引用的『被引用方』并据此判更新；反向一律不成立，绝不反推。"""
    text = str(text or "")
    m = _QHDR.search(text)
    if not m:
        return text, None, None
    mid = re.search(r"mID=(\S+)", m.group("hdr"))
    return text[:m.start()], m.group("body").strip(), (mid.group(1) if mid else None)


def _walk_to_current(conn: sqlite3.Connection, kind: str, content_key: str, depth: int = 20) -> str:
    """从任意一版顺 supersedes 反查链向后走到『当前生效』版；断链/成环退回手上这版。
    方向铁律：只由旧→新走，不把死版本当更新目标，也绝不反向覆盖新行。"""
    table = TABLES[kind]["table"]
    key, seen = content_key, {content_key}
    for _ in range(depth):
        row = conn.execute(f"SELECT status FROM {table} WHERE content_key=?", (key,)).fetchone()
        if not row:
            return key                                   # 目标已不在库：退回原 key（上层按新增处理）
        if row["status"] == DEFAULT_STATUS[kind]:
            return key                                   # 命中当前生效版，停
        nxt = conn.execute(
            f"SELECT content_key FROM {table} WHERE supersedes=? "
            f"ORDER BY message_time DESC, id DESC LIMIT 1", (key,)).fetchone()
        if not nxt or nxt["content_key"] in seen:
            return key                                   # 链断/成环：用手上这版兜底，不硬猜
        seen.add(nxt["content_key"]); key = nxt["content_key"]
    return key


def find_quoted_target(conn: sqlite3.Connection, kind: str,
                       mid: Optional[str], quoted_body: Optional[str]) -> Optional[str]:
    """定位被引用那条行（不限状态：先 mID 精确，再退回被引用正文 == 旧行 message_text），
    再顺 supersedes 链向后解析到该身份『当前生效版』返回——
    更新永远覆盖当前版本，即便引用的是已被覆盖的老版本，也不在历史上分叉。"""
    table = TABLES[kind]["table"]
    hit = None
    if mid:
        hit = conn.execute(f"SELECT content_key FROM {table} WHERE mid=? ORDER BY id DESC LIMIT 1",
                           (mid,)).fetchone()
    if not hit and quoted_body:
        hit = conn.execute(
            f"SELECT content_key FROM {table} WHERE COALESCE(message_text,'')=? "
            f"ORDER BY message_time DESC, id DESC LIMIT 1", (quoted_body,)).fetchone()
    if not hit:
        return None
    return _walk_to_current(conn, kind, hit["content_key"])


def classify_new_or_update(conn: sqlite3.Connection, kind: str, rec: Dict[str, Any],
                           resolved: Dict[str, str]) -> Tuple[str, Optional[str]]:
    """更新判定（给待审核页展示 + 暂存 target_key）：
    ① 产品身份（承运人+起运+目的+柜型/船名+ETD，【不含原文】）命中某当前生效行 → 更新；
    ② 本条原文带『——引用…——』指向某当前生效行 → 更新（单向）；③ 否则新增。
    身份不含原文：一条转发原文会拆成多柜型/多ETD，用原文当身份会互相误覆盖。"""
    probe = {"pol": resolved.get("起运港", ""), "pod": resolved.get("目的港", ""),
             "carrier": resolved.get("船司", ""),
             "container_type": norm_ctype(rec.get("柜型")) or str(rec.get("柜型") or "").strip(),
             "etd": str(rec.get("船期ETD") or "").strip(),
             "vessel_voyage": str(rec.get("船名航次") or "").strip(),
             "space_type": str(rec.get("舱位类型") or "").strip()}
    hit = _find_current_by_identity(conn, kind, probe)
    if hit:
        return ("update", hit["content_key"])
    _, body, mid = split_quote(rec.get("消息原文"))
    key = find_quoted_target(conn, kind, mid, body)
    return ("update", key) if key else ("new", None)


def push_with_review(conn: sqlite3.Connection, master: "Master", kind: str,
                     records: Iterable[Dict[str, Any]], origin: str = "manual",
                     commit: bool = True) -> Dict[str, Any]:
    """带闸门 + 自动新增/更新判定的录入：校验不过 → rejected（不写库）；
    身份命中当前生效行且内容有变 → 自动留痕（supersedes 指回旧行、旧行标已被覆盖）。
    commit=False 时全程不提交（approve_pending 事务模式：业务写入 + pending 状态 + edit_log
    一次 commit，消除"业务表已写但 pending 仍 awaiting → 崩溃后重复放行"的窗口）。"""
    now = now_cn()
    table = TABLES[kind]["table"]
    master.unmatched = []
    accepted = revised = 0
    rejected: List[Dict[str, Any]] = []
    keys: List[str] = []
    src_pairs: List[Dict[str, Any]] = []          # 成功入库行的来源群/发送人，末尾统一登记进字典库
    for rec in records:
        v = validate_record(conn, master, kind, rec)
        if not v["ok"]:
            rejected.append({"record": rec, "errors": v["errors"]})
            continue
        rows, seen = [], set()
        exp = master.expand_cn(dict(rec), conn=conn, allow_carrier_add=False)   # 人工入口不自动补主数据
        if len(exp) > MAX_EXPAND:
            rejected.append({"record": rec, "errors": [{"field": "展开", "input": "",
                          "msg": f"本条展开成 {len(exp)} 行，超过上限 {MAX_EXPAND}：请按实际配对拆开"}]})
            continue
        for cn in exp:
            key = (cn.get("消息ID"), cn.get("柜型"), cn.get("目的港"),
                   cn.get("海运费USD") or cn.get("价格USD"))
            if key in seen:
                continue
            seen.add(key)
            rows.append(normalize(cn, kind, now, origin))
        for r in rows:
            hit = _find_current_by_identity(conn, kind, r)
            target = hit["content_key"] if (hit and hit["content_key"] != r["content_key"]) else None
            if not target:                                   # 无同原文命中 → 看引用指向哪条旧行（只此单向）
                _, body, mid = split_quote(r.get("message_text"))
                target = find_quoted_target(conn, kind, mid, body)
                if target == r["content_key"]:
                    target = None
            if target:
                r["supersedes"] = target
            upsert_rows(conn, kind, [r], commit=False)       # 提交时机统一由本函数末尾/调用方控制
            keys.append(r["content_key"])
            src_pairs.append({"来源群": r.get("source_group"), "发送人": r.get("sender")})
            if r.get("supersedes"):
                conn.execute(f"UPDATE {table} SET status=?, overridden_time=? WHERE content_key=?",
                             (COVERED, now, r["supersedes"]))
                revised += 1
            else:
                accepted += 1
        if commit:
            conn.commit()
    # 写入即登记（只补不覆盖，非闸门）：来源群/发送人库里没有的一并登记，人工/批量/心跳三入口都走这里
    registered = _register_pairs(conn, src_pairs, commit=commit)
    if commit:
        conn.commit()
    return {"ok": True, "table": kind, "accepted": accepted, "revised": revised,
            "rejected": rejected, "keys": keys, "origin": origin,
            "unmatched": list(master.unmatched), "registered": registered}


# 待审核去重用的产品身份（中文键，和 IDENTITY_COLS 同口径，只是 pending 存的是中文字段）
_IDENTITY_CN = {
    "rates": ["船司", "起运港", "目的港", "柜型", "船期ETD"],
    "space": ["船司", "起运港", "目的港", "船名航次", "舱位类型", "船期ETD"],
}


def _pending_identity(kind: str, rec: Dict[str, Any]) -> tuple:
    rec = rec or {}
    if kind == "group_suggestion":          # 港群候选按群名归并，同群重复建议只留最新
        return (str(rec.get("港群") or "").strip(),)
    cols = _IDENTITY_CN.get(kind, _IDENTITY_CN["rates"])
    return tuple(str(rec.get(c) or "").strip() for c in cols)


GSUG_KIND = "group_suggestion"      # agent 只能"提港群候选"，人工放行才写 port_groups（港群是放大器，不让机器直接改）


def validate_group_suggestion(conn: sqlite3.Connection, rec: Dict[str, Any]) -> Dict[str, Any]:
    """港群候选校验：成员必须逐个命中 ports 规范名，否则整条打回。"""
    errors: List[Dict[str, str]] = []
    name = str(rec.get("港群") or "").strip()
    label = str(rec.get("标签") or "").strip()
    gkind = str(rec.get("类型") or "pod").strip()
    members = [m.strip() for m in split_list(rec.get("成员")) if m.strip()]
    if not name:
        errors.append({"field": "港群", "input": "", "msg": "港群名不得为空"})
    if gkind not in ("pol", "pod", "both"):
        errors.append({"field": "类型", "input": gkind, "msg": "类型只能是 pol/pod/both"})
    if not members:
        errors.append({"field": "成员", "input": "", "msg": "成员港不得为空"})
    known = {r[0] for r in conn.execute("SELECT name FROM ports WHERE active=1")}
    bad = [m for m in members if m not in known]
    if bad:
        errors.append({"field": "成员", "input": "、".join(bad),
                       "msg": "成员港不在 ports 规范名里（先补港口主数据或改成实际港名，不许造名）"})
    exists = bool(conn.execute("SELECT 1 FROM port_groups WHERE name=?", (name,)).fetchone()) if name else False
    return {"ok": not errors,
            "resolved": {"港群": name, "标签": label, "类型": gkind,
                         "成员": ",".join(members), "动作": "补充成员" if exists else "新建港群"},
            "errors": errors}


def stage_pending(conn: sqlite3.Connection, master: "Master", kind: str,
                  records: List[Dict[str, Any]], origin: str = "agent") -> List[int]:
    """非人工入口先按港拆分 + 归一（expand_cn），再逐条机械校验 + 判新增/更新，暂存待审核，绝不直接落业务表。
    合并港拆每港一条；港群名不拆；中文/别名/拼音归一。
    同产品身份若已有未处理(待审)旧条 → 撤旧留新（删旧），待审只留最新一条：
    避免连续拉同一报价的多次更新刷屏，也消除“先放新价再放旧价把旧价顶成当前”的顺序坑。"""
    now = now_cn()
    ids: List[int] = []
    awaiting_idx: Dict[tuple, List[int]] = {}
    for r in conn.execute("SELECT id,payload FROM pending_records WHERE kind=? AND review_status='awaiting'", (kind,)):
        try:
            p = json.loads(r["payload"])
        except Exception:
            p = {}
        awaiting_idx.setdefault(_pending_identity(kind, p), []).append(r["id"])
    for rec in records:
        if kind == GSUG_KIND:               # 港群候选：不展开、不落业务表，只校验+暂存，等人工放行
            one = dict(rec)
            ik = _pending_identity(kind, one)
            for old in awaiting_idx.pop(ik, []):
                conn.execute("DELETE FROM pending_records WHERE id=? AND review_status='awaiting'", (old,))
                if old in ids:
                    ids.remove(old)
            v = validate_group_suggestion(conn, one)
            cur = conn.execute(
                "INSERT INTO pending_records(kind,payload,origin,resolved,action,target_key,"
                "check_status,errors,review_status,orig_payload,created_at) VALUES(?,?,?,?,?,?,?,?, 'awaiting', ?, ?)",
                (kind, json.dumps(one, ensure_ascii=False), origin,
                 json.dumps(v["resolved"], ensure_ascii=False),
                 "update" if v["resolved"]["动作"] == "补充成员" else "new", "",
                 "passed" if v["ok"] else "failed",
                 json.dumps(v["errors"], ensure_ascii=False),
                 json.dumps(one, ensure_ascii=False), now))
            ids.append(cur.lastrowid)
            awaiting_idx.setdefault(ik, []).append(cur.lastrowid)
            continue
        src = {"来源群": rec.get("来源群"), "发送人": rec.get("发送人"),
               "消息ID": rec.get("消息ID"), "消息原文": rec.get("消息原文")}
        exp = master.expand_cn(rec, conn=conn, allow_carrier_add=True, src=src)
        if len(exp) > MAX_EXPAND:            # 多维合并炸行：不逐条暂存，只留一条超限工单交人工
            ik = _pending_identity(kind, rec)
            for old in awaiting_idx.pop(ik, []):
                conn.execute("DELETE FROM pending_records WHERE id=? AND review_status='awaiting'", (old,))
            err = [{"field": "展开", "input": "",
                    "msg": f"本条展开成 {len(exp)} 行，超过上限 {MAX_EXPAND}：合并船司/港口/柜型请按原文实际配对拆开再传"}]
            cur = conn.execute(
                "INSERT INTO pending_records(kind,payload,origin,resolved,action,target_key,"
                "check_status,errors,review_status,orig_payload,created_at) VALUES(?,?,?,?,?,?,?,?, 'awaiting', ?, ?)",
                (kind, json.dumps(rec, ensure_ascii=False), origin, json.dumps({}, ensure_ascii=False),
                 "new", "", "failed", json.dumps(err, ensure_ascii=False),
                 json.dumps(rec, ensure_ascii=False), now))
            ids.append(cur.lastrowid)
            awaiting_idx.setdefault(ik, []).append(cur.lastrowid)
            continue
        for one in exp:
            ik = _pending_identity(kind, one)
            for old in awaiting_idx.pop(ik, []):
                conn.execute("DELETE FROM pending_records WHERE id=? AND review_status='awaiting'", (old,))
                if old in ids:
                    ids.remove(old)
            v = validate_record(conn, master, kind, one)
            action, target = classify_new_or_update(conn, kind, one, v["resolved"])
            cur = conn.execute(
                "INSERT INTO pending_records(kind,payload,origin,resolved,action,target_key,"
                "check_status,errors,review_status,orig_payload,created_at) "
                "VALUES(?,?,?,?,?,?,?,?, 'awaiting', ?, ?)",
                (kind, json.dumps(one, ensure_ascii=False), origin,
                 json.dumps(v["resolved"], ensure_ascii=False), action, target,
                 "passed" if v["ok"] else "failed",
                 json.dumps(v["errors"], ensure_ascii=False),
                 json.dumps(one, ensure_ascii=False), now))
            nid = cur.lastrowid
            ids.append(nid)
            awaiting_idx.setdefault(ik, []).append(nid)
    conn.commit()
    return ids


def _pending_row(r: Optional[sqlite3.Row]) -> Optional[Dict[str, Any]]:
    if not r:
        return None
    d = dict(r)
    for k in ("payload", "resolved", "errors"):
        d[k] = json.loads(d[k] or "null")
    return d


def list_pending(conn: sqlite3.Connection, status: str = "awaiting", kind: str = "",
                 limit: int = 200, offset: int = 0, check: str = "") -> Dict[str, Any]:
    where, args = [], []
    if status and status != "all":
        where.append("review_status = ?"); args.append(status)
    if kind in ("rates", "space", GSUG_KIND):
        where.append("kind = ?"); args.append(kind)
    if check == "failed":
        where.append("check_status != 'passed'")
    elif check == "passed":
        where.append("check_status = 'passed'")
    frag = ("WHERE " + " AND ".join(where)) if where else ""
    total = conn.execute(f"SELECT COUNT(*) FROM pending_records {frag}", args).fetchone()[0]
    rows = conn.execute(f"SELECT * FROM pending_records {frag} "
                        f"ORDER BY check_status='failed' DESC, id DESC LIMIT ? OFFSET ?",
                        [*args, max(1, min(limit, 500)), max(0, offset)]).fetchall()
    return {"total": total, "rows": [_pending_row(r) for r in rows]}


# ---------------- 放行时自动登记来源群 / 发送人 ----------------
# 现象：录入端「来源群」下拉有预设值，但库管理里的群聊库/发送人库却是空的——
# 下拉吃的是业务表里 source_group/sender 的使用频次兜底，两库从来没人往里写。
# 这里补上写入口：条目放行入库时，把库里还没有的来源群、发送人分别登记进群聊库、发送人库。

def _ensure_chat_group(conn: sqlite3.Connection, name: str) -> bool:
    """群聊库里还没有这个群才新增；只补缺，绝不覆盖已有条目（免得抹掉维护好的默认起运港/归属/会话ID）。"""
    name = str(name or "").strip()
    if not name:
        return False
    if conn.execute("SELECT 1 FROM chat_groups WHERE name = ?", (name,)).fetchone():
        return False
    conn.execute("INSERT INTO chat_groups (name, active, source, created_at) VALUES (?, 1, 'auto', ?)",
                 (name, now_cn()))
    return True


def _ensure_sender(conn: sqlite3.Connection, name: str) -> bool:
    """发送人库里还没有这个人才新增。"""
    name = str(name or "").strip()
    if not name:
        return False
    if conn.execute("SELECT 1 FROM senders WHERE name = ?", (name,)).fetchone():
        return False
    conn.execute("INSERT INTO senders (name, active, source, created_at) VALUES (?, 1, 'auto', ?)",
                 (name, now_cn()))
    return True


def register_source_entities(conn: sqlite3.Connection, rec: Dict[str, Any]) -> Dict[str, Any]:
    """把一条记录里库中尚未登记的「来源群 / 发送人」写入群聊库、发送人库（只补不覆盖）。
    rec 接受中文字段（来源群/发送人）或英文列名（source_group/sender）。
    新群/新入库后失效主数据缓存，让候选下拉与归一索引立刻跟上。"""
    group = str(rec.get("来源群") or rec.get("source_group") or "").strip()
    sender = str(rec.get("发送人") or rec.get("sender") or "").strip()
    added_group = _ensure_chat_group(conn, group)
    added_sender = _ensure_sender(conn, sender)
    conn.commit()
    if added_group or added_sender:
        invalidate_master()
    return {"group": group or None, "sender": sender or None,
            "group_added": added_group, "sender_added": added_sender}


def _register_pairs(conn: sqlite3.Connection, pairs: List[Dict[str, Any]],
                    commit: bool = True) -> Dict[str, List[str]]:
    """一批记录里未登记的来源群/发送人去重后写进群聊库/发送人库（只补不覆盖，非闸门）。
    每个元素接受中文字段（来源群/发送人）或英文列名（source_group/sender）。
    返回本次真正新增的名字（保序），供上层提示与放行回报复用。人工 push / 批量导入 / 心跳放行共用。"""
    groups: List[str] = []
    senders: List[str] = []
    seen_g, seen_s = set(), set()
    for p in pairs or []:
        g = str(p.get("来源群") or p.get("source_group") or "").strip()
        s = str(p.get("发送人") or p.get("sender") or "").strip()
        if g and g not in seen_g:
            seen_g.add(g)
            if _ensure_chat_group(conn, g):
                groups.append(g)
        if s and s not in seen_s:
            seen_s.add(s)
            if _ensure_sender(conn, s):
                senders.append(s)
    if commit:
        conn.commit()
    if groups or senders:
        invalidate_master()
    return {"added_groups": groups, "added_senders": senders}


def approve_pending(conn: sqlite3.Connection, master: "Master", pid: int,
                    reviewed_by: str = "") -> Dict[str, Any]:
    row = conn.execute("SELECT * FROM pending_records WHERE id = ?", (pid,)).fetchone()
    if not row:
        return {"ok": False, "error": "not found"}
    if row["review_status"] != "awaiting":
        return {"ok": False, "error": "该条已处理"}
    if row["check_status"] != "passed":
        return {"ok": False, "error": "机械校验未通过，不能放行",
                "errors": json.loads(row["errors"] or "[]")}
    if row["kind"] == GSUG_KIND:            # 港群候选放行：只补成员、不覆盖人工维护项
        pl = json.loads(row["payload"] or "{}")
        v = validate_group_suggestion(conn, pl)
        if not v["ok"]:
            return {"ok": False, "error": "放行前校验未通过", "errors": v["errors"]}
        cur_g = conn.execute("SELECT label,members,specials,kind FROM port_groups WHERE name=?",
                             (pl["港群"],)).fetchone()
        cur_g = dict(cur_g) if cur_g else {}
        merged = list(split_list(cur_g.get("members")))
        added = [m for m in v["resolved"]["成员"].split(",") if m and m not in merged]
        merged += added
        res = master_save(conn, "port_groups", {
            "name": pl["港群"], "label": (cur_g or {}).get("label") or pl.get("标签") or "",
            "members": ",".join(merged), "specials": (cur_g or {}).get("specials") or "",
            "kind": (cur_g or {}).get("kind") or v["resolved"]["类型"],
            "note": "候选放行自 %s（%s）" % (pl.get("来源群") or "", pl.get("消息ID") or "")})
        conn.execute("UPDATE pending_records SET review_status='approved', final_key=?, "
                     "reviewed_at=?, reviewed_by=? WHERE id=?",
                     (res["key"], now_cn(), reviewed_by, pid))
        _append_edit_log(conn, pid, [{"ts": now_cn(), "actor": reviewed_by or "unknown",
                                      "action": "approve", "field": "", "old": None, "new": None}])
        conn.commit()
        return {"ok": True, "kind": GSUG_KIND, "result": res,
                "registered": {"group": pl["港群"], "added": added}}
    # 单事务放行（2026-09-17 审查修复）：业务写入 + pending 状态 + edit_log + 观察流候选
    # 一次 commit。此前 push_with_review 内部逐条 commit + 状态二次 commit，
    # 崩溃窗内业务表已写但 pending 仍 awaiting → 重复放行重复入库。
    res = push_with_review(conn, master, row["kind"], [json.loads(row["payload"])],
                           origin=row["origin"], commit=False)
    if res["rejected"]:
        conn.rollback()
        return {"ok": False, "error": "放行前校验未通过", "errors": res["rejected"][0]["errors"]}
    cur = conn.execute("UPDATE pending_records SET review_status='approved', final_key=?, "
                       "reviewed_at=?, reviewed_by=? WHERE id=? AND review_status='awaiting'",
                       (",".join(res["keys"]) or None, now_cn(), reviewed_by, pid))
    if not cur.rowcount:
        # 乐观锁：读到 awaiting 后被并发请求抢先处理 → 回滚本次业务写入，绝不重复入库
        conn.rollback()
        return {"ok": False, "error": "该条已被其他人处理，本次放行已回滚"}
    # P0 审计 + P3-A 观察流：对比 orig_payload（agent 原始判定）vs 最终 payload（人工放行版）
    payload = json.loads(row["payload"] or "{}")
    try:
        orig = json.loads(row["orig_payload"] or row["payload"] or "{}")
    except Exception:
        orig = dict(payload)
    diffs: List[Dict[str, Any]] = []
    for k in set(orig) | set(payload):
        if k == "消息原文":
            continue
        if str(orig.get(k, "")) != str(payload.get(k, "")):
            diffs.append({"ts": now_cn(), "actor": reviewed_by or "unknown", "action": "approve_diff",
                          "field": k, "old": orig.get(k), "new": payload.get(k)})
    _append_edit_log(conn, pid,
                     [{"ts": now_cn(), "actor": reviewed_by or "unknown", "action": "approve",
                       "field": "", "old": None, "new": None}] + diffs)
    n_obs = _observe_from_diffs(conn, payload, diffs, pid)
    conn.commit()          # 事务边界：以上全部生效或（异常时）全部不生效
    # 来源群/发送人的登记已在 push_with_review 内完成；这里还原成前端要的"单条"形状用于放行提示
    reg = res.get("registered") or {}
    g = str(payload.get("来源群") or payload.get("source_group") or "").strip()
    s = str(payload.get("发送人") or payload.get("sender") or "").strip()
    registered = {"group": g or None, "sender": s or None,
                  "group_added": bool(g) and g in (reg.get("added_groups") or []),
                  "sender_added": bool(s) and s in (reg.get("added_senders") or [])}
    return {"ok": True, "kind": row["kind"], "result": res, "registered": registered,
            "audit_diffs": len(diffs), "observed_candidates": n_obs}


_OBSERVE_FIELDS = {"目的港": "pod", "起运港": "pol", "船司": "carrier",
                   "航线": "route", "舱位类型": "space_type", "柜型": "ctype"}


def _append_edit_log(conn: sqlite3.Connection, pid: int, entries: List[Dict[str, Any]]) -> None:
    """追加审核动作日志（不 commit，由调用方统一提交）"""
    if not entries:
        return
    row = conn.execute("SELECT edit_log FROM pending_records WHERE id=?", (pid,)).fetchone()
    if not row:
        return
    try:
        log_list = json.loads(row["edit_log"] or "[]")
        if not isinstance(log_list, list):
            log_list = []
    except Exception:
        log_list = []
    log_list.extend(entries)
    conn.execute("UPDATE pending_records SET edit_log=? WHERE id=?",
                 (json.dumps(log_list, ensure_ascii=False), pid))


def _observe_from_diffs(conn: sqlite3.Connection, final_payload: Dict[str, Any],
                        diffs: List[Dict[str, Any]], pid: int) -> int:
    """P3-A agent 观察流：放行时人工把字段值 A 改成 B（值替换类）→ 按 sender+carrier+group
    三维打标生成候选（scope_key 群名末段约定）。old 为空的"补默认值"类不在此处理（走 P1 audit 流）。
    观察流失败绝不阻断放行。返回生成候选数。"""
    group = str(final_payload.get("来源群") or final_payload.get("source_group") or "").strip() or "未登记群"
    sender = str(final_payload.get("发送人") or final_payload.get("sender") or "").strip() or "未知发送人"
    carrier = str(final_payload.get("船司") or "").strip() or "未知船司"
    n = 0
    for d in diffs:
        kind = _OBSERVE_FIELDS.get(d.get("field") or "")
        old, new = d.get("old"), d.get("new")
        if not kind or new in (None, "") or str(new) == str(old or ""):
            continue
        if old in (None, ""):
            continue
        try:
            res = candidates_upsert(conn, {
                "scope_kind": "sender+carrier+group",
                "scope_key": f"{sender}|{carrier}|{group}",
                "target_layer": "alias_scope",
                "pattern_type": "alias",
                "alias": str(old),
                "kind": kind,
                "suggested_target": str(new),
                "reason": f"审核放行时人工把「{d['field']}」从「{old}」改成「{new}」（pending#{pid}）",
                "confidence": 0.5,
                "evidence_refs": [pid],
                "source": "agent_observation",
            })
            if res.get("action") in ("created", "updated"):
                n += 1
        except Exception as e:
            # 观察流是旁路，绝不阻断放行；只留痕
            try:
                conn.execute("UPDATE pending_records SET note=COALESCE(note,'')||? WHERE id=?",
                             (f"[observe-warn]{e}", pid))
            except Exception:
                pass
    return n


def update_pending(conn: sqlite3.Connection, master: "Master", pid: int,
                   fields: Dict[str, Any], kind: Optional[str] = None,
                   actor: str = "") -> Dict[str, Any]:
    """审核者就地编辑暂存记录：重跑机械校验 + 重判新增/更新，回写 payload 与校验态。
    编辑后 check_status 实时翻转（failed 修好变 passed，放行解禁）。
    kind 可传入以在运价/舱位间互切（卡片右上角切换），校验/必填随之改判。
    P0 审计：每次字段修改追加 edit_log {ts,actor,action,field,old,new}，供反向沉淀扫描。"""
    row = conn.execute("SELECT * FROM pending_records WHERE id = ?", (pid,)).fetchone()
    if not row:
        return {"ok": False, "error": "not found"}
    if row["review_status"] != "awaiting":
        return {"ok": False, "error": "该条已处理，不能编辑"}
    kind = kind if kind in ("rates", "space") else row["kind"]
    # 以旧 payload 为底，覆盖编辑过的中文键（保留未改字段，如图片引用/消息ID）
    rec = json.loads(row["payload"] or "{}")
    # P0 审计：覆盖前先算字段级 diff
    edits: List[Dict[str, Any]] = []
    for k, v in (fields or {}).items():
        if v is None:
            continue
        if str(rec.get(k, "")) != str(v):
            edits.append({"ts": now_cn(), "actor": actor or "unknown", "action": "update",
                          "field": k, "old": rec.get(k), "new": v})
    rec.update({k: v for k, v in (fields or {}).items() if v is not None})
    v = validate_record(conn, master, kind, rec)
    action, target = classify_new_or_update(conn, kind, rec, v["resolved"])
    conn.execute(
        "UPDATE pending_records SET kind=?, payload=?, resolved=?, action=?, target_key=?, "
        "check_status=?, errors=? WHERE id=?",
        (kind, json.dumps(rec, ensure_ascii=False), json.dumps(v["resolved"], ensure_ascii=False),
         action, target, "passed" if v["ok"] else "failed",
         json.dumps(v["errors"], ensure_ascii=False), pid))
    _append_edit_log(conn, pid, edits)
    conn.commit()
    return {"ok": True, "id": pid, "kind": kind,
            "check_status": "passed" if v["ok"] else "failed",
            "action": action, "target_key": target, "resolved": v["resolved"], "errors": v["errors"],
            "edits_recorded": len(edits)}


def reject_pending(conn: sqlite3.Connection, pid: int, note: str = "",
                   reviewed_by: str = "") -> Dict[str, Any]:
    """软驳：移入驳回日志（review_status='rejected'，留原因），行仍在库里，可恢复、可回查。"""
    cur = conn.execute(
        "UPDATE pending_records SET review_status='rejected', note=?, reviewed_at=?, reviewed_by=? "
        "WHERE id=? AND review_status='awaiting'", (note, now_cn(), reviewed_by, pid))
    if cur.rowcount:
        _append_edit_log(conn, pid, [{"ts": now_cn(), "actor": reviewed_by or "unknown",
                                      "action": "reject", "field": "", "old": None, "new": note}])
    conn.commit()
    return {"ok": bool(cur.rowcount)} if cur.rowcount else {"ok": False, "error": "not found/已处理"}


def restore_pending(conn: sqlite3.Connection, pid: int) -> Dict[str, Any]:
    """撤回误驳：rejected → awaiting，重回待处理队列。"""
    cur = conn.execute("UPDATE pending_records SET review_status='awaiting', note=NULL "
                       "WHERE id=? AND review_status='rejected'", (pid,))
    conn.commit()
    return {"ok": bool(cur.rowcount)} if cur.rowcount else {"ok": False, "error": "not found/非驳回态"}


def delete_pending(conn: sqlite3.Connection, pid: int, force: bool = False) -> Dict[str, Any]:
    """物理删除：默认仅驳回日志（rejected）内可删，防前端误调把 awaiting/approved 行直接丢掉。
    force=True 显式强删任意状态（运维/测试用，handler 需 body.force 明确传入）。
    顺带清 pending_shadow 里该 pending 的孤儿影子行。"""
    if force:
        cur = conn.execute("DELETE FROM pending_records WHERE id = ?", (pid,))
    else:
        cur = conn.execute(
            "DELETE FROM pending_records WHERE id = ? AND review_status = 'rejected'", (pid,))
    conn.execute("DELETE FROM pending_shadow WHERE source_pending_id = ?", (pid,))
    conn.commit()
    if not cur.rowcount and not force:
        row = conn.execute("SELECT review_status FROM pending_records WHERE id=?", (pid,)).fetchone()
        if row:
            return {"ok": False, "deleted": 0,
                    "error": f"该条状态是 {row['review_status']}，非驳回日志不允许物理删除（可用 force）"}
    return {"ok": bool(cur.rowcount), "deleted": cur.rowcount}


def _median(vals: List[Any]) -> float:
    xs = sorted(vals)
    n = len(xs)
    if n == 0:
        return 0.0
    return float(xs[n // 2]) if n % 2 else (xs[n // 2 - 1] + xs[n // 2]) / 2.0


def audit_all(conn: sqlite3.Connection, master: "Master") -> Dict[str, Any]:
    """全库体检：
    1) 标准化复查（原口径）——存量行核心字段命不命中主数据 → violations；
    2) 引用完整性/健康度（新增，只报告不阻断、不改数据）——孤儿群/人/港、超链断裂、
       过期仍生效、价格异常。守"来源群/发送人不强制对齐"的原则：这里只把你该看一眼的挑出来。"""
    out: List[Dict[str, Any]] = []
    integ: List[Dict[str, Any]] = []
    known_ports = {p["name"] for p in master.ports} | set(master.group_by_name)
    known_groups = {r[0] for r in conn.execute("SELECT name FROM chat_groups")}
    known_senders = {r[0] for r in conn.execute("SELECT name FROM senders")}
    today = today_cn()
    cur_status = {"rates": DEFAULT_STATUS["rates"], "space": DEFAULT_STATUS["space"]}
    id_cols = {"rates": ("pol", "pod", "carrier", "container_type"),
               "space": ("pol", "carrier", "space_type")}

    orphan_g: Dict[str, int] = {}
    orphan_s: Dict[str, int] = {}
    orphan_port: List[Dict[str, Any]] = []
    dup_current: List[Dict[str, Any]] = []
    expired: List[Dict[str, Any]] = []
    zero_price: List[Dict[str, Any]] = []
    outlier_price: List[Dict[str, Any]] = []

    for kind in ("rates", "space"):
        table = TABLES[kind]["table"]
        inv = {v: k for k, v in TABLES[kind]["map"].items()}
        rows = [dict(r) for r in conn.execute(f"SELECT * FROM {table}")]
        for r in rows:                                    # 1) 标准化：全表逐条复查（含历史/覆盖行）
            rec = {inv[c]: r[c] for c in r if c in inv}
            v = validate_record(conn, master, kind, rec)
            if not v["ok"]:
                out.append({"kind": kind, "content_key": r["content_key"],
                            "pol": r["pol"], "pod": r["pod"], "carrier": r["carrier"],
                            "errors": v["errors"]})
        cur = [r for r in rows if r.get("status") == cur_status[kind]]   # 2) 健康度只看当前生效
        grp: Dict[tuple, List[Dict[str, Any]]] = {}
        cohort: Dict[tuple, List[Dict[str, Any]]] = {}
        for r in cur:
            g = str(r.get("source_group") or "").strip()
            if g and g not in known_groups:
                orphan_g[g] = orphan_g.get(g, 0) + 1
            s = str(r.get("sender") or "").strip()
            if s and s not in known_senders:
                orphan_s[s] = orphan_s.get(s, 0) + 1
            for col in ("pol", "pod"):
                val = str(r.get(col) or "").strip()
                if val and val not in known_ports:
                    orphan_port.append({"kind": kind, "content_key": r["content_key"],
                                        "field": "起运港" if col == "pol" else "目的港", "value": val,
                                        "pol": r.get("pol"), "pod": r.get("pod"), "carrier": r.get("carrier")})
            vt = str(r.get("valid_to") or "").strip()
            if vt and vt < today:
                expired.append({"kind": kind, "content_key": r["content_key"], "pol": r.get("pol"),
                                "pod": r.get("pod"), "carrier": r.get("carrier"), "valid_to": vt})
            key = tuple(str(r.get(c) or "") for c in id_cols[kind])
            grp.setdefault(key, []).append(r)
            if kind == "rates":
                pv = r.get("freight_usd")
                if not isinstance(pv, (int, float)) or pv is None or pv <= 0:
                    zero_price.append({"kind": kind, "content_key": r["content_key"], "pol": r.get("pol"),
                                       "pod": r.get("pod"), "carrier": r.get("carrier")})
                elif pv > 0:
                    ck = (str(r.get("pod") or ""), str(r.get("carrier") or ""), str(r.get("container_type") or ""))
                    cohort.setdefault(ck, []).append(r)
        for key, rs in grp.items():
            if len(rs) > 1:
                dup_current.append({"kind": kind, "identity": list(key), "count": len(rs),
                                    "content_keys": [x["content_key"] for x in rs]})
        for ck, rs in cohort.items():
            if len(rs) < 3:
                continue
            m = _median([x["freight_usd"] for x in rs])
            if m <= 0:
                continue
            for x in rs:
                pv = x["freight_usd"]
                if pv < 0.5 * m or pv > 2 * m:
                    outlier_price.append({"kind": kind, "content_key": x["content_key"], "pol": x.get("pol"),
                                          "pod": x.get("pod"), "carrier": x.get("carrier"),
                                          "freight_usd": pv, "median": round(m, 1)})

    def _add(t: str, items: List[Dict[str, Any]], detail: str) -> None:
        if items:
            integ.append({"type": t, "count": len(items), "detail": detail, "items": items[:20]})

    _add("来源群未入库", [{"value": k, "count": c} for k, c in
         sorted(orphan_g.items(), key=lambda kv: -kv[1])],
         "业务里有、群聊库没有——此后放行/导入会自动登记，历史存量可去库管理补默认起运港/归属")
    _add("发送人未入库", [{"value": k, "count": c} for k, c in
         sorted(orphan_s.items(), key=lambda kv: -kv[1])], "业务里有、发送人库没有")
    _add("港口不在主数据", orphan_port, "起运/目的港既不是规范港名也不是港群名，疑脏值，需补港口或港群")
    _add("同身份多条当前生效", dup_current, "同一身份出现多条当前生效，超链疑似断裂，建议就地合并/覆盖")
    _add("已过期仍当前生效", expired, "有效期止已过但状态仍是当前生效，需改状态或更新")
    _add("价格为0/空", zero_price, "当前生效运价缺价格，疑录入漏")
    _add("价格离群(疑录入错)", outlier_price, "同 目的港+船司+柜型 相对中位数 <0.5 倍或 >2 倍")

    return {"ok": True, "violations": out, "count": len(out),
            "integrity": integ, "integrity_count": sum(c["count"] for c in integ)}


def audit_master(conn: sqlite3.Connection, master: "Master") -> Dict[str, Any]:
    """主数据自身体检（只报告、不阻断）。这七类问题 2026-09-12 全靠人眼抓出来，这里变成每次自动查。"""
    issues: List[Dict[str, Any]] = []

    def _add(code: str, items: List[Any], detail: str, level: str = "warn") -> None:
        if items:
            issues.append({"code": code, "level": level, "count": len(items),
                           "samples": items[:10], "detail": detail})

    known = {p["name"] for p in master.ports}
    _add("港群成员不在 ports 规范名",
         ["%s←%s" % (g["name"], m) for g in master.group_rows
          for m in split_list(g.get("members")) + split_list(g.get("specials")) if m and m not in known],
         "双向展开会静默断链；新保存已被 master_save 拦住，这里是存量", "error")

    for label, rows_, pk, cols_ in (
            ("ports", master.ports, "name", ["name", "name_cn", "name_en", "city", "aliases"]),
            ("carriers", master.carriers, "code", ["code", "name_en", "name_cn", "aliases"]),
            ("routes", master.routes, "name", ["name", "aliases"])):
        buckets: Dict[str, set] = {}
        for r in rows_:
            for k in [r.get(c) for c in cols_] + split_list(r.get("aliases")):
                fk = fold(k)
                if fk:
                    buckets.setdefault(fk, set()).add(r[pk])
        _add("%s 歧义死别名" % label,
             sorted(("%s→%d港" % (k, len(v)) for k, v in buckets.items() if len(v) > 1),
                    key=lambda s: -int(s.split("→")[1].rstrip("港"))),
             "折叠后指向多个实体 → 索引里被整条丢弃，写了不生效；消歧请用 LOCODE 或加国家后缀规范名")

    # 折叠口径必须与索引一致（fold 保留空格、只压标点），否则会把 "Vera Cruz / Veracruz" 误报成重复
    by_fold: Dict[str, List[str]] = {}
    for r in conn.execute("SELECT name FROM ports WHERE active=1"):
        by_fold.setdefault(fold(r[0]), []).append(r[0])
    dup = [" / ".join(v) for v in by_fold.values() if len(v) > 1]
    _add("规范名折叠后重复", dup, "裸词命中不了（歧义丢弃），确认是不同港就补 LOCODE 别名、是同一港就合并")
    _add("规范名括号不闭合",
         [r[0] for r in conn.execute("SELECT name FROM ports WHERE name LIKE '%(%' AND name NOT LIKE '%)'")],
         "多为长名截断，会让别名拆分与匹配都出问题", "error")
    _add("locode_ref 缺失",
         [r[0] for r in conn.execute("SELECT name FROM ports WHERE active=1 AND (locode_ref IS NULL OR locode_ref='')")],
         "没有 LOCODE，跨国同名港就只能靠国家后缀规范名消歧")
    n_all, n_act, n_auto = conn.execute(
        "SELECT COUNT(*), SUM(active), SUM(CASE WHEN source='auto' THEN 1 ELSE 0 END) FROM ports").fetchone()
    if n_all and n_act == n_all and n_auto * 2 > n_all:
        _add("active 没收口（auto 长尾全在索引里）",
             ["ports 共 %d 行，active=1 占 %d，其中 source='auto' %d 行" % (n_all, n_act, n_auto)],
             "长尾小港全量参与匹配会扩大误命中面；建议把从未被引用的 auto 行置 active=0（先出清单人工勾）")
    _add("senders 主键里嵌了电话",
         [r[0] for r in conn.execute("SELECT name FROM senders WHERE name GLOB '*[0-9][0-9][0-9][0-9][0-9][0-9][0-9]*'")],
         "对方换号就变成两个发送人，频次统计与权限归属会断；建议拆 name/phone 列")

    return {"ok": not any(i["level"] == "error" for i in issues),
            "issues": issues, "issue_count": sum(i["count"] for i in issues)}


def revise_record(conn: sqlite3.Connection, master: Master, kind: str, content_key: str,
                  cn_fields: Dict[str, Any], origin: str = "manual") -> Dict[str, Any]:
    """留痕式更新：追加新行（supersedes 指回旧行）+ 旧行标已被覆盖 + 现算变动摘要。

    新行指纹由本函数自己算，不回查数据库——按"origin+时间倒序找最新几条"
    会把别人刚录的记录误标成新版。
    """
    spec = TABLES[kind]
    table = spec["table"]
    now = now_cn()
    old = conn.execute(f"SELECT * FROM {table} WHERE content_key = ?", (content_key,)).fetchone()
    if old is None:
        return {"ok": False, "error": "record not found"}
    inv = {v: k for k, v in spec["map"].items()}
    rec: Dict[str, Any] = {inv[c]: old[c] for c in old.keys() if c in inv and old[c] is not None}
    for cn, val in (cn_fields or {}).items():
        col = spec["map"].get(cn)
        if col and col not in UPDATE_BLOCKED:
            rec[cn] = val
    rec.pop("记录状态", None)                 # 新行按默认状态，旧行才标覆盖
    rec.pop("被覆盖消息时间", None)

    master.unmatched = []
    rows = [normalize(cn, kind, now, origin) for cn in master.expand_cn(dict(rec))]
    rows = [r for r in rows if r["content_key"] != content_key]   # 指纹没变 = 没有实质改动
    if not rows:
        return {"ok": False, "error": "没有实质改动（新旧内容指纹相同）"}
    upsert_rows(conn, kind, rows)
    for r in rows:
        conn.execute(f"UPDATE {table} SET supersedes = ? WHERE content_key = ?",
                     (content_key, r["content_key"]))
    conn.execute(f"UPDATE {table} SET status = ?, overridden_time = ? WHERE content_key = ?",
                 (COVERED, now, content_key))
    conn.commit()
    changes = []
    for col in spec["key"]:
        if col == "message_time":
            continue                          # 录入时间每次都变，写进摘要是噪声
        a = "" if old[col] is None else str(old[col])
        b = "" if rows[0].get(col) is None else str(rows[0][col])
        if a != b:
            changes.append({"col": col, "label": inv.get(col, col), "from": a, "to": b})
    return {"ok": True, "table": kind, "old_key": content_key,
            "new_keys": [r["content_key"] for r in rows], "revised": len(rows),
            "changes": changes, "unmatched": list(master.unmatched)}


def update_record(conn: sqlite3.Connection, kind: str, content_key: str,
                  cn_fields: Dict[str, Any]) -> Dict[str, Any]:
    """就地改（笔误、备注这类小修），要留痕请走 revise_record。"""
    spec = TABLES[kind]
    sets, args, ignored = [], [], []
    for cn, val in (cn_fields or {}).items():
        col = spec["map"].get(cn)
        if not col or col in UPDATE_BLOCKED:
            ignored.append(cn)
            continue
        sets.append(f"{col} = ?")
        args.append(coerce(val))
    if not sets:
        return {"updated": 0, "ignored": ignored}
    args.append(content_key)
    cur = conn.execute(f'UPDATE {spec["table"]} SET {", ".join(sets)} WHERE content_key = ?', args)
    conn.commit()
    return {"updated": cur.rowcount, "ignored": ignored}


def delete_record(conn: sqlite3.Connection, kind: str, content_key: str) -> int:
    cur = conn.execute(f'DELETE FROM {TABLES[kind]["table"]} WHERE content_key = ?', (content_key,))
    conn.commit()
    return cur.rowcount


# ---------------- 查询 ----------------

def _lit(v: Any) -> str:
    return "'" + str(v).replace("'", "''") + "'"


def scope_clause(conn: sqlite3.Connection, viewer: str) -> str:
    """分区可见范围：航管只看得见自己负责的来源群/航线；查无此人或停用 → 不限制（不误锁死）。"""
    if not viewer:
        return ""
    row = conn.execute("SELECT routes, groups, match_mode FROM operators WHERE name = ? AND active = 1",
                       (viewer,)).fetchone()
    if row is None:
        return ""
    groups, routes = split_list(row["groups"]), split_list(row["routes"])
    if not groups and not routes:
        return ""
    parts = []
    if groups:
        parts.append("source_group IN (" + ",".join(_lit(v) for v in groups) + ")")
    if routes:
        parts.append("route IN (" + ",".join(_lit(v) for v in routes) + ")")
    joiner = " AND " if (row["match_mode"] or "any") == "all" and len(parts) == 2 else " OR "
    return "(" + joiner.join(parts) + ")"


def _multi_filter(master: "Master", col: str, raw: Any, expand_groups: bool) -> Tuple[str, List[Any]]:
    """pol/pod 支持逗号分隔多值：每值内部走 port_filter（保留群↔成员双向展开），多值之间 OR。
    单值时等价原行为；跨字段（pol 与 pod 同时有值）由上层 AND 组合 = 交集。"""
    toks = [t.strip() for t in str(raw or "").split(",") if t.strip()]
    if not toks:
        return f"{col} = ?", [""]
    frags: List[str] = []; args: List[Any] = []
    for t in toks:
        if expand_groups and master.group_by_name:
            f, v = master.port_filter(col, t)
        else:
            f, v = f"{col} = ?", [t]
        frags.append(f); args += v
    return ("(" + " OR ".join(frags) + ")"), args


def _where(conn: sqlite3.Connection, master: Master, kind: str, q: Dict[str, str],
           with_status: bool = True) -> Tuple[str, List[Any]]:
    spec = TABLES[kind]
    cols = set(spec["map"].values()) | {"origin", "transit_days", "supersedes"}
    expand_groups = (q.get("nogroup") or "") != "1"   # 销售端传 nogroup=1：不展开港群，按单港精确匹配
    where, args = [], []
    for key, col in FILTERS.items():
        if key == "status" and not with_status:
            continue                          # 折叠视图要带出历史行
        val = q.get(key)
        if not val or val == "all" or col not in cols:
            continue
        if col in ("pol", "pod"):
            frag, vals = _multi_filter(master, col, val, expand_groups)   # 多值 OR；每值保留群↔成员双向展开
            where.append(frag)
            args += vals
        else:
            where.append(f"{col} = ?")
            args.append(val)
    kw = (q.get("q") or "").strip()
    if kw:
        like = f"%{kw}%"
        where.append("(pod LIKE ? OR message_text LIKE ? OR remark LIKE ? OR validity_raw LIKE ?)")
        args += [like] * 4
    scope = scope_clause(conn, q.get("viewer") or "")
    if scope:
        where.append(scope)
    return ("WHERE " + " AND ".join(where)) if where else "", args


def query(conn: sqlite3.Connection, master: Master, kind: str, q: Dict[str, str]) -> Dict[str, Any]:
    table = TABLES[kind]["table"]
    frag, args = _where(conn, master, kind, q)
    total = conn.execute(f"SELECT COUNT(*) FROM {table} {frag}", args).fetchone()[0]
    limit = max(1, min(int(q.get("limit") or 50), 500))
    offset = max(0, int(q.get("offset") or 0))
    rows = conn.execute(f"SELECT * FROM {table} {frag} ORDER BY message_time DESC, id DESC "
                        f"LIMIT ? OFFSET ?", args + [limit, offset]).fetchall()
    return {"total": total, "limit": limit, "offset": offset, "rows": [dict(r) for r in rows]}


def query_grouped(conn: sqlite3.Connection, master: Master, kind: str,
                  q: Dict[str, str]) -> Dict[str, Any]:
    """折叠：同一条报价的多柜型并成一行；同身份的当前价与历史价分开。"""
    spec = TABLES[kind]
    frag, args = _where(conn, master, kind, q, with_status=False)
    rows = [dict(r) for r in conn.execute(
        f'SELECT * FROM {spec["table"]} {frag} ORDER BY message_time DESC, id DESC LIMIT 10000', args)]
    price_col = "freight_usd" if kind == "rates" else "price_usd"
    today = today_cn()
    quotes: Dict[tuple, Dict[str, Any]] = {}
    for r in rows:
        qk = (r.get("carrier"), r.get("route"), r.get("pol"), r.get("pod"),
              r.get("etd") or "", r.get("message_time"), r.get("sender"))
        qt = quotes.get(qk)
        if qt is None:
            qt = {k: r.get(k) for k in GROUP_FIELDS}
            qt["cab"] = {}
            quotes[qk] = qt
        ct = norm_ctype(r.get("container_type"))
        pv = r.get(price_col)
        if ct and pv not in (None, ""):
            qt["cab"][ct] = pv
    byid: Dict[tuple, List[Dict[str, Any]]] = {}
    for qt in quotes.values():
        qt["expired"] = bool(qt.get("valid_to")) and str(qt["valid_to"])[:10] < today
        byid.setdefault((qt["carrier"], qt["route"], qt["pol"], qt["pod"], qt["etd"] or ""), []).append(qt)
    groups = []
    for idk, lst in byid.items():
        lst.sort(key=lambda x: x.get("message_time") or "", reverse=True)
        alive = [x for x in lst if not x["expired"] and x.get("status") != COVERED]
        cur = alive[0] if alive else None
        groups.append({"id": {"carrier": idk[0], "route": idk[1], "pol": idk[2],
                              "pod": idk[3], "etd": idk[4]},
                       "current": cur, "history": [x for x in lst if x is not cur]})
    groups.sort(key=lambda g: (_latest(g), g["id"]["carrier"] or "", g["id"]["pod"] or ""),
                reverse=True)
    limit = max(1, min(int(q.get("limit") or 50), 200))
    offset = max(0, int(q.get("offset") or 0))
    return {"total": len(groups), "limit": limit, "offset": offset,
            "groups": groups[offset:offset + limit], "rows": rows}


def _latest(g: Dict[str, Any]) -> str:
    q = g["current"] or (g["history"][0] if g["history"] else None)
    return (q or {}).get("message_time") or ""


OPT_COLS = {"rates": ["pol", "pod", "route", "carrier", "container_type", "source_group"],
            "space": ["pol", "pod", "route", "carrier", "container_type", "space_type",
                      "source_group"]}


def options(conn: sqlite3.Connection) -> Dict[str, Any]:
    out = {}
    for kind, cols in OPT_COLS.items():
        table = TABLES[kind]["table"]
        out[kind] = {c: [r[0] for r in conn.execute(
            f"SELECT DISTINCT {c} FROM {table} WHERE {c} IS NOT NULL AND {c} <> '' ORDER BY {c}")]
            for c in cols}
    return out


def stats(conn: sqlite3.Connection) -> Dict[str, Any]:
    def one(sql: str, args=()):
        r = conn.execute(sql, args).fetchone()
        return r[0] if r and r[0] is not None else 0     # 空表安全，不在这上面炸
    latest = max(str(one("SELECT message_time FROM freight_rates ORDER BY message_time DESC LIMIT 1") or ""),
                 str(one("SELECT message_time FROM space_records ORDER BY message_time DESC LIMIT 1") or ""))
    opts = options(conn)
    return {"ratesTotal": one("SELECT COUNT(*) FROM freight_rates"),
            "ratesCurrent": one("SELECT COUNT(*) FROM freight_rates WHERE status = ?",
                                (DEFAULT_STATUS["rates"],)),
            "spaceTotal": one("SELECT COUNT(*) FROM space_records"),
            "groups": len(opts["rates"]["source_group"]),
            "carriers": len(opts["rates"]["carrier"]),
            "latestMessage": latest or ""}


def freq(conn: sqlite3.Connection, col: str) -> List[Dict[str, Any]]:
    """使用频次现算不存字段：下拉按它降序排。"""
    return [{"name": r[0], "n": r[1]} for r in conn.execute(
        f"SELECT {col}, COUNT(*) FROM (SELECT {col} FROM freight_rates "
        f"UNION ALL SELECT {col} FROM space_records) "
        f"WHERE {col} IS NOT NULL AND {col} <> '' GROUP BY {col} ORDER BY 2 DESC")]


# ---------------- 账号库 / 群聊库 ----------------

def list_operators(conn: sqlite3.Connection) -> List[Dict[str, Any]]:
    out = []
    for r in conn.execute("SELECT * FROM operators ORDER BY active DESC, id"):
        d = dict(r)
        d["routes_list"] = split_list(d.get("routes"))
        d["groups_list"] = split_list(d.get("groups"))
        out.append(d)
    return out


def save_operator(conn: sqlite3.Connection, data: Dict[str, Any]) -> Dict[str, Any]:
    name = str(data.get("name") or "").strip()
    if not name:
        raise ValueError("name required")
    mode = data.get("match_mode") or "any"
    if mode not in ("any", "all"):
        raise ValueError("match_mode 只能是 any(群或航线命中) 或 all(两维都命中)")
    cols = {"title": coerce(data.get("title")),
            "routes": ",".join(split_list(data.get("routes"))),
            "groups": ",".join(split_list(data.get("groups"))), "match_mode": mode,
            "active": 1 if str(data.get("active", 1)) in ("1", "true", "True") else 0,
            "note": coerce(data.get("note"))}
    return _upsert_named(conn, "operators", name, cols)


def list_chat_groups(conn: sqlite3.Connection) -> List[Dict[str, Any]]:
    return [dict(r) for r in conn.execute("SELECT * FROM chat_groups ORDER BY active DESC, name")]


def save_chat_group(conn: sqlite3.Connection, data: Dict[str, Any]) -> Dict[str, Any]:
    name = str(data.get("name") or "").strip()
    if not name:
        raise ValueError("name required")
    cols = {"cid": coerce(data.get("cid")), "default_pol": coerce(data.get("default_pol")),
            "region": coerce(data.get("region")), "owner": coerce(data.get("owner")),
            "active": 1 if str(data.get("active", 1)) in ("1", "true", "True") else 0,
            "note": coerce(data.get("note")), "source": "manual"}
    exists = conn.execute("SELECT 1 FROM chat_groups WHERE name = ?", (name,)).fetchone()
    if exists and not cols["cid"]:
        cols.pop("cid")            # 更新时别把已有 cid 覆盖成空
    return _upsert_named(conn, "chat_groups", name, cols)


def _upsert_named(conn: sqlite3.Connection, table: str, name: str,
                  cols: Dict[str, Any]) -> Dict[str, Any]:
    exists = conn.execute(f"SELECT 1 FROM {table} WHERE name = ?", (name,)).fetchone()
    if exists:
        conn.execute(f'UPDATE {table} SET {", ".join(f"{k} = ?" for k in cols)} WHERE name = ?',
                     [*cols.values(), name])
        action = "updated"
    else:
        cols["name"] = name
        cols["created_at"] = now_cn()
        conn.execute(f'INSERT INTO {table} ({",".join(cols)}) VALUES ({",".join("?" * len(cols))})',
                     list(cols.values()))
        action = "created"
    conn.commit()
    return {"ok": True, "action": action, "name": name}


def delete_named(conn: sqlite3.Connection, table: str, name: str) -> int:
    if table not in ("operators", "chat_groups"):
        raise ValueError("table must be operators or chat_groups")
    cur = conn.execute(f"DELETE FROM {table} WHERE name = ?", (name,))
    conn.commit()
    return cur.rowcount


def import_chat_groups(conn: sqlite3.Connection, groups: List[Dict[str, Any]]) -> Dict[str, int]:
    created = updated = 0
    for g in groups:
        name = str((g or {}).get("name") or "").strip()
        if not name:
            continue
        if conn.execute("SELECT 1 FROM chat_groups WHERE name = ?", (name,)).fetchone():
            conn.execute("UPDATE chat_groups SET cid = ?, default_pol = ? WHERE name = ?",
                         (coerce(g.get("cid")), coerce(g.get("default_pol")), name))
            updated += 1
        else:
            conn.execute("INSERT INTO chat_groups (name, cid, default_pol, active, created_at) "
                         "VALUES (?, ?, ?, 1, ?)",
                         (name, coerce(g.get("cid")), coerce(g.get("default_pol")), now_cn()))
            created += 1
    conn.commit()
    invalidate_master()          # 群的默认起运港会影响归一兜底
    return {"created": created, "updated": updated}


# ---------------- 主数据载荷与维护 ----------------

def master_payload(conn: sqlite3.Connection, full: bool = False) -> Dict[str, Any]:
    m = get_master(conn)
    out: Dict[str, Any] = {
        "ok": True,
        "ports": [{"n": p["name"], "c": p.get("name_cn") or "", "e": p.get("name_en") or "",
                   "a": p.get("aliases") or "", "k": p.get("kind") or "pod",
                   "r": p.get("route") or "", "y": p.get("country") or ""} for p in m.ports],
        "carriers": [{"n": c["code"], "c": c.get("name_cn") or "", "e": c.get("name_en") or "",
                      "a": c.get("aliases") or "", "o": c.get("note") or ""} for c in m.carriers],
        "routes": [{"n": r["name"], "g": r.get("region") or "", "a": r.get("aliases") or ""}
                   for r in m.routes],
        "senders": list(m.senders),
        "portGroups": [{"n": g["name"], "l": g.get("label") or "", "m": g.get("members") or "",
                        "s": g.get("specials") or "", "k": g.get("kind") or "pod",
                        "o": g.get("note") or ""} for g in m.group_rows],
        "aliasScope": [{"al": r["alias"], "k": r["kind"], "t": r["target"],
                        "g": r["scope_group"] or "*", "n": r["note"] or ""}
                       for r in conn.execute("SELECT * FROM alias_scope ORDER BY alias, kind")],
        "groupFreq": freq(conn, "source_group"),
        "senderFreq": freq(conn, "sender"),
        "carrierFreq": freq(conn, "carrier"),
    }
    if full:
        out["raw"] = {"ports": m.ports, "carriers": m.carriers, "routes": m.routes,
                      "senders": [dict(r) for r in conn.execute(
                          "SELECT * FROM senders ORDER BY active DESC, name")],
                      "port_groups": m.group_rows}
    return out


def master_save(conn: sqlite3.Connection, table: str, row: Dict[str, Any]) -> Dict[str, Any]:
    if table not in MASTER_TABLES:
        raise ValueError("table must be one of ports/carriers/routes/senders/port_groups")
    pk = MASTER_TABLES[table]["pk"]
    name = str(row.get(pk) or "").strip()
    if not name:
        raise ValueError(f"{pk} required")
    cols: Dict[str, Any] = {}
    for c in MASTER_COLS[table]:
        if c == pk or c not in row:
            continue
        v = row[c]
        if c == "active":
            v = 1 if str(v) in ("1", "true", "True") else 0
        elif c == "sort":
            v = int(v) if str(v).strip().isdigit() else 900
        else:
            v = coerce(v)
        cols[c] = v
    if table == "senders":
        cols["source"] = "manual"     # 人工在库维护/新建的发送人 → 认领为人工维护（对齐群聊库语义）
    if table == "port_groups":        # 成员必须命中规范名：写错一个字母就会静默断掉双向展开（2026-09-12 体检教训）
        known = {r[0] for r in conn.execute("SELECT name FROM ports WHERE active=1")}
        bad = [m for f in ("members", "specials") for m in split_list(cols.get(f))
               if m and m not in known]
        if bad:
            raise ValueError("成员港不在 ports 规范名里，双向展开会断链，请先补主数据：" + "、".join(bad))
    tname = MASTER_TABLES[table]["table"]
    exists = conn.execute(f"SELECT 1 FROM {tname} WHERE {pk} = ?", (name,)).fetchone()
    if exists:
        conn.execute(f'UPDATE {tname} SET {", ".join(f"{k} = ?" for k in cols)} WHERE {pk} = ?',
                     [*cols.values(), name])
        action = "updated"
    else:
        cols[pk] = name
        cols["created_at"] = now_cn()
        conn.execute(f'INSERT INTO {tname} ({",".join(cols)}) VALUES ({",".join("?" * len(cols))})',
                     list(cols.values()))
        action = "created"
    conn.commit()
    invalidate_master()
    return {"ok": True, "table": table, "action": action, "key": name}


def master_delete(conn: sqlite3.Connection, table: str, key: str) -> Dict[str, Any]:
    if table not in MASTER_TABLES:
        raise ValueError("table must be one of ports/carriers/routes/senders/port_groups")
    pk, tname = MASTER_TABLES[table]["pk"], MASTER_TABLES[table]["table"]
    cur = conn.execute(f"DELETE FROM {tname} WHERE {pk} = ?", (key,))
    conn.commit()
    invalidate_master()
    return {"ok": bool(cur.rowcount), "table": table, "deleted": cur.rowcount, "key": key}


# ---------------- alias_scope 生产写接口（三列联合主键，独立于 master_save 单 pk 通用逻辑） ----------------

ALIAS_KINDS = ("pol", "pod", "carrier", "route", "space_type", "ctype", "price", "remark", "schedule")
ALIAS_SPACES = ("manual", "seed_v1", "candidate_approved", "imported")


def alias_scope_save(conn: sqlite3.Connection, row: Dict[str, Any]) -> Dict[str, Any]:
    """新增/更新 alias_scope 一行。主键 (alias, kind, scope_group)。
    职责边界：alias_scope 只承担"带作用域的歧义消解"——同一写法在特定群/特定列下要归到哪个实体。
    **scope_group='*' 的"全局别名"不该进这里**，那属于"实体自述"（如 MSC 有几种中文叫法），
    应写到 ports.aliases / carriers.aliases / routes.aliases 字段（走 /api/master/save）。
    校验：kind 白名单；scope_group 必填具体群名且在 chat_groups；target 建议命中主数据但不阻断（warning 提示）。
    source 缺省视为 'manual'（人工入口），已存在的 seed_v1 行被 manual 覆盖时改标 manual 保留 evidence_count。"""
    alias = str(row.get("alias") or "").strip()
    kind = str(row.get("kind") or "").strip()
    scope_group = str(row.get("scope_group") or "").strip()
    target = str(row.get("target") or "").strip()
    note = str(row.get("note") or "").strip()
    if not alias:
        raise ValueError("alias 必填")
    if kind not in ALIAS_KINDS:
        raise ValueError(f"kind 需在 {ALIAS_KINDS} 内")
    if not target:
        raise ValueError("target 必填")
    if not scope_group or scope_group == "*":
        raise ValueError(
            "scope_group 必须是具体群名（如「宁波舱位滚动更新群」）。"
            "想加**全局别名**（任何群任何列都归到某实体）请走映射库：/api/master/save 表=ports/carriers/routes，"
            "编辑实体的 aliases 字段加一条即可。alias_scope 只承担群+列作用域的歧义消解。"
        )
    warnings: List[str] = []
    # scope_group 检查 chat_groups 里存不存在
    exists = conn.execute("SELECT 1 FROM chat_groups WHERE name=? LIMIT 1", (scope_group,)).fetchone()
    if not exists:
        warnings.append(f"scope_group={scope_group} 不在 chat_groups，运行时会永远命中不到（保留写入）")
    # target 建议性校验：kind 属 pol/pod/carrier/route/space_type/ctype 时看主数据是否命中
    m = get_master(conn)
    if kind in ("pol", "pod"):
        hit = m.lookup_port(target, "pod", "*") or m.lookup_port(target, "pol", "*")
        if not hit or not hit[1]:
            # 允许多值串（"WCSA、MEX"）与港群代号
            parts = [p for p in split_list(target) if p] or [target]
            all_hit = all((m.lookup_port(p, "pod", "*") or m.lookup_port(p, "pol", "*"))[1] for p in parts)
            if not all_hit:
                warnings.append(f"target={target} 未在 ports/port_groups 主数据里，请核对写法")
    elif kind == "carrier":
        if not m.lookup(target, m.carrier_idx):
            warnings.append(f"target={target} 未在 carriers 主数据里")
    elif kind == "route":
        if not m.lookup(target, m.route_idx):
            warnings.append(f"target={target} 未在 routes 主数据里")
    elif kind == "space_type" and target not in SPACE_TYPE_ENUM:
        warnings.append(f"target={target} 不在舱位类型 11 档内")
    elif kind == "ctype" and target not in CTYPE_ENUM:
        warnings.append(f"target={target} 不在柜型三档规范名（20GP / 40HQ/HC / 40NOR）内")
    source = str(row.get("source") or "manual").strip()
    if source not in ALIAS_SPACES:
        source = "manual"
    exists = conn.execute(
        "SELECT evidence_count, source FROM alias_scope WHERE alias=? AND kind=? AND scope_group=?",
        (alias, kind, scope_group)
    ).fetchone()
    if exists:
        # 保留 evidence_count；source 允许被覆盖成 manual（人工修改后不再算 seed）
        conn.execute(
            "UPDATE alias_scope SET target=?, note=?, source=?, created_at=? "
            "WHERE alias=? AND kind=? AND scope_group=?",
            (target, note, source, now_cn(), alias, kind, scope_group)
        )
        action = "updated"
    else:
        conn.execute(
            "INSERT INTO alias_scope (alias, kind, target, scope_group, evidence_count, source, note, created_at) "
            "VALUES (?, ?, ?, ?, 0, ?, ?, ?)",
            (alias, kind, target, scope_group, source, note, now_cn())
        )
        action = "created"
    conn.commit()
    invalidate_master()
    return {"ok": True, "action": action,
            "key": {"alias": alias, "kind": kind, "scope_group": scope_group},
            "warnings": warnings}


def alias_scope_delete(conn: sqlite3.Connection, alias: str, kind: str, scope_group: str) -> Dict[str, Any]:
    cur = conn.execute(
        "DELETE FROM alias_scope WHERE alias=? AND kind=? AND scope_group=?",
        (alias, kind, scope_group or "*")
    )
    conn.commit()
    invalidate_master()
    return {"ok": bool(cur.rowcount), "deleted": cur.rowcount,
            "key": {"alias": alias, "kind": kind, "scope_group": scope_group}}


def alias_scope_list(conn: sqlite3.Connection, q: Optional[str] = None,
                     group: Optional[str] = None, kind: Optional[str] = None) -> List[Dict[str, Any]]:
    sql = "SELECT alias, kind, target, scope_group, evidence_count, source, note, created_at FROM alias_scope WHERE 1=1"
    args: List[Any] = []
    if q:
        sql += " AND (alias LIKE ? OR target LIKE ?)"
        args += [f"%{q}%", f"%{q}%"]
    if group:
        sql += " AND scope_group = ?"
        args.append(group)
    if kind:
        sql += " AND kind = ?"
        args.append(kind)
    sql += " ORDER BY scope_group, kind, alias"
    return [dict(r) for r in conn.execute(sql, args)]


# ---------------- P2 三模式灰度：影子重放（不落 pending_records，只落 pending_shadow 看 diff） ----------------

SHADOW_KEY_FIELDS = ("目的港", "起运港", "船司", "柜型", "舱位类型", "航线")


def _shadow_diff(main_records: Any, shadow_records: Any) -> str:
    """比对主与影子在关键字段上的差异。主/影都归一到 list of dict：
    主 resolved 可能是 {字段:值}（单行）或 [{字段:值}, ...]（多行）
    影 resolved 是 [validate_record(i).resolved, ...]
    """
    def _norm(x: Any) -> List[Dict[str, Any]]:
        if isinstance(x, dict):
            # validate_record 返回 {ok, resolved, errors} 时也取 .resolved
            if "resolved" in x and isinstance(x["resolved"], dict):
                return [x["resolved"]]
            return [x]
        if isinstance(x, list):
            out = []
            for e in x:
                if isinstance(e, dict):
                    out.append(e["resolved"] if (isinstance(e.get("resolved"), dict)) else e)
            return out
        return []
    fa, fb = _norm(main_records), _norm(shadow_records)
    if len(fa) != len(fb):
        return f"条数不同：主 {len(fa)} vs 影 {len(fb)}"
    diffs: List[str] = []
    for i, (a, b) in enumerate(zip(fa, fb)):
        for fld in SHADOW_KEY_FIELDS:
            av, bv = a.get(fld), b.get(fld)
            if av != bv:
                diffs.append(f"#{i} {fld}: 主={av} → 影={bv}")
    if not diffs:
        return "完全一致（说明本 pending 不依赖 alias_scope）"
    return "；".join(diffs[:6]) + (f"（还有 {len(diffs)-6} 处）" if len(diffs) > 6 else "")


def shadow_replay(conn: sqlite3.Connection, master: "Master",
                  pending_ids: List[int], modes: List[str]) -> Dict[str, Any]:
    """对每个 pending_id × mode 组合跑一次，写 pending_shadow（UNIQUE 幂等）。
    mode='baseline'：直接复用主 pending 的 resolved 作为影子
    mode='no_alias'：临时清空 master.scope_idx 重跑 expand_cn + validate_record，看 alias_scope 的贡献
    """
    results = {"replayed": 0, "skipped": 0, "by_mode": {m: 0 for m in modes}}
    for pid in pending_ids:
        row = conn.execute(
            "SELECT id, kind, payload, resolved, errors, check_status FROM pending_records WHERE id=?",
            (pid,)
        ).fetchone()
        if not row:
            results["skipped"] += 1
            continue
        try:
            rec = json.loads(row["payload"])
        except Exception:
            results["skipped"] += 1
            continue
        for mode in modes:
            if mode == "baseline":
                shadow_resolved = row["resolved"] or "[]"
                shadow_errors = row["errors"] or "[]"
                diff = "复用主 pending 的 resolved（对照基线）"
            elif mode == "no_alias":
                # 浅拷贝 Master：只换 scope_idx/unmatched 引用，原共享实例不动。
                # 直接清空 master.scope_idx 会让并发请求在窗口期拿到空索引、错误归一落库（2026-09-17 审查修复）
                import copy as _copy
                shadow_master = _copy.copy(master)
                shadow_master.scope_idx = {}
                shadow_master.unmatched = []
                expanded = shadow_master.expand_cn(rec, conn=conn, allow_carrier_add=False)
                validations = [validate_record(conn, shadow_master, row["kind"], r) for r in expanded]
                shadow_resolved = json.dumps([v.get("resolved") for v in validations], ensure_ascii=False)
                shadow_errors = json.dumps([v.get("errors") for v in validations], ensure_ascii=False)
                try:
                    main_resolved_list = json.loads(row["resolved"] or "[]") if row["resolved"] else []
                except Exception:
                    main_resolved_list = []
                diff = _shadow_diff(main_resolved_list, [v.get("resolved") for v in validations])
            else:
                continue
            conn.execute(
                "INSERT OR REPLACE INTO pending_shadow "
                "(source_pending_id, mode, kind, payload, resolved, errors, diff_summary, note, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (row["id"], mode, row["kind"], row["payload"], shadow_resolved, shadow_errors, diff, "", now_cn())
            )
            results["replayed"] += 1
            results["by_mode"][mode] = results["by_mode"].get(mode, 0) + 1
    conn.commit()
    invalidate_master()
    return results


def shadow_list(conn: sqlite3.Connection, pending_id: Optional[int] = None,
                mode: Optional[str] = None, limit: int = 200) -> List[Dict[str, Any]]:
    sql = ("SELECT id, source_pending_id, mode, kind, payload, resolved, errors, diff_summary, note, created_at "
           "FROM pending_shadow WHERE 1=1")
    args: List[Any] = []
    if pending_id:
        sql += " AND source_pending_id = ?"
        args.append(int(pending_id))
    if mode:
        sql += " AND mode = ?"
        args.append(mode)
    sql += " ORDER BY id DESC LIMIT ?"
    args.append(int(limit))
    return [dict(r) for r in conn.execute(sql, args)]


def shadow_clear(conn: sqlite3.Connection, older_than: Optional[str] = None,
                 mode: Optional[str] = None) -> Dict[str, Any]:
    sql = "DELETE FROM pending_shadow WHERE 1=1"
    args: List[Any] = []
    if older_than:
        sql += " AND created_at < ?"
        args.append(older_than)
    if mode:
        sql += " AND mode = ?"
        args.append(mode)
    cur = conn.execute(sql, args)
    conn.commit()
    return {"ok": True, "deleted": cur.rowcount}


# ---------------- 规则候选（rule_candidates）：difflib 扫 + 人工批准写回 ----------------

def candidates_upsert(conn: sqlite3.Connection, cand: Dict[str, Any]) -> Dict[str, Any]:
    """按 UNIQUE (scope_kind, scope_key, alias, kind, suggested_target) upsert。
    新出现：INSERT；已存在（含 rejected）：evidence_count += 1、evidence_refs 追加、confidence 取较大值。
    rejected 状态不自动复活（避免反复推荐同一个已被人工否掉的规则）；除非显式带 reactivate=True。
    返回 {action: created/updated/skipped_rejected, id}。"""
    scope_kind = str(cand.get("scope_kind") or "").strip()
    scope_key = str(cand.get("scope_key") or "").strip()
    target_layer = str(cand.get("target_layer") or "alias_scope").strip()
    pattern_type = str(cand.get("pattern_type") or "alias").strip()
    alias = str(cand.get("alias") or "").strip()
    kind = str(cand.get("kind") or "").strip()
    suggested_target = str(cand.get("suggested_target") or "").strip()
    reason = str(cand.get("reason") or "").strip()
    confidence = float(cand.get("confidence") or 0)
    evidence_refs = cand.get("evidence_refs") or []
    if not isinstance(evidence_refs, list):
        evidence_refs = [str(evidence_refs)]
    evidence_refs_json = json.dumps(evidence_refs, ensure_ascii=False)
    source = str(cand.get("source") or "difflib_scan").strip()
    if not all([scope_kind, scope_key, alias, kind, suggested_target]):
        raise ValueError("candidates_upsert 必填：scope_kind/scope_key/alias/kind/suggested_target")
    row = conn.execute(
        "SELECT id, status, evidence_count, evidence_refs, confidence FROM rule_candidates "
        "WHERE scope_kind=? AND scope_key=? AND alias=? AND kind=? AND suggested_target=?",
        (scope_kind, scope_key, alias, kind, suggested_target)
    ).fetchone()
    if row:
        if row["status"] == "rejected" and not cand.get("reactivate"):
            return {"action": "skipped_rejected", "id": row["id"]}
        try:
            old_refs = json.loads(row["evidence_refs"] or "[]")
        except Exception:
            old_refs = []
        merged_refs = list(dict.fromkeys(old_refs + evidence_refs))  # 去重保序
        # evidence_count = 去重后证据数（扫描是全量重扫，幂等不虚增）
        new_count = max(len(merged_refs), 1)
        new_conf = max(float(row["confidence"]), confidence)
        new_status = "pending" if row["status"] == "rejected" and cand.get("reactivate") else row["status"]
        conn.execute(
            "UPDATE rule_candidates SET evidence_count=?, evidence_refs=?, confidence=?, "
            "reason=COALESCE(NULLIF(?, ''), reason), status=? WHERE id=?",
            (new_count, json.dumps(merged_refs, ensure_ascii=False), new_conf, reason, new_status, row["id"])
        )
        conn.commit()
        return {"action": "updated", "id": row["id"], "evidence_count": new_count}
    cur = conn.execute(
        "INSERT INTO rule_candidates "
        "(scope_kind, scope_key, target_layer, pattern_type, alias, kind, suggested_target, "
        " reason, confidence, evidence_count, evidence_refs, source, status, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?)",
        (scope_kind, scope_key, target_layer, pattern_type, alias, kind, suggested_target,
         reason, confidence, max(len(evidence_refs), 1), evidence_refs_json, source, now_cn())
    )
    conn.commit()
    return {"action": "created", "id": cur.lastrowid}


def candidates_list(conn: sqlite3.Connection, status: Optional[str] = "pending",
                    source: Optional[str] = None, limit: int = 200) -> List[Dict[str, Any]]:
    sql = ("SELECT id, scope_kind, scope_key, target_layer, pattern_type, alias, kind, "
           "suggested_target, reason, confidence, evidence_count, evidence_refs, source, "
           "status, reviewed_by, reviewed_at, reviewer_note, created_at FROM rule_candidates WHERE 1=1")
    args: List[Any] = []
    if status:
        sql += " AND status = ?"
        args.append(status)
    if source:
        sql += " AND source = ?"
        args.append(source)
    sql += " ORDER BY confidence DESC, evidence_count DESC, id DESC LIMIT ?"
    args.append(int(limit))
    return [dict(r) for r in conn.execute(sql, args)]


def candidates_approve(conn: sqlite3.Connection, cand_id: int, reviewer: str,
                       override_scope_group: Optional[str] = None) -> Dict[str, Any]:
    """批准一条候选 → 按 target_layer 落到实际表。
    target_layer='alias_scope' → 拆 scope_key 取群名调 alias_scope_save；
      - scope_kind='group+column' scope_key='群名|列名' → alias_scope.scope_group='群名'
      - 其他更窄作用域（sender+carrier+group 等）当前只落 group 层，其余信息进 note（后续扩展 sender 级别消费）
    target_layer='master_aliases' → 读 ports/carriers/routes 对应实体 aliases 字段，追加 alias；
      scope_kind 必须是 'group' 或 'global' 时才走这条路径（否则 alias_scope 更贴切）
    """
    row = conn.execute("SELECT * FROM rule_candidates WHERE id=?", (cand_id,)).fetchone()
    if not row:
        raise ValueError(f"候选 {cand_id} 不存在")
    if row["status"] != "pending":
        return {"ok": False, "error": f"候选状态已是 {row['status']}，不能重复批准"}
    cand = dict(row)
    layer = cand["target_layer"]
    scope_key = cand["scope_key"]
    parts = [p.strip() for p in scope_key.split("|")]
    if layer == "alias_scope":
        # scope_key 末段一般是群名；若是 'a|b|群名'（sender+carrier+group）也取末段作群
        group_name = parts[-1]
        if group_name in ("*", ""):
            raise ValueError("alias_scope 候选的 scope_key 必须含具体群名")
        if override_scope_group:
            group_name = override_scope_group
        # note 里保留完整 scope 与来源，方便后续扩展到 sender 级别消费
        extra = cand["reason"] or ""
        save_row = {
            "alias": cand["alias"],
            "kind": cand["kind"],
            "target": cand["suggested_target"],
            "scope_group": group_name,
            "source": "candidate_approved",
            "note": f"候选#{cand_id} 批准 by {reviewer}｜scope_kind={cand['scope_kind']}｜{extra}".strip(),
        }
        res = alias_scope_save(conn, save_row)
        # alias_scope_save 可能抛 ValueError（scope_group 不在 chat_groups 会警告但保留、kind/target 违规会 raise）
        action_res = res
    elif layer == "master_aliases":
        # 只在 kind 有对应主数据表时支持（pol/pod→ports，carrier→carriers，route→routes）
        pk_map = {"pol": ("ports", "name"), "pod": ("ports", "name"),
                  "carrier": ("carriers", "code"), "route": ("routes", "name")}
        if cand["kind"] not in pk_map:
            raise ValueError(f"master_aliases 暂不支持 kind={cand['kind']}，请走 alias_scope")
        table, pk_col = pk_map[cand["kind"]]
        entity = conn.execute(
            f"SELECT * FROM {table} WHERE {pk_col}=?", (cand["suggested_target"],)
        ).fetchone()
        if not entity:
            raise ValueError(f"主数据 {table} 里找不到 {cand['suggested_target']}")
        ent = dict(entity)
        old_aliases = str(ent.get("aliases") or "").strip()
        alias_list = [a.strip() for a in old_aliases.split(",") if a.strip()]
        if cand["alias"] in alias_list:
            action_res = {"ok": True, "action": "already_present", "table": table, "key": ent[pk_col]}
        else:
            alias_list.append(cand["alias"])
            ent["aliases"] = ",".join(alias_list)
            action_res = master_save(conn, table, ent)
    elif layer == "field_default":
        # alias 位存字段名、suggested_target 是默认值；批准后进 field_defaults（审核界面提示用，不自动改数据）
        conn.execute(
            "INSERT INTO field_defaults (scope_key, field, default_value, evidence_count, source, status, note, created_at) "
            "VALUES (?, ?, ?, ?, 'candidate_approved', 'active', ?, ?) "
            "ON CONFLICT (scope_key, field, default_value) DO UPDATE SET "
            "evidence_count=excluded.evidence_count, status='active', note=excluded.note",
            (cand["scope_key"], cand["alias"], cand["suggested_target"],
             int(cand["evidence_count"] or 1),
             f"候选#{cand_id} 批准 by {reviewer}｜{cand['reason'] or ''}".strip(), now_cn())
        )
        action_res = {"ok": True, "action": "field_default_saved",
                      "scope_key": cand["scope_key"], "field": cand["alias"],
                      "default_value": cand["suggested_target"]}
    else:
        raise ValueError(f"未支持的 target_layer={layer}")

    conn.execute(
        "UPDATE rule_candidates SET status='approved', reviewed_by=?, reviewed_at=? WHERE id=?",
        (reviewer, now_cn(), cand_id)
    )
    conn.commit()
    invalidate_master()
    return {"ok": True, "candidate_id": cand_id, "layer_action": action_res,
            "candidate": {"alias": cand["alias"], "kind": cand["kind"],
                          "scope_key": cand["scope_key"], "target_layer": layer,
                          "suggested_target": cand["suggested_target"]}}


def candidates_reject(conn: sqlite3.Connection, cand_id: int, reviewer: str,
                      note: str = "") -> Dict[str, Any]:
    row = conn.execute("SELECT status FROM rule_candidates WHERE id=?", (cand_id,)).fetchone()
    if not row:
        raise ValueError(f"候选 {cand_id} 不存在")
    if row["status"] != "pending":
        return {"ok": False, "error": f"候选状态已是 {row['status']}"}
    conn.execute(
        "UPDATE rule_candidates SET status='rejected', reviewed_by=?, reviewed_at=?, reviewer_note=? WHERE id=?",
        (reviewer, now_cn(), note, cand_id)
    )
    conn.commit()
    return {"ok": True, "candidate_id": cand_id}


_CANDIDATE_FIELD_TO_KIND = {
    "起运港": "pol", "目的港": "pod", "船司": "carrier", "航线": "route",
    "舱位类型": "space_type", "柜型": "ctype",
}
_CANDIDATE_SKIP_MSG = ("含说明文字", "占位符", "只去", "多港", "未注明", "待定", "未填")
_CANDIDATE_HIT_MSG = ("不在港口", "不在主数据", "不在", "找不到", "未登记")


def scan_failed_for_candidates(conn: sqlite3.Connection, limit: int = 2000,
                                cutoff: float = 0.75) -> Dict[str, Any]:
    """扫 pending_records 里 check_status='failed' 且 review_status='awaiting' 的行，
    对 errors 里"未命中主数据"类字段值用 difflib 找相近规范名 → 生成候选。
    跳过"含说明文字/占位符"类（那些是原文脏、不是未登记别名）。
    """
    import difflib
    rows = list(conn.execute(
        "SELECT id, kind, payload, errors FROM pending_records "
        "WHERE check_status='failed' AND review_status='awaiting' "
        "ORDER BY id DESC LIMIT ?", (int(limit),)
    ))
    pool_ports = [r[0] for r in conn.execute("SELECT name FROM ports WHERE active=1")]
    pool_pgs = [r[0] for r in conn.execute("SELECT name FROM port_groups WHERE active=1 AND kind='pod'")]
    pool_carriers = [r[0] for r in conn.execute("SELECT code FROM carriers WHERE active=1")]
    pool_routes = [r[0] for r in conn.execute("SELECT name FROM routes WHERE active=1")]
    pools = {"pol": pool_ports, "pod": pool_ports + pool_pgs,
             "carrier": pool_carriers, "route": pool_routes}
    stats = {"scanned": len(rows), "fields_examined": 0,
             "candidates_created": 0, "candidates_updated": 0, "skipped_rejected": 0,
             "skipped_junk": 0, "skipped_hit": 0, "skipped_no_match": 0}
    for row in rows:
        try:
            errors = json.loads(row["errors"] or "[]")
            payload = json.loads(row["payload"] or "{}")
        except Exception:
            continue
        group = payload["source_group"] if "source_group" in payload else (payload.get("来源群") or "未登记群")
        for err in errors:
            field = err.get("field")
            raw = str(err.get("input") or "").strip()
            msg = str(err.get("msg") or "")
            kind = _CANDIDATE_FIELD_TO_KIND.get(field)
            if not (field and raw and kind):
                continue
            if any(k in msg for k in _CANDIDATE_SKIP_MSG):
                stats["skipped_junk"] += 1
                continue
            if not any(k in msg for k in _CANDIDATE_HIT_MSG):
                stats["skipped_junk"] += 1
                continue
            stats["fields_examined"] += 1
            # 已存在同 alias+kind+群 的 alias_scope 行 → 不重复生成候选
            hit = conn.execute(
                "SELECT 1 FROM alias_scope WHERE alias=? AND kind=? AND scope_group=? LIMIT 1",
                (raw, kind, group)
            ).fetchone()
            if hit:
                stats["skipped_hit"] += 1
                continue
            pool = pools.get(kind, [])
            if not pool:
                stats["skipped_no_match"] += 1
                continue
            matches = difflib.get_close_matches(raw, pool, n=1, cutoff=cutoff)
            if not matches:
                stats["skipped_no_match"] += 1
                continue
            best = matches[0]
            score = difflib.SequenceMatcher(None, raw.lower(), best.lower()).ratio()
            res = candidates_upsert(conn, {
                "scope_kind": "group+column",
                "scope_key": f"{kind}|{group}",  # 约定：群名总是末段
                "target_layer": "alias_scope",
                "pattern_type": "correction",
                "alias": raw,
                "kind": kind,
                "suggested_target": best,
                "reason": f"difflib 相似度 {score:.2f}，pending#{row['id']} errors 里 {field}=\"{raw}\" 未命中主数据",
                "confidence": round(score, 3),
                "evidence_refs": [row["id"]],
                "source": "difflib_scan",
            })
            if res["action"] == "created":
                stats["candidates_created"] += 1
            elif res["action"] == "updated":
                stats["candidates_updated"] += 1
            else:
                stats["skipped_rejected"] += 1
    return {"ok": True, **stats}


def scan_audit_corrections(conn: sqlite3.Connection, min_count: int = 3,
                           min_span_days: int = 2) -> Dict[str, Any]:
    """P1 审核动作反向沉淀：扫 pending_records.edit_log，按 (群|发送人|船司|字段|新值) 三维+字段聚合。
    同组 ≥min_count 次同向修改 且 时间跨度 ≥min_span_days 天 → 生成候选：
      - old 非空且字段在 _OBSERVE_FIELDS → 值替换类，target_layer='alias_scope'
      - old 全空 → 补默认值类，target_layer='field_default'（批准后只做审核提示，不自动改数据）
    scope_kind 固定 'sender+carrier+group'（单靠群不精准），scope_key=f"发送人|船司|群名"（群名末段约定）。
    """
    from collections import defaultdict
    from datetime import datetime as _dt
    groups: Dict[tuple, List[tuple]] = defaultdict(list)
    rows = conn.execute(
        "SELECT id, payload, edit_log FROM pending_records "
        "WHERE edit_log IS NOT NULL AND edit_log != '' AND edit_log != '[]'"
    )
    for row in rows:
        try:
            payload = json.loads(row["payload"] or "{}")
            log_list = json.loads(row["edit_log"] or "[]")
        except Exception:
            continue
        group = str(payload.get("来源群") or payload.get("source_group") or "").strip() or "未登记群"
        sender = str(payload.get("发送人") or payload.get("sender") or "").strip() or "未知发送人"
        carrier = str(payload.get("船司") or "").strip() or "未知船司"
        for e in log_list:
            if e.get("action") not in ("update", "approve_diff"):
                continue
            field = str(e.get("field") or "").strip()
            if not field or field == "消息原文":
                continue
            new = e.get("new")
            if new in (None, ""):
                continue
            groups[(group, sender, carrier, field, str(new))].append(
                (str(e.get("ts") or ""), e.get("old"), row["id"]))
    stats = {"keys_examined": len(groups), "candidates_created": 0, "candidates_updated": 0,
             "skipped_rejected": 0, "below_threshold": 0}
    for (group, sender, carrier, field, new), hits in groups.items():
        if len(hits) < min_count:
            stats["below_threshold"] += 1
            continue
        ts_list = []
        for ts, _, _ in hits:
            try:
                ts_list.append(_dt.strptime(ts[:10], "%Y-%m-%d"))
            except Exception:
                continue
        span_days = (max(ts_list) - min(ts_list)).days if len(ts_list) >= 2 else 0
        if span_days < min_span_days:
            stats["below_threshold"] += 1
            continue
        olds = [str(o) for _, o, _ in hits if o not in (None, "")]
        kind = _OBSERVE_FIELDS.get(field)
        pids = sorted({pid for _, _, pid in hits})[:20]
        scope_key = f"{sender}|{carrier}|{group}"
        conf = round(min(1.0, len(hits) / 10) * min(1.0, max(span_days, 1) / 7), 3)
        if kind and olds:
            old_top = max(set(olds), key=olds.count)
            cand = {
                "scope_kind": "sender+carrier+group",
                "scope_key": scope_key,
                "target_layer": "alias_scope",
                "pattern_type": "alias",
                "alias": old_top,
                "kind": kind,
                "suggested_target": new,
                "reason": f"审核反复把「{field}」从「{old_top}」改成「{new}」共 {len(hits)} 次（跨度 {span_days} 天）",
                "confidence": conf,
                "evidence_refs": pids,
                "source": "audit_correction",
            }
        else:
            cand = {
                "scope_kind": "sender+carrier+group",
                "scope_key": scope_key,
                "target_layer": "field_default",
                "pattern_type": "default",
                "alias": field,
                "kind": kind or "field",
                "suggested_target": new,
                "reason": f"审核反复给「{field}」补值「{new}」共 {len(hits)} 次（跨度 {span_days} 天）",
                "confidence": conf,
                "evidence_refs": pids,
                "source": "audit_correction",
            }
        res = candidates_upsert(conn, cand)
        if res["action"] == "created":
            stats["candidates_created"] += 1
        elif res["action"] == "updated":
            stats["candidates_updated"] += 1
        else:
            stats["skipped_rejected"] += 1
    return {"ok": True, **stats}


def audit_hints(conn: sqlite3.Connection, group: Optional[str] = None,
                sender: Optional[str] = None, carrier: Optional[str] = None,
                limit: int = 500) -> Dict[str, Any]:
    """P2 审核提示：给定三维（可缺省）实时聚合 edit_log → 各字段被改统计；
    再查 field_defaults 里该 scope 生效的默认值（精确 scope_key + 群名后缀宽松匹配）。
    '能算出来就别存'：统计不落表，每次实时算；MVP 限最近 limit 条有 edit_log 的 pending。"""
    field_stats: Dict[str, Dict[str, Any]] = {}
    rows = conn.execute(
        "SELECT id, payload, edit_log FROM pending_records "
        "WHERE edit_log IS NOT NULL AND edit_log NOT IN ('', '[]') "
        "ORDER BY id DESC LIMIT ?", (int(limit),)
    )
    for row in rows:
        try:
            payload = json.loads(row["payload"] or "{}")
            log_list = json.loads(row["edit_log"] or "[]")
        except Exception:
            continue
        g = str(payload.get("来源群") or payload.get("source_group") or "").strip()
        s = str(payload.get("发送人") or payload.get("sender") or "").strip()
        c = str(payload.get("船司") or "").strip()
        if group and g != group:
            continue
        if sender and s != sender:
            continue
        if carrier and c != carrier:
            continue
        for e in log_list:
            if e.get("action") not in ("update", "approve_diff"):
                continue
            f = str(e.get("field") or "").strip()
            new = e.get("new")
            if not f or f == "消息原文" or new in (None, ""):
                continue
            st = field_stats.setdefault(f, {"count": 0, "values": {}, "last_ts": ""})
            st["count"] += 1
            st["values"][str(new)] = st["values"].get(str(new), 0) + 1
            ts = str(e.get("ts") or "")
            if ts > st["last_ts"]:
                st["last_ts"] = ts
    # 转 top 值列表
    for f, st in field_stats.items():
        top = sorted(st["values"].items(), key=lambda x: -x[1])[:3]
        st["top"] = [{"value": v, "n": n} for v, n in top]
        del st["values"]
    # field_defaults：精确 scope_key + 群名后缀宽松匹配
    defaults: List[Dict[str, Any]] = []
    if group or sender or carrier:
        exact = f"{sender or ''}|{carrier or ''}|{group or ''}"
        q = ("SELECT scope_key, field, default_value, evidence_count, source, note "
             "FROM field_defaults WHERE status='active' AND (scope_key = ?")
        args: List[Any] = [exact]
        if group:
            q += " OR scope_key LIKE ?"
            args.append(f"%|{group}")
        q += ") ORDER BY evidence_count DESC LIMIT 20"
        for r in conn.execute(q, args):
            defaults.append(dict(r))
    return {"ok": True, "field_stats": field_stats, "defaults": defaults}


def sync_rows(conn: sqlite3.Connection, table: str, rows: List[Dict[str, Any]]) -> int:
    """批量导入英文列名行：主数据按主键 merge，业务表按 content_key merge。"""
    pk: Optional[str] = MASTER_TABLES.get(table, {}).get("pk")
    if pk is None:
        pk = {"operators": "name", "chat_groups": "name", "pending_records": "id"}.get(table)
    if pk is None and any(s["table"] == table for s in TABLES.values()):
        pk = "content_key"
    if pk is None:
        raise ValueError(f"unknown table: {table}")
    cols = [r[1] for r in conn.execute(f"PRAGMA table_info({table})") if r[1] != "id"]
    ph = ",".join("?" * len(cols))
    upd = ",".join(f"{c}=excluded.{c}" for c in cols if c != pk)
    sql = (f'INSERT INTO {table} ({",".join(cols)}) VALUES ({ph}) '
           f'ON CONFLICT({pk}) DO UPDATE SET {upd}')
    for r in rows:
        conn.execute(sql, [r.get(c) for c in cols])
    conn.commit()
    if table in ("freight_rates", "space_records"):     # 批量导入也走"写入即登记"，只补不覆盖
        _register_pairs(conn, [{"source_group": r.get("source_group"),
                                "sender": r.get("sender")} for r in rows])
    if table in MASTER_TABLES:
        invalidate_master()      # 主数据一改，归一索引必须重建
    return len(rows)


def export_all(conn: sqlite3.Connection) -> Dict[str, Any]:
    """整库快照（备份 / 迁移用）。"""
    out: Dict[str, Any] = {"ok": True, "exported_at": now_cn()}
    tables = list(DDL.keys())
    for t in tables:
        out[t] = [dict(r) for r in conn.execute(f"SELECT * FROM {t}")]
    return out


def load_snapshot(conn: sqlite3.Connection, snap: Dict[str, Any]) -> str:
    """导入整库快照，按主键 merge，可重复跑。

    主数据表额外做"对齐"：改过名的旧行留着会让别名索引产生歧义
    （实测残留一条旧 Manzanillo 就把「曼萨尼约」卡成多义、录入端不再归一）。
    """
    parts = []
    for t in DDL:
        rows = [r for r in (snap.get(t) or []) if r]
        if not rows:
            parts.append(f"{t}=0")
            continue
        parts.append(f"{t}={sync_rows(conn, t, rows)}")
        if t in MASTER_TABLES:
            pk = MASTER_TABLES[t]["pk"]
            keep = {r[pk] for r in rows if r.get(pk)}
            gone = conn.execute(f"DELETE FROM {t} WHERE {pk} NOT IN ({','.join('?' * len(keep))})",
                                 list(keep)).rowcount
            if gone:
                parts[-1] += f"(清旧{gone})"
    conn.commit()
    invalidate_master()
    return " ".join(parts)
