# -*- coding: utf-8 -*-
"""人工维护台账（局域网版）数据层：完全自包含的 SQLite 库 + 主数据归一 + 录入与留痕。

本文件不 import 任何其它看板的代码，库也是独立的 freight_manual.db。
业务口径：港口/船司/航线走主数据归一（规范名为英文官方港名），柜型四档写入即收敛，
价格类必须纯整数，人工录入的时间由服务端盖（客户端传什么都不认），
内容指纹幂等、改价改期走留痕（追加新行 + 旧行标已被覆盖 + 现算变动摘要），
目的港/起运港支持用户自维护的「港群」（库里存群名，查询时双向展开到成员港）。
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import sqlite3
import time
import uuid
import unicodedata
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Lock
from typing import Any, Dict, Iterable, List, Optional, Tuple

from core.contracts import (SALES_VALID_SINCE_PARAM, PORT_ROUTES_FIELD,
                            PORT_ROUTES_COMPACT_FIELD, PORT_ROUTES_TABLE, OPS_MESSAGE_ID_PARAM, OPS_ID_QUERY_PARAM, SOURCE_MESSAGE_ID_PREFIX)
from core.contracts import (PENDING_SOURCE_MAX_GROUPS, PendingSourceListResult,
                            PendingBulkApprovalResult, QUOTE_VARIANT_FIELDS,
                            SPACE_DESTINATION_SCOPE_FIELDS)
from core.contracts import ACCOUNT_ROLES, ROLE_ADMIN, ROLE_OPERATOR
from services.port_routes import route_tokens, unique_common_route
from services.identity_query import normalize_identity_query
from services.sales_sort import sales_order_sql, sort_sales_groups, parse_sales_sort
from services.container_features import container_filter_clause, has_45_high_cube
from core.contracts import SALES_45_HIGH_CUBE_ALIASES
from core.publisher_contracts import PUBLISHER_DDL, PUBLISHER_TABLES, SOURCE_FIELDS
from ops.publisher_identity import resolve as resolve_publisher, ensure_schema as ensure_publisher_schema, create_publisher, annotate as annotate_publishers, canonical as canonical_publisher
from core.notice_contracts import NOTICE_DDL, NOTICE_SCOPE_COLUMNS
from core.notification_contracts import NOTIFICATION_DDL, NOTIFICATION_TABLE
from services.notices import notice_form

HERE = Path(__file__).resolve().parent
DEFAULT_DB = HERE / "freight_manual.db"

# ---------------- 字段映射与表定义 ----------------

RATES_MAP: Dict[str, str] = {
    **QUOTE_VARIANT_FIELDS, **SOURCE_FIELDS,
    "起运港": "pol", "目的港": "pod", "航线": "route", "船司": "carrier",
    "柜型": "container_type", "海运费USD": "price_usd", "有效期船期": "validity_raw",
    "船名航次": "vessel_voyage",
    "目免": "free_days", "亏舱费": "dead_freight", "备注": "remark",
    "来源群": "source_group", "发送人": "sender", "消息时间": "message_time",
    "消息原文": "message_text", "运价图片": "image_url", "有效期起": "valid_from",
    "有效期止": "valid_to", "船期ETD": "etd", "被覆盖消息时间": "overridden_time",
    "记录状态": "status", "消息ID": "mid", "图片名": "image_file", "T/T航程": "transit_days",
    "AI判断": "ai_flag",
}
SPACE_MAP: Dict[str, str] = {
    **QUOTE_VARIANT_FIELDS, **SOURCE_FIELDS,
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
KEY_EXCLUDE = set(SOURCE_FIELDS.values()) | {"image_url", "image_file", "status", "overridden_time", "synced_at", "ai_flag"}
UPDATE_BLOCKED = set(SOURCE_FIELDS.values()) | {"image_url", "image_file", "synced_at"}
INT_COLS: Dict[str, Dict[str, str]] = {
    "rates": {"price_usd": "海运费USD", "dead_freight": "亏舱费",
              "free_days": "Freetime", "transit_days": "T/T航程"},
    "space": {"price_usd": "价格USD", "transit_days": "T/T航程", "box_qty": "箱量"},
}
# 兼容既有前端状态文案；它们只表示 record_type 对应的当前版本，不再代表物理表。
DEFAULT_STATUS = {"rates": "当前生效", "space": "当前有效"}
# 「同一条报价/舱位」的身份键（判“是不是更新”用）：产品维度，【不含消息原文】。
# 一条转发原文常一次拆出多柜型/多ETD/多船，若拿“原文”当身份，同港不同柜型会互相误判成更新而彼此覆盖→
# 出现假“已被覆盖/无有效价”。故身份只认产品字段；完全重复仍由 content_key 指纹幂等。
IDENTITY_COLS: Dict[str, List[str]] = {
    "rates": ["carrier", "pol", "pod", "container_type", "vessel_voyage", "etd", *QUOTE_VARIANT_FIELDS.values()],
    "space": ["carrier", "pol", "pod", "container_type", "vessel_voyage", "etd", *QUOTE_VARIANT_FIELDS.values()],
}
COVERED = "已被覆盖"
FILTERS = {OPS_MESSAGE_ID_PARAM: "mid", "entry_id": "entry_id", "pol": "pol", "pod": "pod", "route": "route", "carrier": "carrier",
           "ctype": "container_type", "group": "source_group", "status": "status",
           "stype": "space_type", "origin": "origin",
           **{col: col for col in QUOTE_VARIANT_FIELDS.values()}}
GROUP_FIELDS = ("entry_id", "publisher_id", "publisher_bound", "carrier", "route", "pol", "pod", "etd", "message_time", "sender", "source_group",
                "validity_raw", "valid_from", "valid_to", "status", "remark", "message_text",
                "image_url", "image_file", "free_days", "dead_freight", "transit_days", "synced_at",
                "content_key", "supersedes", "ai_flag", "service_name", "destination_terminal", *QUOTE_VARIANT_FIELDS.values())

# 标准化闸门：核心字段只能落在主数据预设值上（起运港/目的港/船司/航线），杜绝脏值污染。
CORE_MATCH = ("起运港", "目的港", "船司", "航线")
# "不得为空"分库：运价要目的港/柜型/价格；舱位还须满足下方目的范围契约——
# 现舱消息常常只报"某港还有几高柜"，不给船名航次，2026-09-18 按业务实况放开（占位符仍按未填打回，见 PLACEHOLDER）。
REQUIRED_BY_KIND: Dict[str, Tuple[str, ...]] = {
    "rates": ("起运港", "目的港", "船司", "柜型", "海运费USD"),
    "space": ("起运港", "船司"),
}
VALIDITY_REQUIRED_KINDS = {"rates", "space"}   # 运价、舱位都强制有效期非空

TABLES: Dict[str, Dict[str, Any]] = {
    "rates": {"table": "freight_items", "map": RATES_MAP,
              "key": sorted(({c for c in RATES_MAP.values()} | {c for c in SPACE_MAP.values()}) - KEY_EXCLUDE)},
    "space": {"table": "freight_items", "map": SPACE_MAP,
              "key": sorted(({c for c in RATES_MAP.values()} | {c for c in SPACE_MAP.values()}) - KEY_EXCLUDE)},
}
ITEM_CN_MAP: Dict[str, str] = {**RATES_MAP, **SPACE_MAP}
ITEM_COLUMNS = set(ITEM_CN_MAP.values())

def new_entry_id() -> str:
    return "frt_" + uuid.uuid4().hex

def legacy_entry_id(table: str, row_id: int) -> str:
    return "frt_" + hashlib.sha256(f"{table}:{row_id}".encode("utf-8")).hexdigest()[:32]

DDL: Dict[str, str] = {
    # 统一业务事实表：运价/舱位是 record_type 标签，字段由同一行管理。
    "freight_items": """CREATE TABLE IF NOT EXISTS freight_items (
        id INTEGER PRIMARY KEY AUTOINCREMENT, entry_id TEXT NOT NULL UNIQUE, content_key TEXT NOT NULL UNIQUE,
        record_type TEXT NOT NULL DEFAULT 'unclassified',
        pol TEXT, pod TEXT, route TEXT, carrier TEXT, container_type TEXT, price_usd INTEGER,
        vessel_voyage TEXT, cutoff_raw TEXT, box_desc TEXT, box_qty INTEGER, space_type TEXT,
        validity_raw TEXT, valid_from TEXT, valid_to TEXT, etd TEXT, free_days INTEGER,
        dead_freight INTEGER, transit_days INTEGER, remark TEXT, source_group TEXT, sender TEXT, publisher_id TEXT,
        source_group_id TEXT, source_sender_id TEXT, source_sender_open_id TEXT,
        message_time TEXT, message_text TEXT, image_url TEXT, status TEXT, overridden_time TEXT,
        synced_at TEXT, mid TEXT, image_file TEXT, supersedes TEXT,
        origin TEXT NOT NULL DEFAULT 'manual', ai_flag TEXT, service_name TEXT, destination_terminal TEXT,
        CHECK (record_type IN ('rates','space','unclassified')),
        CHECK (price_usd IS NULL OR (typeof(price_usd) = 'integer' AND price_usd >= 0)),
        CHECK (free_days IS NULL OR (typeof(free_days) = 'integer' AND free_days >= 0)),
        CHECK (dead_freight IS NULL OR (typeof(dead_freight) = 'integer' AND dead_freight >= 0)),
        CHECK (box_qty IS NULL OR (typeof(box_qty) = 'integer' AND box_qty >= 0)),
        CHECK (transit_days IS NULL OR (typeof(transit_days) = 'integer' AND transit_days >= 0)),
        CHECK (valid_from IS NULL OR valid_from = '' OR valid_from GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'),
        CHECK (valid_to IS NULL OR valid_to = '' OR valid_to GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'))""",
    "operators": """CREATE TABLE IF NOT EXISTS operators (
        id INTEGER PRIMARY KEY AUTOINCREMENT, user_id TEXT UNIQUE, name TEXT NOT NULL UNIQUE, title TEXT,
        routes TEXT, groups TEXT, match_mode TEXT DEFAULT 'any', active INTEGER DEFAULT 1,
        role TEXT NOT NULL DEFAULT 'operator', password_hash TEXT,
        password_changed_at TEXT, note TEXT, created_at TEXT,
        display_name TEXT, preferences_json TEXT, publisher_name TEXT, publisher_id TEXT,
        CHECK (role IN ('admin','operator','sales')))""",
    "auth_sessions": """CREATE TABLE IF NOT EXISTS auth_sessions (
        token_hash TEXT PRIMARY KEY, operator_name TEXT NOT NULL, client_ip TEXT NOT NULL,
        expires_at INTEGER NOT NULL, created_at TEXT NOT NULL)""",
    "account_recovery_requests": """CREATE TABLE IF NOT EXISTS account_recovery_requests (
        name TEXT PRIMARY KEY,
        status TEXT NOT NULL DEFAULT 'requested', requested_at TEXT NOT NULL,
        resolved_at TEXT, resolved_by TEXT,
        CHECK (status IN ('requested','resolved')))""",
    "chat_groups": """CREATE TABLE IF NOT EXISTS chat_groups (
        id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL UNIQUE, cid TEXT,
        default_pol TEXT, region TEXT, owner TEXT, active INTEGER DEFAULT 1, note TEXT,
        source TEXT DEFAULT 'manual',
        created_at TEXT)""",
    "sales_group_visits": """CREATE TABLE IF NOT EXISTS sales_group_visits (
        user_id TEXT NOT NULL, source_group TEXT NOT NULL,
        click_count INTEGER NOT NULL DEFAULT 0 CHECK (click_count >= 0),
        last_clicked_at TEXT NOT NULL,
        PRIMARY KEY (user_id, source_group))""",
    "ports": """CREATE TABLE IF NOT EXISTS ports (
        name TEXT PRIMARY KEY, country TEXT, city TEXT, city_pinyin TEXT, name_en TEXT,
        name_cn TEXT, aliases TEXT, kind TEXT DEFAULT 'pod', route TEXT, source TEXT, locode_ref TEXT,
        active INTEGER DEFAULT 1, note TEXT, created_at TEXT)""",
    "carriers": """CREATE TABLE IF NOT EXISTS carriers (
        code TEXT PRIMARY KEY, name_en TEXT, name_cn TEXT, scac TEXT, sort INTEGER DEFAULT 900,
        aliases TEXT, website_url TEXT, active INTEGER DEFAULT 1, note TEXT, created_at TEXT)""",
    "routes": """CREATE TABLE IF NOT EXISTS routes (
        name TEXT PRIMARY KEY, region TEXT, sort INTEGER DEFAULT 900, aliases TEXT,
        active INTEGER DEFAULT 1, note TEXT, created_at TEXT)""",
    PORT_ROUTES_TABLE: """CREATE TABLE IF NOT EXISTS port_routes (
        port_name TEXT NOT NULL, route_name TEXT NOT NULL,
        source TEXT NOT NULL DEFAULT 'manual', note TEXT, created_at TEXT,
        PRIMARY KEY (port_name, route_name),
        FOREIGN KEY (port_name) REFERENCES ports(name) ON DELETE CASCADE,
        FOREIGN KEY (route_name) REFERENCES routes(name) ON DELETE RESTRICT)""",
    "senders": """CREATE TABLE IF NOT EXISTS senders (
        name TEXT PRIMARY KEY, active INTEGER DEFAULT 1, note TEXT, source TEXT DEFAULT 'manual',
        created_at TEXT)""",
    "port_groups": """CREATE TABLE IF NOT EXISTS port_groups (
        name TEXT PRIMARY KEY, label TEXT, aliases TEXT, members TEXT, specials TEXT,
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
        publisher_id TEXT,
        id INTEGER PRIMARY KEY AUTOINCREMENT, entry_id TEXT NOT NULL UNIQUE,
        kind TEXT NOT NULL,
        payload TEXT NOT NULL,
        origin TEXT NOT NULL DEFAULT 'agent',
        resolved TEXT,
        action TEXT,
        target_key TEXT,
        check_status TEXT NOT NULL DEFAULT 'pending',
        errors TEXT,
        warnings TEXT,
        review_status TEXT NOT NULL DEFAULT 'awaiting',
        final_key TEXT,
        reviewed_by TEXT, reviewed_at TEXT, note TEXT, created_at TEXT)""",
    "review_issues": """CREATE TABLE IF NOT EXISTS review_issues (
        source_group TEXT NOT NULL, message_id TEXT NOT NULL,
        message_text TEXT NOT NULL, media_ids TEXT NOT NULL, reasons TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'open', created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL, resolved_by TEXT, resolved_at TEXT,
        PRIMARY KEY (source_group, message_id))""",
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
DDL['announcements'] = NOTICE_DDL
DDL[NOTIFICATION_TABLE] = NOTIFICATION_DDL
from core.admin_contracts import ADMIN_DDL, ADMIN_TABLES
DDL.update(ADMIN_DDL)
DDL.update(PUBLISHER_DDL)
from core.prospector_contracts import DDL as PROSPECTOR_DDL, PRIVATE_TABLES as PROSPECTOR_PRIVATE_TABLES
DDL.update(PROSPECTOR_DDL)
from core.growth_contracts import GROWTH_DDL, GROWTH_TABLE
DDL[GROWTH_TABLE] = GROWTH_DDL
from core.activity_contracts import (ACTIVITY_DDL, ACTIVITY_INDEX_COLUMNS, ACTIVITY_TABLE,
                                     QUOTE_USAGE_DDL, QUOTE_USAGE_INDEX_COLUMNS, QUOTE_USAGE_TABLE)
DDL[ACTIVITY_TABLE] = ACTIVITY_DDL
DDL[QUOTE_USAGE_TABLE] = QUOTE_USAGE_DDL
from core.api_monitor_contracts import API_EVENT_DDL, API_EVENT_INDEX_COLUMNS, API_EVENT_TABLE
DDL[API_EVENT_TABLE] = API_EVENT_DDL
from ops.item_feedback import SQLITE_DDL as ITEM_FEEDBACK_DDL, ACTIVE_INDEX as ITEM_FEEDBACK_ACTIVE_INDEX, ENTRY_INDEX as ITEM_FEEDBACK_ENTRY_INDEX
DDL["item_feedback"] = ITEM_FEEDBACK_DDL
from core.company_rates_contracts import COMPANY_RATE_DDL
DDL.update(COMPANY_RATE_DDL)

INDEX_PLAN = {
    API_EVENT_TABLE: list(API_EVENT_INDEX_COLUMNS),
    PORT_ROUTES_TABLE: ["route_name"],
    "freight_items": ["record_type", "pol", "pod", "carrier", "route", "container_type",
                      "space_type", "source_group", "message_time", "status", "origin", "supersedes"],
    "pending_records": ["review_status", "check_status", "kind", "created_at"],
    "auth_sessions": ["operator_name", "expires_at"],
    NOTIFICATION_TABLE: ["recipient_id", "created_at", "read_at", "entity_id"],
    GROWTH_TABLE: ["user_id", "created_at"],
    ACTIVITY_TABLE: list(ACTIVITY_INDEX_COLUMNS),
    QUOTE_USAGE_TABLE: list(QUOTE_USAGE_INDEX_COLUMNS),
    "company_rate_tasks": ["user_id", "kind", "query_signature", "created_at"],
    "company_rate_results": ["expires_at"],
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
    "carriers": ["code", "name_en", "name_cn", "scac", "sort", "aliases", "website_url", "active", "note"],
    "routes": ["name", "region", "sort", "aliases", "active", "note"],
    "senders": ["name", "active", "note"],
    "port_groups": ["name", "label", "aliases", "members", "specials", "kind", "active", "note"],
}


def connect(db_path: str | Path = DEFAULT_DB) -> sqlite3.Connection:
    """请求级轻量连接：只开连接 + 设连接级并发参数，不再重复建表/迁移/建索引。
    结构初始化只由 init_schema() 在服务启动时跑一次（manual_server 启动段调用）。
    全新库兜底：探测业务表不存在才现场 init（ops 脚本直连时也能自举）。"""
    conn = sqlite3.connect(str(db_path), check_same_thread=False, timeout=10.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=10000").fetchall()  # 写锁最长等 10 秒再报错
    conn.execute("PRAGMA synchronous=NORMAL").fetchall()  # 连接级配置，WAL 下持久与吞吐兼顾
    conn.execute("PRAGMA foreign_keys=ON")
    if conn.execute("SELECT name FROM sqlite_master "
                    "WHERE type='table' AND name='freight_items'").fetchone() is None:
        init_schema(conn)                                 # 全新库兜底；存量库走服务启动 init_schema
    return conn


def database_probe(db_path: str | Path = DEFAULT_DB) -> Dict[str, Any]:
    """Return a minimal verified persistence fact set for operations endpoints."""
    c = None
    try:
        c = connect(db_path)
        c.execute("SELECT 1").fetchone()
        return {"connected": True, "engine": "sqlite", "wal_enabled": True}
    except sqlite3.Error as exc:
        return {"connected": False, "engine": "sqlite", "error_type": type(exc).__name__}
    finally:
        if c is not None:
            c.close()


def find_active_auth_session(conn: sqlite3.Connection, token_hash: str,
                             now_epoch: int) -> Dict[str, Any] | None:
    """Resolve a session by its bearer token hash.

    ``client_ip`` is retained as login audit metadata only. Binding browser
    sessions to an IP breaks on DHCP, VPN and reverse proxies, while the
    cookie is already high-entropy, HttpOnly and stored only as a hash.
    """
    row = conn.execute(
        "SELECT o.user_id,o.name,o.display_name,o.role,s.created_at AS login_at,s.expires_at,COALESCE(sec.must_change_password,0) AS must_change_password FROM auth_sessions s JOIN operators o ON o.name=s.operator_name LEFT JOIN account_security sec ON sec.user_id=o.user_id "
        "WHERE s.token_hash=? AND s.expires_at>? AND o.active=1 "
        "AND o.password_hash IS NOT NULL",
        (token_hash, now_epoch)).fetchone()
    return dict(row) if row else None


def init_schema(conn: sqlite3.Connection) -> None:
    """建库/补列/索引，一次性。并发迁移竞态只在多连接同时跑这里时才会出现，
    因此只允许服务启动单线程调用；connect 仅在全新库探测兜底调用。"""
    # journal_mode 是数据库级写操作。过去每个读请求都重复设置它，高并发查询会
    # 争抢同一把 SQLite 锁；只允许服务启动/建库阶段设置一次。
    conn.execute("PRAGMA journal_mode=WAL").fetchall()
    if not conn.in_transaction:
        conn.execute("BEGIN IMMEDIATE")
    new_port_routes = not conn.execute("PRAGMA table_info(port_routes)").fetchall()
    for ddl in DDL.values():
        conn.execute(ddl)
    _migrate(conn)
    ensure_account_schema(conn, commit=False)
    if new_port_routes:
        try:
            _backfill_port_routes(conn)
        except Exception:
            conn.rollback()
            raise
    for table, cols in INDEX_PLAN.items():
        for c in cols:
            conn.execute(f'CREATE INDEX IF NOT EXISTS idx_{table}_{c} ON {table}({c})')
    conn.execute(ITEM_FEEDBACK_ACTIVE_INDEX)
    conn.execute(ITEM_FEEDBACK_ENTRY_INDEX)
    conn.commit()


def _replace_port_routes(conn, port: str, names: List[str], source: str = "manual") -> None:
    if names:
        conn.execute("DELETE FROM port_routes WHERE port_name=? AND route_name NOT IN ("
                     + ",".join("?" for _ in names) + ")", [port, *names])
    else:
        conn.execute("DELETE FROM port_routes WHERE port_name=?", (port,))
    for name in names:
        conn.execute("INSERT INTO port_routes(port_name,route_name,source,created_at) VALUES(?,?,?,?) "
                     "ON CONFLICT(port_name,route_name) DO NOTHING",
                     (port, name, source, now_cn()))
    mirror = ",".join(names)
    conn.execute("UPDATE ports SET route=? WHERE name=? AND COALESCE(route,'')<>?", (mirror, port, mirror))


def _backfill_port_routes(conn) -> None:
    known = {r[0] for r in conn.execute("SELECT name FROM routes")}
    for row in conn.execute("SELECT name,route FROM ports").fetchall():
        names = route_tokens(row["route"])
        unknown = [name for name in names if name not in known]
        if unknown:
            raise ValueError("legacy port routes contain unknown names: " + row["name"])
        _replace_port_routes(conn, row["name"], names, source="legacy")


def ensure_port_route_schema(conn) -> Dict[str, Any]:
    """One startup transaction; never infer new memberships from countries or quotes."""
    if conn.execute("PRAGMA table_info(port_routes)").fetchall():
        return {"ok": True, "data": {"migrated": False}}
    try:
        if getattr(conn, "engine", "sqlite") == "sqlite" and not conn.in_transaction:
            conn.execute("BEGIN IMMEDIATE")
        conn.execute(DDL[PORT_ROUTES_TABLE])
        conn.execute("CREATE INDEX IF NOT EXISTS idx_port_routes_route_name ON port_routes(route_name)")
        _backfill_port_routes(conn)
        count = conn.execute("SELECT COUNT(*) FROM port_routes").fetchone()[0]
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    invalidate_master()
    return {"ok": True, "data": {"migrated": True, "memberships": count}}


def backup_to(db_path: str | Path = DEFAULT_DB, dst: str | Path | None = None) -> str:
    """在线热备份：sqlite3 backup API 不锁库、WAL 内容一并写入。
    dst 缺省为 <库目录>/backup/freight_manual-<YYYYmmdd-HHMMSS>.db。返回备份文件路径。"""
    from datetime import datetime
    src = str(db_path)
    if not dst:
        d = os.path.dirname(os.path.abspath(src))
        bdir = os.path.join(d, "backup")
        os.makedirs(bdir, exist_ok=True)
        dst = os.path.join(bdir, "freight_manual-%s.db"
                           % datetime.now().strftime("%Y%m%d-%H%M%S"))
    src_c = sqlite3.connect(src)
    dst_c = sqlite3.connect(str(dst))
    try:
        src_c.backup(dst_c)
        dst_c.execute("PRAGMA wal_checkpoint(TRUNCATE)")  # 收尾 -wal 尾部，备份文件自洽
    finally:
        dst_c.close()
        src_c.close()
    return str(dst)


def _migrate(conn: sqlite3.Connection) -> None:
    """统一表迁移。测试阶段按用户授权清空旧业务数据，主数据与审核配置保留。"""
    legacy = {r[0]: r[1] for r in conn.execute(
        "SELECT name,type FROM sqlite_master WHERE name IN ('freight_rates','space_records')")}
    reset_test_data = any(typ == "table" for typ in legacy.values())
    if 'expires_on' not in {r[1] for r in conn.execute('PRAGMA table_info(announcements)')}:
        conn.execute('ALTER TABLE announcements ADD COLUMN expires_on TEXT')
    notice_columns = {r[1] for r in conn.execute('PRAGMA table_info(announcements)')}
    for column in NOTICE_SCOPE_COLUMNS:
        if column not in notice_columns:
            conn.execute(f"ALTER TABLE announcements ADD COLUMN {column} TEXT NOT NULL DEFAULT '[]'")
    if 'aliases' not in {r[1] for r in conn.execute('PRAGMA table_info(port_groups)')}:
        conn.execute('ALTER TABLE port_groups ADD COLUMN aliases TEXT')
    for name, typ in legacy.items():
        if typ == "view":
            conn.execute(f"DROP VIEW {name}")
        else:
            conn.execute(f"DROP TABLE {name}")
    # 只读兼容层：旧报表/导出仍可读取原表名，写路径统一经 freight_items。
    conn.execute("CREATE VIEW IF NOT EXISTS freight_rates AS "
                 "SELECT *, price_usd AS freight_usd FROM freight_items WHERE record_type='rates'")
    conn.execute("CREATE VIEW IF NOT EXISTS space_records AS "
                 "SELECT * FROM freight_items WHERE record_type='space'")
    if reset_test_data:
        # 本次是用户确认的测试库初始化：保留主数据/账号，清除所有业务、待审与由其学习出的条目。
        for table in ("pending_records", "pending_shadow", "rule_candidates", "field_defaults"):
            conn.execute(f"DELETE FROM {table}")
    ccols = {r[1] for r in conn.execute("PRAGMA table_info(carriers)")}
    if "website_url" not in ccols:
        conn.execute("ALTER TABLE carriers ADD COLUMN website_url TEXT")

    fcols = {r[1] for r in conn.execute("PRAGMA table_info(freight_items)")}
    for column in QUOTE_VARIANT_FIELDS.values():
        if column not in fcols:
            conn.execute(f"ALTER TABLE freight_items ADD COLUMN {column} TEXT")
    if "entry_id" not in fcols:
        conn.execute("ALTER TABLE freight_items ADD COLUMN entry_id TEXT")
    for row in conn.execute("SELECT id FROM freight_items WHERE entry_id IS NULL OR entry_id = ''").fetchall():
        conn.execute("UPDATE freight_items SET entry_id=? WHERE id=?", (legacy_entry_id("freight", row[0]), row[0]))
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_freight_items_entry_id ON freight_items(entry_id)")
    pcols = {r[1] for r in conn.execute("PRAGMA table_info(ports)")}
    if "route" not in pcols:          # 航线是港口的子属性（可逗号多值），存量库补列
        conn.execute("ALTER TABLE ports ADD COLUMN route TEXT")
    # 2026-09-17 P0 审计地基：pending_records 加原始快照 + 修改日志（审核动作反向沉淀的数据源）
    have_p = {r[1] for r in conn.execute("PRAGMA table_info(pending_records)")}
    if "orig_payload" not in have_p:
        conn.execute("ALTER TABLE pending_records ADD COLUMN orig_payload TEXT")
    if "edit_log" not in have_p:
        conn.execute("ALTER TABLE pending_records ADD COLUMN edit_log TEXT")
    if "warnings" not in have_p:
        conn.execute("ALTER TABLE pending_records ADD COLUMN warnings TEXT")
    if "entry_id" not in have_p:
        conn.execute("ALTER TABLE pending_records ADD COLUMN entry_id TEXT")
    for row in conn.execute("SELECT id FROM pending_records WHERE entry_id IS NULL OR entry_id = ''").fetchall():
        conn.execute("UPDATE pending_records SET entry_id=? WHERE id=?", (legacy_entry_id("pending", row[0]), row[0]))
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_pending_records_entry_id ON pending_records(entry_id)")
    # 港口覆盖率只是人工复核提示，不能把已通过的机械校验打回。
    coverage_rows = list(conn.execute(
        "SELECT id, errors, warnings FROM pending_records WHERE errors LIKE '%港口覆盖率%'"))
    for row in coverage_rows:
        try:
            errors = json.loads(row["errors"] or "[]")
            warnings = json.loads(row["warnings"] or "[]")
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        coverage = [e for e in errors if isinstance(e, dict) and e.get("field") == "港口覆盖率"]
        remaining = [e for e in errors if not (isinstance(e, dict) and e.get("field") == "港口覆盖率")]
        if not coverage:
            continue
        seen = {json.dumps(w, ensure_ascii=False, sort_keys=True) for w in warnings if isinstance(w, dict)}
        warnings.extend(w for w in coverage if json.dumps(w, ensure_ascii=False, sort_keys=True) not in seen)
        conn.execute("UPDATE pending_records SET errors=?, warnings=?, check_status=? WHERE id=?",
                     (json.dumps(remaining, ensure_ascii=False), json.dumps(warnings, ensure_ascii=False),
                      "passed" if not remaining else "failed", row["id"]))
    # 字典库来源标记列（system-auto vs manual）：存量库补列，CREATE IF NOT EXISTS 不给已建表加列
    for tbl in ("chat_groups", "senders"):
        cols = {r[1] for r in conn.execute(f"PRAGMA table_info({tbl})")}
        if "source" not in cols:
            conn.execute(f"ALTER TABLE {tbl} ADD COLUMN source TEXT DEFAULT 'manual'")
    # 2026-09-22 账户鉴权：原 operators 只有可见范围，不能作为登录身份。
    ocols = {r[1] for r in conn.execute("PRAGMA table_info(operators)")}
    if "role" not in ocols:
        conn.execute("ALTER TABLE operators ADD COLUMN role TEXT NOT NULL DEFAULT 'operator'")
    if "password_hash" not in ocols:
        conn.execute("ALTER TABLE operators ADD COLUMN password_hash TEXT")
    if "password_changed_at" not in ocols:
        conn.execute("ALTER TABLE operators ADD COLUMN password_changed_at TEXT")
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


def ensure_review_issue_schema(conn) -> None:
    conn.execute(DDL["review_issues"])
    conn.commit()


def upsert_review_issues(conn, issues: list[dict]) -> dict:
    if not isinstance(issues, list) or not issues or len(issues) > 200:
        raise ValueError("issues must contain 1-200 messages")
    timestamp = now_cn()
    for issue in issues:
        group = str(issue.get("source_group") or "").strip()
        message_id = str(issue.get("message_id") or "").strip()
        reasons = issue.get("reasons")
        media_ids = issue.get("media_ids") or []
        if not group or not message_id or not isinstance(reasons, list) or not reasons or not all(
                isinstance(reason, str) and reason.strip() for reason in reasons):
            raise ValueError("review issue requires source_group, message_id and reasons")
        if not isinstance(media_ids, list) or not all(isinstance(mid, str) for mid in media_ids):
            raise ValueError("media_ids must be a list of strings")
        conn.execute("INSERT INTO review_issues(source_group,message_id,message_text,media_ids,reasons,status,created_at,updated_at) "
                     "VALUES(?,?,?,?,?,'open',?,?) ON CONFLICT(source_group,message_id) DO UPDATE SET "
                     "message_text=excluded.message_text,media_ids=excluded.media_ids,reasons=excluded.reasons,"
                     "updated_at=excluded.updated_at WHERE review_issues.status='open'",
                     (group, message_id, str(issue.get("message_text") or ""),
                      json.dumps(media_ids, ensure_ascii=False), json.dumps(reasons, ensure_ascii=False),
                      timestamp, timestamp))
    conn.commit()
    return {"count": len(issues)}


def list_review_issues(conn, status: str = "open", limit: int = 200) -> dict:
    if status not in ("open", "resolved", "all"):
        raise ValueError("invalid issue status")
    limit = max(1, min(int(limit), 500))
    where = "" if status == "all" else "WHERE status=?"
    args = () if status == "all" else (status,)
    total = conn.execute("SELECT COUNT(*) FROM review_issues " + where, args).fetchone()[0]
    rows = conn.execute("SELECT * FROM review_issues " + where + " ORDER BY updated_at DESC,message_id LIMIT ?",
                        (*args, limit)).fetchall()
    return {"total": total, "rows": [dict(row, media_ids=json.loads(row["media_ids"]),
                                           reasons=json.loads(row["reasons"])) for row in rows]}


def resolve_review_issue(conn, source_group: str, message_id: str, reviewer: str) -> dict:
    if not source_group or not message_id or not reviewer:
        raise ValueError("source_group, message_id and reviewer required")
    timestamp = now_cn()
    cursor = conn.execute("UPDATE review_issues SET status='resolved',resolved_by=?,resolved_at=?,updated_at=? "
                          "WHERE source_group=? AND message_id=? AND status='open'",
                          (reviewer, timestamp, timestamp, source_group, message_id))
    conn.commit()
    return {"resolved": bool(cursor.rowcount)}


REVIEW_SETTLEMENT_MAX_RECORDS = 10000


def settle_review_issue(conn, body: dict) -> dict:
    group, mid = str(body.get("source_group") or ""), str(body.get("message_id") or "")
    ids, expected = body.get("pending_ids"), body.get("expected_count")
    non_quote = body.get("classification") == "non_quote"
    if (not group or not mid or not isinstance(ids, list) or (not ids and not non_quote)
            or not all(type(value) is int and value > 0 for value in ids)
            or len(ids) != len(set(ids)) or type(expected) is not int
            or expected != len(ids) or expected > REVIEW_SETTLEMENT_MAX_RECORDS):
        raise ValueError("settlement requires the complete unique pending record receipt")
    issue = conn.execute("SELECT * FROM review_issues WHERE source_group=? AND message_id=?", (group, mid)).fetchone()
    if not issue:
        raise ValueError("source review issue not found")
    source_rows = conn.execute("SELECT id,payload FROM pending_records WHERE json_extract(payload,'$.消息ID')=? "
                               "AND review_status IN ('awaiting','approved')", (mid,)).fetchall()
    source_ids = {row["id"] for row in source_rows
                  if json.loads(row["payload"]).get("消息原文") == issue["message_text"]}
    if non_quote:
        reason = str(body.get("reason") or "").strip()
        media = body.get("media_ids")
        if (ids or expected != 0 or source_ids or not reason or not isinstance(media, list)
                or set(media) != set(json.loads(issue["media_ids"]) or [])):
            raise ValueError("non-quote settlement requires matching image evidence, a reason and no staged quotes")
    if source_ids != set(ids):
        raise ValueError("settlement must cover the complete staged source message")
    for pending_id in ids:
        row = conn.execute("SELECT payload,review_status FROM pending_records WHERE id=?", (pending_id,)).fetchone()
        if not row or row["review_status"] not in ("awaiting", "approved"):
            raise ValueError("pending receipt missing or rejected")
        payload = json.loads(row["payload"])
        if payload.get("消息ID") != mid or payload.get("消息原文") != issue["message_text"]:
            raise ValueError("pending receipt source does not match review issue")
    timestamp = now_cn()
    reason = ("已逐图识读，非独立报价：" + reason) if non_quote else "已完成解析并转入待审核，共%d条；校验未过项在待审核中显示，不自动放行。" % expected
    conn.execute("UPDATE review_issues SET status='resolved',reasons=?,resolved_by='agent',resolved_at=?,updated_at=? "
                 "WHERE source_group=? AND message_id=? AND status='open'",
                 (json.dumps([reason], ensure_ascii=False), timestamp, timestamp, group, mid))
    conn.commit()
    return {"resolved": True, "pending_count": expected}


def today_cn() -> str:
    return now_cn()[:10]


# ---------------- 归一化基础件 ----------------

COUNTRIES = {"venezuela", "brazil", "argentina", "chile", "peru", "colombia", "uruguay",
             "paraguay", "bolivia", "ecuador", "guatemala", "honduras", "el salvador",
             "costa rica", "panama", "panamá", "cuba", "haiti", "dominican republic",
             "republica dominicana", "mexico"}
PORT_SEP = re.compile(r"[/、]")
LOCODE_LIST = re.compile(r"[A-Z]{2}[A-Z0-9]{3}(?:\s*[,，]\s*[A-Z]{2}[A-Z0-9]{3})+", re.I)

# ---- 多值展开 / 脏值硬闸 / 主数据兜底（三条入口共用，见 expand_cn 与 resolve_core）----
CARRIER_SEP = re.compile(r"[/、,，;；]+")          # 联盟/共舱写法 ONE/HPL/MSC/HMM；逗号在船司列不作港国分隔用
PRICE_MULTI = re.compile(r"[/、,，;；\s]+")
CTYPE_ENUM = ("20GP", "40HQ/HC", "40NOR", "45HQ/HC")
# 舱位类型十一档（2026-09-11 由八档扩）：空班=整班取消、跳港=船开但不挂这港、首航=新航线首班。
# 这三类过去被就近塞进「撤载改期」（本意是甩柜/退舱改期），语义不同且销售端查不到，故单独立档
SPACE_TYPE_ENUM = ("现舱", "加班船", "放舱开放", "售罄", "舱位紧张", "截关截单",
                   "撤载改期", "箱子动态", "空班", "跳港", "首航")
# "40HQ/HC" 本身含斜杠，柜型多值不能裸拆 → 先贪婪匹配已知写法
_CTYPE_FORM = re.compile(r"45HQ/HC|45HQ|45HC|40HQ/HC|40NOR/HC|40N/HC|40HQ-HC|20GP|40GP|40DC|40DV|40HQ|40HC|40NOR|40N|20|40", re.I)
CTYPE_FORMS = ("20GP", "20", "40HQ/HC", "40HQ", "40HC", "40HQ-HC",
               "40GP", "40DC", "40DV", "40NOR", "40N", "40NOR/HC", "40N/HC", "45HQ/HC", "45HQ", "45HC")
MAX_EXPAND = 60                                    # 单条消息展开上限：多维笛卡尔积炸行一律交人工
# 目的港里出现这些就是"说明文字/服务码/占位符"，不许部分命中就放过（只去X/Y、Base port via Z、(未注明…)）
POD_JUNK = re.compile(r"[()（）]|未注明|只去|via\b|base ?port|port group|待定|未拆|多港", re.I)
# 判脏之前先剥掉结尾的两位国家码消歧后缀（Cartagena(CO)/Manzanillo(MX)），否则会被误报成"含说明文字"
COUNTRY_SUFFIX = re.compile(r"\([A-Za-z]{2}\)\s*$")
# 内陆铁路点标记：by rail/CY/ICD/铁路/内陆 —— 这类目的港是"经铁路到内陆城市"，标记自带业务含义（非乱码说明），
# 单独引导"填海运港+内陆点入备注"，不混进"含说明文字"。CY/ICD 用词边界防误伤普通词。
INLAND_RAIL_RE = re.compile(r"by[\s-]*rail|rail\s*way|inland|铁路|内陆|\bICD\b|\bCY\b", re.I)
PLACEHOLDER = re.compile(r"^\s*[（(].*[)）]\s*$|未注明|待定|^\s*无\s*$", re.I)
CARRIER_SHAPE = re.compile(r"^[A-Z][A-Z0-9&\-]{1,5}$")   # 只有像船司缩写的未知 token 才允许自动补主数据


def fold(s: Any) -> str:
    """匹配用归一：去音标、转小写、压掉 . , ' - ( ) —— Antwerpen / Göteborg / Pecém 都对得上。"""
    from services.master_resolution import fold_name
    return fold_name(s)


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
    if c in ("45HQ/HC", "45HQ", "45HC"):
        return "45HQ/HC"
    if c.startswith("40"):
        return "40HQ/HC"
    return str(ct or "")


def ctype_list(raw: Any) -> List[str]:
    """20GP/40HQ/HC/40NOR/45HQ/HC" → 逐档拆分；识别不了就返回 [] 表示不拆。
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
    parts = []
    for item in PORT_SEP.split(str(v)):
        # Only a list of full LOCODEs is a comma delimiter. Keep port/country
        # names such as "Manzanillo,MX" intact for the existing lookup.
        tokens = re.split(r"[,，]", item) if LOCODE_LIST.fullmatch(item.strip()) else [item]
        parts.extend(token.strip() for token in tokens if token.strip())
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
        from services.master_resolution import NameResolver
        self.scope_rows = q("SELECT * FROM alias_scope")
        self.name_resolver = NameResolver(self.ports, self.carriers, self.routes, self.group_rows, self.scope_rows)
        # 作用域别名：(fold(别名), kind, 来源群) → 规范名；'*' 为不限群的全局覆盖
        self.scope_idx: Dict[tuple, str] = {}
        for r in self.scope_rows:
            self.scope_idx[(fold(r["alias"]), r["kind"], r["scope_group"] or "*")] = r["target"]
        from core.contracts import CONTEXT_ALIAS_APPROVAL_NOTE
        self.context_aliases = [dict(r) for r in conn.execute(
            "SELECT * FROM rule_candidates WHERE status='approved' AND target_layer='alias_scope' AND reviewer_note=?", (CONTEXT_ALIAS_APPROVAL_NOTE,))]

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
        self.port_routes: Dict[str, List[str]] = {}
        if conn.execute("PRAGMA table_info(port_routes)").fetchall():
            for r in q("SELECT pr.port_name,pr.route_name FROM port_routes pr "
                       "JOIN routes rt ON rt.name=pr.route_name WHERE rt.active=1 "
                       "ORDER BY COALESCE(rt.sort,900),rt.name"):
                self.port_routes.setdefault(r["port_name"], []).append(r["route_name"])
        else:
            # Read-only tools can still inspect an archived pre-migration SQLite file.
            self.port_routes = {p["name"]: route_tokens(p.get("route")) for p in self.ports}
        for p in self.ports:
            p[PORT_ROUTES_FIELD] = self.port_routes.get(p["name"], [])
            p["route"] = ",".join(p[PORT_ROUTES_FIELD])
        self.port_route = {name: ",".join(routes) for name, routes in self.port_routes.items() if routes}

        self.group_by_name: Dict[str, Dict[str, Any]] = {}
        self.member_to_groups: Dict[str, List[str]] = {}
        for g in self.group_rows:
            members, specials = split_list(g.get("members")), split_list(g.get("specials"))
            info = {"name": g["name"], "label": g.get("label") or "", "aliases": g.get("aliases") or "", "members": members,
                    "specials": specials, "kind": g.get("kind") or "pod",
                    "note": g.get("note") or ""}
            self.group_by_name[g["name"]] = info
            for m in members:
                self.member_to_groups.setdefault(m, []).append(g["name"])
        self.group_pol = {r["name"]: r["default_pol"] for r in conn.execute(
            "SELECT name, default_pol FROM chat_groups WHERE active=1 AND default_pol IS NOT NULL")}

        # 筛选侧"写法展开"索引：实体 → 它的全部已知**原始写法**。
        # 港口改名（Tianjin New Port → Xingang）后历史行仍存旧字面值，只按规范名筛会整批漏掉；
        # 折叠键只用来反查，取值必须用原文——SQL 的 IN/= 对 ASCII 大小写敏感。
        self.name_variants: Dict[str, List[str]] = {}
        self.variant_to_name: Dict[str, str] = {}

        def _add_variant(name: str, writing: Any) -> None:
            w = str(writing or "").strip()
            if not name or not w:
                return
            bucket = self.name_variants.setdefault(name, [])
            if w not in bucket:
                bucket.append(w)
            self.variant_to_name.setdefault(fold(w), name)

        for p in self.ports:
            nm = str(p.get("name") or "").strip()
            _add_variant(nm, nm)
            for c in ("name_en", "name_cn", "city"):
                _add_variant(nm, p.get(c))
            for v in split_list(p.get("aliases")):
                _add_variant(nm, v)
        for r in q("SELECT * FROM alias_scope"):
            tgt = str(r.get("target") or "").strip()
            if str(r.get("kind") or "") in ("pol", "pod") and tgt in self.name_variants:
                _add_variant(tgt, r.get("alias"))

    # ---- 单值归一 ----
    def contextual_record(self, rec):
        if not self.context_aliases:
            return rec
        from services.learning_policy import apply_context_aliases
        context = dict(rec)
        raw = str(rec.get('船司') or '')
        context['船司'] = self.carrier_idx.get(fold(raw)) or raw
        result = apply_context_aliases(context, self.context_aliases, fold,
                                       lambda carrier:self.carrier_idx.get(fold(carrier)) or carrier)
        if result.get('船司') == context['船司']:
            if '船司' in rec:
                result['船司'] = rec['船司']
            else:
                result.pop('船司', None)
        return rec if result == rec else result

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
        if fold(s) in self.name_idx:
            decision = self.name_resolver.resolve(kind, s, group)
            if decision['status'] == 'resolved' and decision['entity'] == 'port':
                return decision['canonical'], True
            return s, False
        hit = self.scoped(kind, s, group)                 # 本群专属写法优先于全局别名
        if hit:
            if hit in self.group_by_name:
                group_name = self.group_for(s, kind, group)
                return (group_name, True) if group_name else (s, False)
            return self.name_idx.get(fold(hit)) or hit, True
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

    def group_for(self, v: Any, side: str, group: str = "") -> str:
        """填的是港群名（或中文标签）就返回群名；用作起运港要看 kind 允许不允许。"""
        result = self.name_resolver.resolve(side, str(v or "").strip(), group)
        return result["canonical"] if result["entity"] == "port_group" else ""

    def derive_route(self, pod: str) -> str:
        """Only one unambiguous membership, or one common group membership, may fill a blank."""
        pod = str(pod or "").strip()
        if not pod:
            return ""
        if pod in self.port_routes:
            return unique_common_route([self.port_routes[pod]])
        g = self.group_by_name.get(pod)
        if g:
            return unique_common_route(self.port_routes.get(m, []) for m in g["members"])
        return ""

    def route_candidates(self, pod: str) -> List[str]:
        group = self.group_by_name.get(pod)
        members = group["members"] if group else [pod]
        return list(dict.fromkeys(route for member in members for route in self.port_routes.get(member, [])))

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
        rec = self.contextual_record(rec)
        grp = str(rec.get("来源群") or "").strip()
        dp = self.group_pol.get(grp, "")
        pod_group = self.group_for(rec.get("目的港"), "pod", grp)
        pods: List[str] = []
        if pod_group:
            # group_for 命中的是港群名，一般单值；仍走 split_list 兼容 alias_scope 未来多值串
            pods = split_list(pod_group) or [pod_group]
        else:
            for p in _split_port(rec.get("目的港")):
                if fold(p.split(",")[0]) in COUNTRIES:
                    continue                       # 纯国家名丢弃
                if INLAND_RAIL_RE.search(p):
                    pods.append(p)
                    continue
                np, ok = self.lookup_port(p, "pod", grp)
                if not np:
                    continue
                if p.strip() and not ok:
                    self.unmatched.append({"field": "目的港", "input": p})
                # 只在 alias_scope 命中时拆多值串（"WCSA、MEX"→两条 rates）；
                # 未命中保留原值交给 POD_JUNK 判脏，不误拆含中文逗号的说明性输入
                if ok:
                    parts = [np] if fold(np) in self.name_idx else split_list(np)
                    pods.extend(parts if parts else [np])
                else:
                    pods.append(np)
        if not pods:
            pods = [""]
        pol_group = self.group_for(rec.get("起运港"), "pol", grp)
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
                    parts = [final] if fold(final) in self.name_idx else split_list(final)
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

    def port_filter(self, col: str, val: str, expand_members: bool = True) -> Tuple[str, List[Any]]:
        """筛选展开两件事：① 同一实体的全部已知写法（港口改过名，历史行仍写旧字面值）；
        ② 群名↔成员双向（查成员港带出群价，查群名带出成员价）。"""
        side = col.rsplit(".", 1)[-1]
        group_name = self.group_for(val, side)
        if group_name:
            group = self.group_by_name[group_name]
            variants = [val, group_name, group["label"], *split_list(group["aliases"])]
            variants += [alias for (kind, alias), targets in self.name_resolver.aliases.items()
                         if kind == side and ("port_group", group_name) in targets]
            out = variants + (group["members"] if expand_members else [])
            # Member queries must also find historical group spellings.
            for member in group["members"] if expand_members else []:
                out += self.name_variants.get(member, [])
            uniq = [v for v in dict.fromkeys(out) if v]
            return f"{col} IN ({','.join('?' * len(uniq))})", uniq
        decision = self.name_resolver.resolve(side, val)
        if decision["status"] in ("ambiguous", "invalid_target"):
            return "1=0", []
        opposite = "pod" if side == "pol" else "pol"
        if self.group_for(val, opposite):
            return "1=0", []
        if not expand_members:
            return f"{col} = ?", [val]
        base = [val]
        canon = self.variant_to_name.get(fold(val))
        if canon:
            base += self.name_variants.get(canon, []) + [canon]
        cands = [v for v in dict.fromkeys(base) if v]
        out = list(cands)
        for v in cands:
            g = self.group_by_name.get(v)
            if g:
                out += g["members"]
            else:
                for name in self.member_to_groups.get(v, []):
                    info = self.group_by_name[name]
                    if info["kind"] not in (side, "both"):
                        continue
                    out += [name, info["label"], *split_list(info["aliases"])]
        uniq = [v for v in dict.fromkeys(out) if v]
        return f"{col} IN ({','.join('?' * len(uniq))})", uniq


_CACHE: Dict[str, Any] = {"master": None, "at": 0.0}
_MASTER_CACHE_LOCK = Lock()


def get_master(conn: sqlite3.Connection, ttl: float = 60.0) -> Master:
    if _CACHE["master"] is not None and (time.time() - _CACHE["at"]) <= ttl:
        return _CACHE["master"]
    # ThreadingHTTPServer 会同时处理首批销售查询；没有此锁会让所有线程重建
    # 1,200+ 港口的归一索引，造成仅冷启动时出现的秒级尾延迟。
    with _MASTER_CACHE_LOCK:
        if _CACHE["master"] is None or (time.time() - _CACHE["at"]) > ttl:
            m = Master(conn)
            if m.ports:
                _CACHE["master"], _CACHE["at"] = m, time.time()
            else:
                _CACHE["master"], _CACHE["at"] = m, 0.0  # 读空不缓存，下一请求重新拉
        return _CACHE["master"]


def invalidate_master() -> None:
    with _MASTER_CACHE_LOCK:
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


def normalize(cn_record: Dict[str, Any], kind: str, now: str, origin: str, entry_id: Optional[str] = None) -> Dict[str, Optional[str]]:
    spec = TABLES[kind]
    row: Dict[str, Optional[str]] = {c: None for c in ITEM_COLUMNS}
    # 先采用目标分类的字段名：海运费USD 与 价格USD 共用 price_usd，不能让另一别名覆盖。
    primary_cols = set(spec["map"].values())
    for cn, col in spec["map"].items():
        v = coerce(cn_record.get(cn))
        if v is not None:
            row[col] = v
    # 再保留另一分类的专属事实，供统一表版本链和后续重分类使用。
    for cn, col in ITEM_CN_MAP.items():
        if col in primary_cols:
            continue
        v = coerce(cn_record.get(cn))
        if v is not None:
            row[col] = v
    if not row.get("status"):
        row["status"] = DEFAULT_STATUS[kind]
    row["record_type"] = kind
    row["entry_id"] = entry_id or new_entry_id()
    if origin == "manual":
        row["message_time"] = now          # 录入时间：客户端传什么都不认
    for col, label in INT_COLS.get(kind, {}).items():
        v = row.get(col)
        if v not in (None, "") and not re.fullmatch(r"\d+", str(v).strip()):
            raise ValueError(f"{label} 必须是非负整数（收到 \"{v}\"）")
    # Empty additive fields must not change fingerprints of pre-upgrade records.
    legacy_keys = [f for f in spec["key"] if f not in QUOTE_VARIANT_FIELDS.values()]
    basis = "\x1f".join(canon_for_key(str(row.get(f) or "")) for f in legacy_keys)
    for column in QUOTE_VARIANT_FIELDS.values():
        if row.get(column):
            basis += "\x1f" + column + "=" + canon_for_key(str(row[column]))
    # Stable source IDs separate same-name senders; missing fields preserve legacy fingerprints.
    for column in SOURCE_FIELDS.values():
        if row.get(column):
            basis += "\x1f" + column + "=" + str(row[column])
    row["content_key"] = hashlib.sha256(basis.encode("utf-8")).hexdigest()
    row["synced_at"] = now
    row["origin"] = origin
    return row


def upsert_rows(conn: sqlite3.Connection, kind: str, rows: List[Dict[str, Optional[str]]],
                commit: bool = True, trusted_publisher_id: Optional[str] = None) -> int:
    if not rows:
        return 0
    table = TABLES[kind]["table"]
    cols = sorted({c for r in rows for c in r} | {"publisher_id"})
    ph = ",".join("?" * len(cols))
    upd = ",".join(f"{c}=excluded.{c}" for c in cols if c not in ("content_key", "entry_id"))
    sql = (f'INSERT INTO {table} ({",".join(cols)}) VALUES ({ph}) '
           f'ON CONFLICT(content_key) DO UPDATE SET {upd}')
    for r in rows:
        r["publisher_id"] = canonical_publisher(conn, trusted_publisher_id) if trusted_publisher_id else resolve_publisher(conn, r, now_cn())
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
    rec = master.contextual_record(rec)
    grp = str(rec.get("来源群") or "").strip()      # 作用域：同一缩写在不同群/列可指不同实体

    def port_side(cn: str, side: str) -> str:
        raw = str(rec.get(cn) or "").strip()
        if not raw:
            return ""
        outs = []
        for tok in _split_port(raw):
            # 原文明确标识 by rail/CY/ICD 的是内陆交付点，即使同名地点恰好存在于港口
            # 主数据，也不能被当作海港目的港放行。
            if INLAND_RAIL_RE.search(tok):
                cur_note = str(rec.get("备注") or "").strip()
                tag = f"内陆铁路点(by rail)：{tok}"
                if tag not in cur_note:
                    rec["备注"] = (cur_note + ("；" if cur_note else "") + tag)
                errors.append({"field": cn, "input": tok,
                               "msg": f"{cn}「{tok}」是内陆铁路点(by rail)，请把{cn}改成对应海运港(如 Genova/Venice/Trieste/Ravenna)；内陆目的地已记入备注供销售查看"})
                outs.append(tok); continue
            g = master.group_for(tok, side, grp)            # One canonical group plus scoped aliases.
            if g:
                outs.append(g); continue
            decision = master.name_resolver.resolve(side, tok, grp)
            if decision["status"] == "ambiguous" and any(
                    candidate["entity"] == "port_group" for candidate in decision.get("candidates", [])):
                errors.append({"field": cn, "input": tok, "msg": "名称对应多个港口或港群，无法确定"})
                outs.append(tok)
                continue
            name, ok = master.lookup_port(tok, side, grp)   # 专属别名 → 港口别名/中英/拼音/LOCODE
            if ok:
                outs.append(name); continue                 # 命中即放过：Caldera(CR)/Cartagena(CO) 是合法规范名
            # 内陆铁路点：带 by rail/CY/ICD/铁路/内陆 等【原文自带标记】的内陆城市（Milan(by rail) 米兰）。
            # 标记是有效业务知识，不笼统判"含说明文字"：① 整条原值搬进备注（销售端详情可见"经铁路至X"）
            # ② 专属引导让审核员把目的港填成对应海运港。裸写的 Milan/Padova（无标记）不走这里，仍按"不在主数据"人工判。
            # "国家(港名)"写法兜底：图片目的港常写成 Mexico(Progreso)/Brazil(Santos)，
            # 整串不在别名里但括号内港名主数据有 → 剥括号内核再试，命中就归一，别误判"含说明文字"
            inner = re.search(r'[（(]([A-Za-z][A-Za-z .\'&/-]*)[）)]\s*$', tok)
            if inner:
                nm2, ok2 = master.lookup_port(inner.group(1).strip(), side, grp)
                if ok2:
                    outs.append(nm2); continue
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


def name_resolution_shadow(master, rec, resolved, errors):
    """Compare names only; never change legacy validation or quote fields."""
    contextual = master.contextual_record(rec)
    group = str(contextual.get("来源群") or "")
    comparisons = []
    for field, side in (("起运港", "pol"), ("目的港", "pod"), ("船司", "carrier"), ("航线", "route")):
        raw = str(contextual.get(field) or "").strip()
        if not raw:
            continue  # Derived route/default values are business rules, not alias recognition.
        tokens = _split_port(raw) if side in ("pol", "pod") else (
            [v.strip() for v in CARRIER_SEP.split(raw) if v.strip()] if side == "carrier" else [raw])
        matches = [master.name_resolver.resolve(side, value, group) for value in tokens]
        accepted = all(value["status"] == "resolved" for value in matches)
        canonical = "/".join(dict.fromkeys(value["canonical"] for value in matches)) if accepted else None
        legacy_ok = not any(error["field"] == field for error in errors)
        equal = accepted == legacy_ok and (not accepted or canonical == resolved.get(field))
        comparisons.append({"field": field, "input": raw, "legacy": resolved.get(field),
                            "legacy_ok": legacy_ok, "matches": matches, "equal": equal})
    return {"stage": "shadow", "equal": all(value["equal"] for value in comparisons), "fields": comparisons}


def validate_record(conn: sqlite3.Connection, master: "Master", kind: str,
                    rec: Dict[str, Any]) -> Dict[str, Any]:
    """机械校验闸门（预检 / 全库体检 / 待审核 / 录入 共用）。返回 {ok, resolved, errors}。"""
    resolved, errors = resolve_core(master, kind, rec)
    if kind == "space" and not any(str(
            resolved.get(field) if field in resolved else rec.get(field) or "").strip()
            for field in SPACE_DESTINATION_SCOPE_FIELDS):
        errors.append({"field": "目的范围", "input": "",
                       "msg": "舱位必须填写目的港/港群、航线或航线服务，确保销售可以检索"})
    inv = {v: k for k, v in TABLES[kind]["map"].items()}
    cv = str(rec.get("柜型") or "").strip()
    if cv and not ctype_ok(cv):
        errors.append({"field": "柜型", "input": cv, "msg": "柜型只四档：20GP / 40HQ/HC / 40NOR / 45HQ/HC"})
    st = str(rec.get("舱位类型") or "").strip()
    if kind == "space" and st and st not in SPACE_TYPE_ENUM:
        errors.append({"field": "舱位类型", "input": st,
                       "msg": "舱位类型只十一档：" + " / ".join(SPACE_TYPE_ENUM)})
    if kind == "space" and st == "现舱" and not any(
            str(rec.get(field) or "").strip()
            for field in (*SPACE_DESTINATION_SCOPE_FIELDS, "船名航次", "箱量", "箱型箱量")):
        errors.append({"field": "现舱明细", "input": "",
                       "msg": "现舱至少需要目的港、船名航次或箱量，不能发布空占位"})
    if kind == "space":
        price = str(rec.get("价格USD") or "").strip()
        loss_fees = set(re.findall(r"亏[舱仓](?:费)?\s*(?:USD|CNY|RMB|¥|\$)?\s*(\d{2,4})",
                                   str(rec.get("消息原文") or ""), re.I))
        if price and price in loss_fees:
            errors.append({"field": "价格USD", "input": price,
                           "msg": "价格与原文明示亏舱费相同，不能当作舱位售价"})
    for col, label in INT_COLS.get(kind, {}).items():
        cn = inv.get(col)
        v = str(rec.get(cn) or "").strip() if cn else ""
        if v and not re.fullmatch(r"\d+", v):
            errors.append({"field": cn or col, "input": v, "msg": f"{label} 必须是非负整数"})
    warnings = []
    candidates = master.route_candidates(resolved.get("目的港") or "")
    if not resolved.get("航线") and candidates:
        warnings.append({"field": "航线归属", "input": ",".join(candidates),
                         "msg": "港口归属无法唯一确定报价航线，未自动补写；按原文审核"})
    return {"ok": not errors, "resolved": resolved, "errors": errors, "warnings": warnings,
            "name_resolution": name_resolution_shadow(master, rec, resolved, errors)}


def detect_ports_in_text(master: "Master", text: str) -> List[str]:
    """从原文里反向匹配所有出现的港口名（用于覆盖率校验）。
    返回匹配到的规范港口名列表（去重）。"""
    if not text or not text.strip():
        return []
    found = set()
    text_folded = fold(text)
    # 遍历港口别名索引，按别名长度从长到短匹配（避免短别名误匹配）
    for alias_folded, canon in master.port_idx.items():
        if len(alias_folded) < 2:   # 单字符太短，误匹配率高，跳过
            continue
        if alias_folded in text_folded:
            found.add(canon)
    return sorted(found)


def _find_current_by_identity(conn: sqlite3.Connection, kind: str,
                              row: Dict[str, Any], viewer: str = "") -> Optional[sqlite3.Row]:
    """按产品身份键（IDENTITY_COLS：承运人+起运+目的+柜型+ETD[+船名/舱位类型]，不含原文）找当前生效行。
    精确身份未命中时回退到「同产品身份(不含 ETD)+有效期窗口重叠」：同一报价换图/换原文重发时，
    两次解析的 ETD/有效期常有微差，精确键对不上就会重复堆行（实测 HMM 同原文两行并存即此因）。"""
    table = TABLES[kind]["table"]
    cols = IDENTITY_COLS[kind]
    visibility = scope_clause(conn, viewer)
    visibility = " AND " + visibility if visibility else ""
    where = " AND ".join(f"COALESCE({c},'')=?" for c in cols)
    args = [("" if row.get(c) is None else str(row.get(c))) for c in cols]
    for hit in conn.execute(
            f"SELECT content_key, valid_from, valid_to FROM {table} WHERE {where} AND record_type=? AND status=? " + visibility +
            " ORDER BY message_time DESC, id DESC", args + [kind, DEFAULT_STATUS[kind]]):
        if _windows_overlap(row.get('valid_from'), row.get('valid_to'), hit['valid_from'], hit['valid_to']):
            return hit
    base = [c for c in cols if c != "etd"]
    if len(base) != len(cols):
        where2 = " AND ".join(f"COALESCE({c},'')=?" for c in base)
        args2 = [("" if row.get(c) is None else str(row.get(c))) for c in base]
        for cand in conn.execute(
                f"SELECT content_key, valid_from, valid_to FROM {table} "
                f"WHERE {where2} AND record_type=? AND status=? "
                "AND (COALESCE(etd,'')='' OR ?='' OR COALESCE(etd,'')=?)" + visibility + " ORDER BY message_time DESC, id DESC",
                args2 + [kind, DEFAULT_STATUS[kind], str(row.get("etd") or ""), str(row.get("etd") or "")]):
            if _windows_overlap(row.get("valid_from"), row.get("valid_to"),
                                cand["valid_from"], cand["valid_to"]):
                return cand
    return None


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


def _walk_to_current(conn: sqlite3.Connection, kind: str, content_key: str, depth: int = 20, viewer: str = "") -> str:
    """从任意一版顺 supersedes 反查链向后走到『当前生效』版；断链/成环退回手上这版。
    方向铁律：只由旧→新走，不把死版本当更新目标，也绝不反向覆盖新行。"""
    table = TABLES[kind]["table"]
    visibility = scope_clause(conn, viewer)
    visibility = " AND " + visibility if visibility else ""
    key, seen = content_key, {content_key}
    for _ in range(depth):
        row = conn.execute(f"SELECT status FROM {table} WHERE content_key=?", (key,)).fetchone()
        if not row:
            return key                                   # 目标已不在库：退回原 key（上层按新增处理）
        if row["status"] == DEFAULT_STATUS[kind]:
            return key                                   # 命中当前生效版，停
        nxt = conn.execute(
            f"SELECT content_key FROM {table} WHERE supersedes=? " + visibility +
            " ORDER BY message_time DESC, id DESC LIMIT 1", (key,)).fetchone()
        if not nxt or nxt["content_key"] in seen:
            return key                                   # 链断/成环：用手上这版兜底，不硬猜
        seen.add(nxt["content_key"]); key = nxt["content_key"]
    return key


def find_quoted_target(conn: sqlite3.Connection, kind: str,
                       mid: Optional[str], quoted_body: Optional[str],
                       container_type: str = "", variant: Optional[Dict[str, Any]] = None, viewer: str = "") -> Optional[str]:
    """Resolve references only within the same explicit product variant.

    Ambiguous quoted messages never select an arbitrary sibling service/port.
    """
    table = TABLES[kind]["table"]
    variant = variant or {}
    columns = list(QUOTE_VARIANT_FIELDS.values())
    columns += [col for col in ("carrier", "pol", "pod") if variant.get(col)]
    clause = " AND ".join(f"COALESCE({col},'')=?" for col in columns)
    visibility = scope_clause(conn, viewer)
    if visibility:
        clause += " AND " + visibility
    args = [str(variant.get(col) or "") for col in columns]
    if container_type:
        clause += " AND COALESCE(container_type,'')=?"
        args.append(container_type)
    for column, value in (("mid", mid), ("message_text", quoted_body)):
        if not value:
            continue
        rows = conn.execute(f"SELECT content_key FROM {table} WHERE {column}=? AND record_type=? AND {clause}",
                            [value, kind, *args]).fetchall()
        targets = {_walk_to_current(conn, kind, row["content_key"], viewer=viewer) for row in rows}
        if targets:
            return next(iter(targets)) if len(targets) == 1 else None
    return None


def classify_new_or_update(conn: sqlite3.Connection, kind: str, rec: Dict[str, Any],
                           resolved: Dict[str, str], viewer: str = "") -> Tuple[str, Optional[str]]:
    """更新判定（给待审核页展示 + 暂存 target_key）：
    ① 产品身份（承运人+起运+目的+柜型/船名+ETD，【不含原文】）命中某当前生效行 → 更新；
    ② 本条原文带『——引用…——』指向某当前生效行 → 更新（单向）；③ 否则新增。
    身份不含原文：一条转发原文会拆成多柜型/多ETD，用原文当身份会互相误覆盖。"""
    probe = {"pol": resolved.get("起运港", ""), "pod": resolved.get("目的港", ""),
              "carrier": resolved.get("船司", ""),
              "valid_from": rec.get("有效期起"), "valid_to": rec.get("有效期止"),
             "container_type": norm_ctype(rec.get("柜型")) or str(rec.get("柜型") or "").strip(),
             "etd": str(rec.get("船期ETD") or "").strip(),
             "vessel_voyage": str(rec.get("船名航次") or "").strip(),
             "space_type": str(rec.get("舱位类型") or "").strip(),
             **{col: str(rec.get(cn) or "").strip() for cn, col in QUOTE_VARIANT_FIELDS.items()}}
    hit = _find_current_by_identity(conn, kind, probe, viewer=viewer)
    if hit:
        return ("update", hit["content_key"])
    _, body, mid = split_quote(rec.get("消息原文"))
    key = find_quoted_target(conn, kind, mid, body, probe.get("container_type", ""), probe, viewer=viewer)
    return ("update", key) if key else ("new", None)


def push_with_review(conn: sqlite3.Connection, master: "Master", kind: str,
                     records: Iterable[Dict[str, Any]], origin: str = "manual",
                     commit: bool = True, entry_id: Optional[str] = None, viewer: str = "",
                     trusted_publisher_id: Optional[str] = None) -> Dict[str, Any]:
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
                   cn.get("海运费USD") or cn.get("价格USD"),
                   *(cn.get(field) or "" for field in QUOTE_VARIANT_FIELDS))
            if key in seen:
                continue
            seen.add(key)
            rows.append(normalize(cn, kind, now, origin, entry_id=entry_id if len(exp) == 1 else None))
        for r in rows:
            hit = _find_current_by_identity(conn, kind, r, viewer=viewer)
            target = hit["content_key"] if (hit and hit["content_key"] != r["content_key"]) else None
            if not target:                                   # 无同原文命中 → 看引用指向哪条旧行（只此单向）
                _, body, mid = split_quote(r.get("message_text"))
                target = find_quoted_target(conn, kind, mid, body, r.get("container_type", ""), r, viewer=viewer)
                if target == r["content_key"]:
                    target = None
            if target:
                r["supersedes"] = target
            upsert_rows(conn, kind, [r], commit=False, trusted_publisher_id=trusted_publisher_id)
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
    "rates": ["船司", "起运港", "目的港", "柜型", "船名航次", "船期ETD", *QUOTE_VARIANT_FIELDS],
    "space": ["船司", "起运港", "目的港", "柜型", "船名航次", "舱位类型", "船期ETD", *QUOTE_VARIANT_FIELDS],
}


def _pending_identity_loose(kind: str, rec: Dict[str, Any]) -> tuple:
    """判重用产品身份（不含 ETD）：同一报价换图/换原文重发时 ETD 解析常有差异，
    精确身份（含 ETD）对不上会重复堆行。去 ETD 后配合有效期窗口重叠做宽松归并。"""
    rec = rec or {}
    if kind == "group_suggestion":
        return (str(rec.get("港群") or "").strip(),)
    if kind == "rates":
        return tuple(str(rec.get(c) or "").strip() for c in ("船司", "起运港", "目的港", "柜型", "船名航次", *QUOTE_VARIANT_FIELDS))
    return tuple(str(rec.get(c) or "").strip() for c in ("船司", "起运港", "目的港", "柜型", "船名航次", "舱位类型", *QUOTE_VARIANT_FIELDS))


# 消息级条款字段：一条报价通常整票只写一次（`Freetime：10days 【HDL USD 25/柜】`），
# 拆成多档柜型时 LLM 常常只把它挂在第一行 → 表里 F/T 列看着"匹配不到"（2026-09-22 实例 398/399）。
MSG_LEVEL_FIELDS = ("目免", "T/T航程", "亏舱费")


def inherit_message_fields(records: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """同消息、船司、港口、服务及日期产品的柜型行补齐条款字段。

    只在"唯一值"时动手，两种情况绝不改：**组内有多个不同值**（业务上免箱期确实按柜型分档，
    20GP 7 天 / 40HQ 14 天真有这种事）、**整组都没值**（不猜，交采集或人工）。只补空、
    不改已有值，也不碰 备注/消息原文。返回补齐明细，供调用方记日志给审核员复核。"""
    groups: Dict[tuple, List[Dict[str, Any]]] = {}
    for rec in records or []:
        mid = str(rec.get("消息ID") or "").strip()
        if mid:
            scope = tuple(str(rec.get(key) or "").strip() for key in (
                "来源群", "消息ID", "船司", "起运港", "目的港", "航线服务",
                "船名航次", "船期ETD", "有效期起", "有效期止"))
            groups.setdefault(scope, []).append(rec)
    done: List[Dict[str, Any]] = []
    for scope, rows in groups.items():
        mid = scope[1]
        for fld in MSG_LEVEL_FIELDS:
            vals = {str(r.get(fld)).strip() for r in rows if str(r.get(fld) or "").strip()}
            if len(vals) != 1:
                continue
            val = vals.pop()
            for r in rows:
                if not str(r.get(fld) or "").strip():
                    r[fld] = val
                    done.append({"消息ID": mid, "字段": fld, "值": val,
                                 "柜型": r.get("柜型"), "来源群": r.get("来源群")})
    return done


def _windows_overlap(a1: Any, a2: Any, b1: Any, b2: Any) -> bool:
    """有效期窗口重叠判定（ISO 日期字符串可直接比较）；任一方缺失 → False（保守不合并）。
    只用于「同一报价换图/换原文重发」的宽松判重，避免不同航次的真实报价被误合并。"""
    a1, a2 = str(a1 or "").strip(), str(a2 or "").strip()
    b1, b2 = str(b1 or "").strip(), str(b2 or "").strip()
    if not (a1 and a2 and b1 and b2):
        return False
    try:
        return a1 <= b2 and b1 <= a2
    except Exception:
        return False


# 航线/群名关键词 → 国家码集合：仅作 difflib 候选的「防误配软约束」——
# 命中但目标港国家不在集合内，且相似度不够高时，拒绝生成候选（如 Arrecife-ES ↔ Recife-BR 跨洲误配）。
_ROUTE_GEO_HINT = {
    "南美东": {"BR", "UY", "AR"},
    "巴西": {"BR"},
    "南美西": {"CL", "PE", "EC"},
    "墨西哥": {"MX"},
    "加勒比": {"PA", "CO", "VE", "CR", "NI", "GT", "HN", "DO", "JM", "TT"},
    "中美洲": {"MX", "GT", "HN", "NI", "CR", "PA"},
    "地西": {"ES", "IT", "FR", "MA", "TN"},
    "地东": {"GR", "TR", "EG", "IL", "CY"},
    "亚得里亚海": {"IT", "HR", "SI"},
}


def _geo_hint_for(payload: Dict[str, Any]) -> set:
    """从 payload 的航线/来源群关键词推断目标国家码集合；无线索返回空集（不约束）。"""
    hints: set = set()
    for src in (str(payload.get("航线") or ""), str(payload.get("来源群") or ""),
                str(payload.get("source_group") or "")):
        if not src:
            continue
        for kw, countries in _ROUTE_GEO_HINT.items():
            if kw in src:
                hints |= countries
    return hints


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
                  records: List[Dict[str, Any]], origin: str = "agent",
                  auto_suggest: bool = True, preserve_other_sources: bool = False,
                  preserve_existing_source_rows: bool = False,
                  preserved_ids: Optional[List[int]] = None, viewer: str = "") -> List[int]:
    """非人工入口先按港拆分 + 归一（expand_cn），再逐条机械校验 + 判新增/更新，暂存待审核，绝不直接落业务表。
    auto_suggest=True：校验失败的条目（未命中主数据）落库后立即自动生成别名候选（rule_candidates），
    人工批准写 alias_scope → 下次同写法识别直接通过（「识别失败 → 自动提出 → 人工批准」闭环）。
    合并港拆每港一条；港群名不拆；中文/别名/拼音归一。
    同产品身份若已有未处理(待审)旧条 → 默认撤旧留新（删旧），待审只留最新一条。
    preserve_existing_source_rows=True 专供同一来源补缺项：命中已有产品时保留其编号、审计和内容，
    只插入缺失产品；preserved_ids 接收被保留且覆盖本次请求的旧编号。"""
    from services.agent_repair import pending_source_scope
    from services.submission_coverage import fingerprint
    source_scope = pending_source_scope(records, preserve_other_sources, preserve_existing_source_rows)
    if preserve_existing_source_rows and kind not in ("rates", "space"):
        raise ValueError("preserve_existing_source_rows supports rates/space only")

    def scoped_identity(rec, loose=False):
        product = _pending_identity_loose(kind, rec) if loose else _pending_identity(kind, rec)
        if source_scope:
            return (str(rec.get('来源群') or '').strip(), str(rec.get('消息ID') or '').strip(), *product)
        return product

    now = now_cn()
    ids: List[int] = []
    awaiting_idx: Dict[tuple, List[int]] = {}
    awaiting_win: Dict[tuple, List[tuple]] = {}   # 无ETD产品身份 → [(id, 有效期起, 有效期止)]：窗口归并用
    awaiting_dates: Dict[int, tuple] = {}
    awaiting_payloads: Dict[int, dict] = {}
    audited_ids = set()
    visibility = scope_clause(conn, viewer, "json_extract(p.payload,'$.来源群')", "json_extract(p.payload,'$.航线')")
    visibility = " AND " + visibility if visibility else ""
    for r in conn.execute("SELECT id,payload,edit_log FROM pending_records p WHERE kind=? AND review_status='awaiting'" + visibility, (kind,)):
        try:
            p = json.loads(r["payload"])
        except Exception:
            p = {}
        if source_scope and (str(p.get("来源群") or "").strip(), str(p.get("消息ID") or "").strip()) not in source_scope:
            continue
        if preserve_existing_source_rows:
            p.update(validate_record(conn, master, kind, p).get("resolved") or {})
        awaiting_idx.setdefault(scoped_identity(p), []).append(r["id"])
        awaiting_payloads[r["id"]] = p
        if r["edit_log"]:
            audited_ids.add(r["id"])
        dates = (str(p.get("有效期起") or "").strip(), str(p.get("有效期止") or "").strip())
        awaiting_dates[r["id"]] = dates
        awaiting_win.setdefault(scoped_identity(p, loose=True), []).append((r["id"], *dates, str(p.get("船期ETD") or "").strip()))
    for rec in records:
        if kind == GSUG_KIND:               # 港群候选：不展开、不落业务表，只校验+暂存，等人工放行
            one = dict(rec)
            ik = scoped_identity(one)
            for old in awaiting_idx.pop(ik, []):
                conn.execute("DELETE FROM pending_records WHERE id=? AND review_status='awaiting'", (old,))
                if old in ids:
                    ids.remove(old)
            v = validate_group_suggestion(conn, one)
            pending_entry_id = new_entry_id()
            cur = conn.execute(
                "INSERT INTO pending_records(entry_id,kind,payload,origin,resolved,action,target_key,"
                "check_status,errors,review_status,orig_payload,created_at) VALUES(?,?,?,?,?,?,?,?,?, 'awaiting', ?, ?)",
                (pending_entry_id, kind, json.dumps(one, ensure_ascii=False), origin,
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
            if preserve_existing_source_rows:
                raise ValueError("source-row preservation cannot append an over-limit expansion")
            ik = scoped_identity(rec)
            for old in awaiting_idx.pop(ik, []):
                conn.execute("DELETE FROM pending_records WHERE id=? AND review_status='awaiting'", (old,))
            err = [{"field": "展开", "input": "",
                    "msg": f"本条展开成 {len(exp)} 行，超过上限 {MAX_EXPAND}：合并船司/港口/柜型请按原文实际配对拆开再传"}]
            pending_entry_id = new_entry_id()
            cur = conn.execute(
                "INSERT INTO pending_records(entry_id,kind,payload,origin,resolved,action,target_key,"
                "check_status,errors,review_status,orig_payload,created_at) VALUES(?,?,?,?,?,?,?,?,?, 'awaiting', ?, ?)",
                (pending_entry_id, kind, json.dumps(rec, ensure_ascii=False), origin, json.dumps({}, ensure_ascii=False),
                 "new", "", "failed", json.dumps(err, ensure_ascii=False),
                 json.dumps(rec, ensure_ascii=False), now))
            ids.append(cur.lastrowid)
            awaiting_idx.setdefault(ik, []).append(cur.lastrowid)
            continue
        for one in exp:
            if preserve_existing_source_rows:
                one.update(validate_record(conn, master, kind, one).get("resolved") or {})
            ik = scoped_identity(one)
            vf, vt = str(one.get("有效期起") or "").strip(), str(one.get("有效期止") or "").strip()
            etd = str(one.get("船期ETD") or "").strip()
            def independent_batch_row(old):
                # An interval quote and a sailing quote in this submission coexist.
                previous = awaiting_payloads.get(old, {})
                return old in ids and (vf, vt, etd) != (
                    *awaiting_dates.get(old, ("", "")),
                    str(previous.get("船期ETD") or "").strip())
            exact_matches = []
            for old in list(awaiting_idx.get(ik, [])):
                if independent_batch_row(old):
                    continue
                of, ot = awaiting_dates.get(old, ("", ""))
                if all((vf, vt, of, ot)) and not _windows_overlap(vf, vt, of, ot):
                    continue
                exact_matches.append(old)
            # 同产品身份(不含ETD) + 有效期窗口重叠 → 默认撤旧留新；补缺模式只认定为已覆盖。
            ik2 = scoped_identity(one, loose=True)
            loose_matches = []
            for (old, of, ot, old_etd) in list(awaiting_win.get(ik2, [])):
                if independent_batch_row(old):
                    continue
                etd = str(one.get("船期ETD") or "").strip()
                if etd and old_etd and etd != old_etd:
                    continue  # Explicit different sailings are independent products.
                if not _windows_overlap(vf, vt, of, ot):
                    continue
                loose_matches.append((old, of, ot, old_etd))
            if preserve_existing_source_rows:
                expected_fingerprint = fingerprint(kind, one)
                def covered(old):
                    return old in audited_ids or fingerprint(kind, awaiting_payloads[old]) == expected_fingerprint
                exact_matches = [old for old in exact_matches if covered(old)]
                loose_matches = [item for item in loose_matches if covered(item[0])]
            if preserve_existing_source_rows and (exact_matches or loose_matches):
                if preserved_ids is not None:
                    for old in dict.fromkeys([*exact_matches, *(item[0] for item in loose_matches)]):
                        if old not in ids and old not in preserved_ids:
                            preserved_ids.append(old)
                continue
            for old in exact_matches:
                conn.execute("DELETE FROM pending_records WHERE id=? AND review_status='awaiting'", (old,))
                awaiting_idx[ik].remove(old)
                if old in ids:
                    ids.remove(old)
                # 同步从窗口索引摘掉（已被精确身份撤旧，避免窗口归并再碰它）
                ik2 = scoped_identity(one, loose=True)
                if ik2 in awaiting_win:
                    awaiting_win[ik2] = [(o, of, ot, oe) for (o, of, ot, oe) in awaiting_win[ik2] if o != old]
            for (old, of, ot, old_etd) in loose_matches:
                if old in exact_matches:
                    continue
                awaiting_win[ik2].remove((old, of, ot, old_etd))
                if old in awaiting_idx.get(ik, []):
                    awaiting_idx[ik].remove(old)
                conn.execute("DELETE FROM pending_records WHERE id=? AND review_status='awaiting'", (old,))
                if old in ids:
                    ids.remove(old)
            v = validate_record(conn, master, kind, one)
            action, target = classify_new_or_update(conn, kind, one, v["resolved"], viewer=viewer)
            pending_entry_id = new_entry_id()
            cur = conn.execute(
                "INSERT INTO pending_records(entry_id,kind,payload,origin,resolved,action,target_key,"
                "check_status,errors,warnings,review_status,orig_payload,created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?, 'awaiting', ?, ?)",
                (pending_entry_id, kind, json.dumps(one, ensure_ascii=False), origin,
                 json.dumps(v["resolved"], ensure_ascii=False), action, target,
                 "passed" if v["ok"] else "failed",
                 json.dumps(v["errors"], ensure_ascii=False),
                 json.dumps(v["warnings"], ensure_ascii=False),
                 json.dumps(one, ensure_ascii=False), now))
            nid = cur.lastrowid
            ids.append(nid)
            awaiting_idx.setdefault(ik, []).append(nid)
            awaiting_dates[nid] = (vf, vt)
            awaiting_payloads[nid] = one
            awaiting_win.setdefault(ik2, []).append((nid, vf, vt, str(one.get("船期ETD") or "").strip()))
            if auto_suggest and not v["ok"]:
                # 失败即自动提出：仅对"未命中主数据"类错误生成候选（函数内部按 _CANDIDATE_HIT_MSG 过滤）；
                # 找不到相近规范名（如纯中文新写法「勒哈佛」）不产生候选，交人工/未来 AI 审计
                suggest_candidates_for_failed(conn, {
                    "id": nid, "kind": kind,
                    "payload": json.dumps(one, ensure_ascii=False),
                    "errors": json.dumps(v["errors"], ensure_ascii=False)})
    # 港口覆盖率反向校验：按消息ID分组，确保原文里出现的所有目的港都被识别到
    if kind == "rates":
        coverage_ids = ids
        if source_scope:
            coverage_ids = []
            for row in conn.execute("SELECT id,payload,warnings FROM pending_records WHERE kind=? AND review_status='awaiting'", (kind,)):
                payload = json.loads(row['payload'])
                if (str(payload.get('来源群') or '').strip(), str(payload.get('消息ID') or '').strip()) in source_scope:
                    coverage_ids.append(row['id'])
                    warnings = [w for w in json.loads(row['warnings'] or '[]') if not isinstance(w, dict) or w.get('field') != '港口覆盖率']
                    conn.execute('UPDATE pending_records SET warnings=? WHERE id=?', (json.dumps(warnings, ensure_ascii=False), row['id']))
        _coverage_check(conn, master, coverage_ids)
    for pending_id in ids:
        row = conn.execute("SELECT payload FROM pending_records WHERE id=?", (pending_id,)).fetchone()
        if row:
            identity = resolve_publisher(conn, json.loads(row["payload"]), now)
            conn.execute("UPDATE pending_records SET publisher_id=? WHERE id=?", (identity,pending_id))
    conn.commit()
    return ids


def _coverage_check(conn: sqlite3.Connection, master: "Master", ids: List[int]):
    """按消息ID分组做港口覆盖率校验：原文里出现的目的港，如果没被提取出来，标红警告。
    只警告不打回——航管人工确认是否真的漏了。"""
    if not ids:
        return
    # 按消息ID分组收集
    groups: Dict[str, Dict[str, Any]] = {}
    for r in conn.execute(
            f"SELECT id,payload,resolved,errors FROM pending_records WHERE id IN ({','.join('?'*len(ids))})",
            ids):
        try:
            p = json.loads(r["payload"] or "{}")
            res = json.loads(r["resolved"] or "{}")
            errs = json.loads(r["errors"] or "[]")
        except Exception:
            continue
        mid = str(p.get("消息ID") or "").strip()
        if not mid:
            continue
        if mid not in groups:
            groups[mid] = {"ids": [], "text": "", "pods": set(), "pols": set(), "errs": {}}
        groups[mid]["ids"].append(r["id"])
        groups[mid]["text"] = str(p.get("消息原文") or "")
        pod = res.get("目的港") or p.get("目的港") or ""
        if pod:
            groups[mid]["pods"].update(_coverage_locations(master, pod, "pod"))
        pol = res.get("起运港") or p.get("起运港") or ""
        if pol:
            groups[mid]["pols"].update(_coverage_locations(master, pol, "pol"))
        groups[mid]["errs"][r["id"]] = errs
    # 逐条消息做覆盖率校验
    for mid, g in groups.items():
        text = g["text"]
        if not text:
            continue
        # 从原文反向匹配所有港口
        found_ports = set(detect_ports_in_text(master, text))
        if not found_ports:
            continue
        # 原文里出现的港口，减去已提取目的港和起运港，才是可能漏掉的目的港。
        # 目的港群需要展开到成员港再比较，否则提了“西班牙基本港”仍会误报 Valencia。
        missing = sorted(found_ports - g["pods"] - g["pols"])
        if not missing:
            continue
        # 把遗漏写为独立警告：提示人工核对，但不影响机械校验结果。
        warn = {"field": "港口覆盖率", "input": "",
                "msg": f"原文中可能存在未识别的目的港：{', '.join(missing[:5])}"
                       + (f" 等{len(missing)}个" if len(missing) > 5 else "")}
        for pid in g["ids"]:
            old = conn.execute("SELECT warnings FROM pending_records WHERE id=?", (pid,)).fetchone()
            try:
                warnings = json.loads((old["warnings"] if old else "") or "[]")
            except (TypeError, ValueError, json.JSONDecodeError):
                warnings = []
            if not any(e.get("field") == "港口覆盖率" for e in warnings if isinstance(e, dict)):
                warnings.append(warn)
                conn.execute("UPDATE pending_records SET warnings=? WHERE id=?",
                             (json.dumps(warnings, ensure_ascii=False), pid))


def _coverage_locations(master: "Master", raw: Any, side: str) -> set:
    """把已解析的起运/目的港展开为可与原文反查结果比较的规范港集合。"""
    names = set()
    for token in _split_port(raw):
        group = master.group_for(token, side)
        if group:
            names.update(master.group_by_name.get(group, {}).get("members") or [])
            continue
        canon, ok = master.lookup_port(token, side)
        names.add(canon if ok else token)
    return names


GROUP_MARK = "组完整性"        # 历史标记：曾经把整组连坐刷成未过，现只用于清理；归属改由读取路径派生


def group_mark_cleanup(conn: sqlite3.Connection, dry_run: bool = True) -> Dict[str, Any]:
    """一次性复原：剥掉历史「组完整性」连坐标记，并把每行 check_status 还原成它自己的真实判定。

    背景：连坐一度直接改 check_status，把"本身识别正确、只是同组别条未过"的行也标成未过——
    那是篡改校验结论。现在整组归属由 _pending_where(group_aware=True) 派生，判定不再被动。
    默认 dry_run=True（只报会动多少条），确认后再写。"""
    scanned = stripped = restored = 0
    samples: List[Dict[str, Any]] = []
    for r in conn.execute("SELECT id,check_status,errors,kind,payload FROM pending_records"
                          " WHERE review_status='awaiting' ORDER BY id"):
        scanned += 1
        try:
            errs = json.loads(r["errors"] or "[]")
        except Exception:
            continue                                   # errors 坏了不猜，交人工
        own = [e for e in errs if (e or {}).get("field") != GROUP_MARK]
        want = "failed" if own else "passed"
        had_mark = len(own) != len(errs)
        if not had_mark and r["check_status"] == want:
            continue
        stripped += 1 if had_mark else 0
        restored += 1 if r["check_status"] != want else 0
        if len(samples) < 5:
            samples.append({"id": r["id"], "was": r["check_status"], "will": want,
                            "mark": had_mark})
        if not dry_run:
            conn.execute("UPDATE pending_records SET check_status=?, errors=? WHERE id=?",
                         (want, json.dumps(own, ensure_ascii=False), r["id"]))
    if not dry_run and (stripped or restored):
        conn.commit()
    return {"ok": True, "scanned": scanned, "marks_stripped": stripped,
            "status_restored": restored, "dry_run": dry_run, "sample": samples}


def _pending_row(r: Optional[sqlite3.Row]) -> Optional[Dict[str, Any]]:
    if not r:
        return None
    d = dict(r)
    for k in ("payload", "resolved", "errors", "warnings"):
        d[k] = json.loads(d[k] or "null")
    return d


GRP_KEY_SQL = ("COALESCE(NULLIF(TRIM(json_extract({t}.payload,'$.消息ID')),''), 'row-'||{t}.id)")
# 半连接而非相关子查询：后者会让外层每一行都全表重扫一遍（354 行实测 770ms/次，列表要跑两次）。
# 键值必须非 NULL（COALESCE 兜成 row-<id>），否则 "NOT (NULL IN ...)" 是 unknown，
# 没带消息ID 的通过行会被整条挤出「已通过」页签。
GRP_BAD_SQL = ("(" + GRP_KEY_SQL.format(t="p") + ", p.kind) IN"
               " (SELECT " + GRP_KEY_SQL.format(t="p2") + ", p2.kind FROM pending_records p2"
               " WHERE p2.review_status='awaiting' AND p2.check_status != 'passed')")


def _normalized_message_id_sql(column: str) -> str:
    """Normalize only caller-owned SQL columns; incoming IDs remain bound values."""
    text = f"TRIM(CAST({column} AS TEXT))"
    prefix_length = len(SOURCE_MESSAGE_ID_PREFIX)
    return (f"RTRIM(CASE WHEN SUBSTR({text}, 1, {prefix_length})='{SOURCE_MESSAGE_ID_PREFIX}' "
            f"THEN SUBSTR({text}, {prefix_length + 1}) ELSE {text} END, '=')")


def _pending_source_key_sql(alias: str) -> str:
    message = _normalized_message_id_sql(f"json_extract({alias}.payload,'$.消息ID')")
    return f"COALESCE(NULLIF({message},''), 'row-'||{alias}.id)"


def _bulk_pending_where(conn, kind: str = "", viewer: str = "",
                        eligible: bool = True, entry_id: str = "", message_id: str = "",
                        id_query: str = "") -> Tuple[str, List[Any]]:
    """Count and execute with the same complete-source gate, before scope filtering."""
    frag, args = _pending_where("awaiting", kind, "passed", conn=conn, viewer=viewer,
                                entry_id=entry_id, message_id=message_id, id_query=id_query)
    bad = "COALESCE(p2.check_status,'') != 'passed'"
    sibling_scope = scope_clause(conn, viewer, "json_extract(p2.payload,'$.来源群')",
                                "json_extract(p2.payload,'$.航线')") if viewer else ""
    if sibling_scope:
        bad += " OR NOT COALESCE((" + sibling_scope + "), FALSE)"
    blocked = (_pending_source_key_sql("p") + " IN (SELECT " + _pending_source_key_sql("p2")
               + " FROM pending_records p2 WHERE p2.review_status='awaiting'"
                 " AND p2.kind IN ('rates','space') AND (" + bad + "))")
    return frag + " AND p.kind IN ('rates','space') AND " + ("NOT " if eligible else "") + blocked, args


def _pending_where(status: str = "awaiting", kind: str = "",
                   check: str = "", group_aware: bool = False, entry_id: str = "",
                   message_id: str = "", id_query: str = "", conn=None, viewer: str = "") -> Tuple[str, List[Any]]:
    """队列筛选条件的唯一构造点：列表分页与「批量同步」候选扫描共用，避免两处口径漂移。
    返回 (WHERE 片段, 绑定参数)。未知 kind 值一律忽略（不当成筛选，也不报错）。
    group_aware（只给看板列表用）＝**按来源消息整组归属**：同一条消息拆出的子条目要么一起出现在
    "未过"、要么一起出现在"已通过"，否则审核员看到半截会误判识别完整性。
    注意它只改**归属**，不改任何一行的 check_status——通过的就是通过。"""
    where: List[str] = []
    args: List[Any] = []
    if status and status != "all":
        where.append("review_status = ?"); args.append(status)
    if entry_id:
        where.append("entry_id = ?"); args.append(entry_id.strip())
    message_column = _normalized_message_id_sql("json_extract(p.payload,'$.消息ID')")
    if message_id:
        value = normalize_identity_query(message_id)
        where.append(message_column + " = ?" if value else "1=0")
        if value:
            args.append(value)
    if id_query:
        value = normalize_identity_query(id_query)
        where.append("(entry_id = ? OR " + message_column + " = ?)" if value else "1=0")
        if value:
            args.extend([value, value])
    if kind in ("rates", "space", GSUG_KIND):
        where.append("kind = ?"); args.append(kind)
    if check == "failed":
        where.append("(check_status != 'passed'" + (" OR " + GRP_BAD_SQL if group_aware else "") + ")")
    elif check == "passed":
        where.append("check_status = 'passed'" + (" AND NOT " + GRP_BAD_SQL if group_aware else ""))
    if viewer:
        account_scope = scope_clause(conn, viewer,
            "json_extract(p.payload,'$.来源群')", "json_extract(p.payload,'$.航线')")
        if account_scope:
            where.append(account_scope)
    return (("WHERE " + " AND ".join(where)) if where else ""), args


def group_stats(conn: sqlite3.Connection) -> Dict[Tuple[str, str], Dict[str, int]]:
    """按 (消息ID, kind) 汇总待审组：共几条、几条未过。纯派生，不写库、不改任何 check_status。"""
    out: Dict[Tuple[str, str], Dict[str, int]] = {}
    for r in conn.execute("SELECT id, kind, check_status, payload FROM pending_records"
                          " WHERE review_status='awaiting'"):
        try:
            mid = str(json.loads(r["payload"] or "{}").get("消息ID") or "").strip()
        except Exception:
            mid = ""
        key = (mid or ("row-" + str(r["id"])), r["kind"])
        s = out.setdefault(key, {"total": 0, "bad": 0})
        s["total"] += 1
        if r["check_status"] != "passed":
            s["bad"] += 1
    return out


def _sibling_map(conn: sqlite3.Connection) -> Dict[Tuple[str, str], str]:
    """待审「舱价同源」映射：(消息标识, kind) -> 同一条消息的另一 kind（rates<->space）。
    同消息同时存在运价和舱位条目才算同源（复合消息拆两条）；只统计 awaiting（待审卡场景）。
    前端据此在组头显示"舱价同源"标签并跳转定位。"""
    by_mid: Dict[str, set] = {}
    for r in conn.execute("SELECT id, kind, payload FROM pending_records"
                          " WHERE review_status='awaiting'"):
        try:
            mid = str(json.loads(r["payload"] or "{}").get("消息ID") or "").strip()
        except Exception:
            mid = ""
        by_mid.setdefault(mid or ("row-" + str(r["id"])), set()).add(r["kind"])
    out: Dict[Tuple[str, str], str] = {}
    for mid, kinds in by_mid.items():
        if "rates" in kinds and "space" in kinds:
            for k in kinds:
                out[(mid, k)] = "space" if k == "rates" else "rates"
    return out


def list_pending_sources(conn: sqlite3.Connection, status: str = "awaiting", kind: str = "",
                         limit: int = PENDING_SOURCE_MAX_GROUPS, offset: int = 0, check: str = "",
                         entry_id: str = "", message_id: str = "", id_query: str = "", viewer: str = "") -> PendingSourceListResult:
    """Filter and paginate source messages, then return every scoped row in each message.

    A failed filter selects messages with any failed row; passed selects messages whose
    rows all pass. Identity queries select a message without cutting its siblings away.
    All grouping and totals are derived; no validation or review status is rewritten.
    """
    scope, scope_args = _pending_where(status, kind, conn=conn, viewer=viewer)
    message_column = _normalized_message_id_sql("json_extract(p.payload,'$.消息ID')")
    source_column = _pending_source_key_sql("p")
    wanted = None
    if entry_id or message_id or id_query:
        selected, selected_args = _pending_where(status, kind, entry_id=entry_id,
                                                 message_id=message_id, id_query=id_query, conn=conn, viewer=viewer)
        wanted = {r[0] for r in conn.execute(
            "SELECT DISTINCT " + source_column + " FROM pending_records p " + selected, selected_args)}
    groups: Dict[str, Dict[str, int]] = {}
    for row in conn.execute("SELECT p.id," + source_column + " AS source_key,p.check_status "
                            "FROM pending_records p " + scope, scope_args):
        key = row["source_key"]
        if wanted is not None and key not in wanted:
            continue
        group = groups.setdefault(key, {"total": 0, "bad": 0, "last_id": 0})
        group["total"] += 1
        group["bad"] += int(row["check_status"] != "passed")
        group["last_id"] = max(group["last_id"], row["id"])
    keys = [key for key, group in groups.items()
            if (check != "failed" or group["bad"] > 0)
            and (check != "passed" or group["bad"] == 0)]
    keys.sort(key=lambda key: groups[key]["last_id"], reverse=True)
    page_size = max(1, min(limit, PENDING_SOURCE_MAX_GROUPS))
    page_keys = keys[max(0, offset):max(0, offset) + page_size]
    out = []
    if page_keys:
        extra = source_column + " IN (" + ",".join("?" for _ in page_keys) + ")"
        frag = scope + (" AND " if scope else "WHERE ") + extra
        rows = conn.execute("SELECT p.*," + source_column + " AS source_key FROM pending_records p "
                            + frag + " ORDER BY p.id DESC", [*scope_args, *page_keys]).fetchall()
        for row in rows:
            result = _pending_row(row)
            group = groups[row["source_key"]]
            result["group"] = {"total": group["total"], "bad": group["bad"]}
            out.append(result)
    return {"ok": True, "total": len(keys), "record_total": sum(groups[key]["total"] for key in keys),
            "pagination_unit": "groups", "rows": out}


def list_pending(conn: sqlite3.Connection, status: str = "awaiting", kind: str = "",
                 limit: int = 200, offset: int = 0, check: str = "",
                 group_aware: bool = False, entry_id: str = "", message_id: str = "", id_query: str = "", viewer: str = "",
                 whole_passed: bool = False) -> Dict[str, Any]:
    """待审列表默认逐条返回和筛选。

    ``group_aware`` 仅保留给旧的合并视图兼容调用；审核页不再把同源条目绑定为一张卡。
    """
    frag, args = _pending_where(status, kind, check, group_aware=group_aware, entry_id=entry_id, message_id=message_id, id_query=id_query, conn=conn, viewer=viewer)
    if whole_passed and status == "awaiting" and check == "passed":
        frag, args = _bulk_pending_where(conn, kind, viewer, entry_id=entry_id,
                                         message_id=message_id, id_query=id_query)
    total = conn.execute(f"SELECT COUNT(*) FROM pending_records p {frag}", args).fetchone()[0]
    rows = conn.execute(f"SELECT * FROM pending_records p {frag} "
                        f"ORDER BY check_status='failed' DESC, id DESC LIMIT ? OFFSET ?",
                        [*args, max(1, min(limit, 500)), max(0, offset)]).fetchall()
    out = annotate_publishers(conn, [_pending_row(r) for r in rows])
    if group_aware and out:
        stats = group_stats(conn)
        sib = _sibling_map(conn)
        for r in out:
            mid = str((r.get("payload") or {}).get("消息ID") or "").strip()
            g = stats.get((mid or ("row-" + str(r["id"])), r["kind"])) or {}
            r["group"] = {"total": g.get("total", 1), "bad": g.get("bad", 0)}
            r["sibling"] = sib.get((mid or ("row-" + str(r["id"])), r["kind"]))
    return {"total": total, "rows": out}


# ---------------- 待审核合并视图（展示层折叠，存储仍窄行） ----------------
# 设计：按 (消息ID, 船司, 起运港, 目的港, 船期ETD) 把同组窄行聚成一行，
# 柜型并排进 cab dict；每个柜型格带自己的 pending id，编辑/放行锚到格。
# 与 query_grouped 的聚合键一致，保证待审核页和销售端长得一样。
CTYPE_UI_ORDER = ("20GP", "40HQ/HC", "40NOR", "45HQ/HC")


def _pending_group_key(p: Dict[str, Any], kind: str) -> tuple:
    return (str(p.get("消息ID") or "").strip(), kind,
            str(p.get("船司") or "").strip(), str(p.get("起运港") or "").strip(),
            str(p.get("目的港") or "").strip(), str(p.get("船期ETD") or "").strip(),
            *(str(p.get(field) or "").strip() for field in QUOTE_VARIANT_FIELDS))


def _pending_group_match(gk: tuple, group_key: Dict[str, str], kind: str) -> bool:
    return (gk[0] == str(group_key.get("消息ID") or "").strip()
            and gk[1] == kind
            and gk[2] == str(group_key.get("carrier") or "").strip()
            and gk[3] == str(group_key.get("pol") or "").strip()
            and gk[4] == str(group_key.get("pod") or "").strip()
            and gk[5] == str(group_key.get("etd") or "").strip()
            and all(value == str(group_key.get(col) or "").strip()
                    for value, col in zip(gk[6:], QUOTE_VARIANT_FIELDS.values())))


def list_pending_grouped(conn: sqlite3.Connection, status: str = "awaiting", kind: str = "",
                         limit: int = 200, offset: int = 0, check: str = "", entry_id: str = "", message_id: str = "", id_query: str = "", viewer: str = "") -> Dict[str, Any]:
    """待审合并视图：一港一行、柜型并排。底层窄行不动，只做展示层聚合。"""
    frag, args = _pending_where(status, kind, check, group_aware=True, entry_id=entry_id, message_id=message_id, id_query=id_query, conn=conn, viewer=viewer)
    rows = conn.execute(
        f"SELECT * FROM pending_records p {frag} "
        f"ORDER BY check_status='failed' DESC, id DESC LIMIT 5000", args).fetchall()
    gmap: Dict[tuple, Dict[str, Any]] = {}
    for r in rows:
        d = _pending_row(r)
        p = d.get("payload") or {}
        gk = _pending_group_key(p, r["kind"])
        g = gmap.get(gk)
        if g is None:
            g = {
                "key": {"消息ID": gk[0], "kind": gk[1], "carrier": gk[2],
                        "pol": gk[3], "pod": gk[4], "etd": gk[5],
                        **dict(zip(QUOTE_VARIANT_FIELDS.values(), gk[6:]))},
                "meta": {
                    "来源群": p.get("来源群") or "", "发送人": p.get("发送人") or "",
                    "消息时间": p.get("消息时间") or "",
                    "有效期起": str(p.get("有效期起") or "")[:10],
                    "有效期止": str(p.get("有效期止") or "")[:10],
                    "船名航次": p.get("船名航次") or "",
                    "备注": p.get("备注") or "",
                    "图片名": p.get("图片名") or p.get("运价图片") or "",
                    "航线": p.get("航线") or "",
                },
                "cab": {},
            }
            gmap[gk] = g
        ct = norm_ctype(p.get("柜型"))
        price = p.get("海运费USD") if r["kind"] == "rates" else p.get("价格USD")
        g["cab"][ct] = {
            "id": d["id"],
            "price": price,
            "check_status": d["check_status"],
            "errors": d.get("errors") or [],
        }
    # 固定柜型位序：20GP → 40HQ/HC → 40NOR → 45HQ/HC，缺的不出现（前端留空）
    glist = []
    for g in gmap.values():
        ordered = {ct: g["cab"][ct] for ct in CTYPE_UI_ORDER if ct in g["cab"]}
        # 兜底：识别出的非四档柜型也带上（不应出现，但不丢）
        for ct, v in g["cab"].items():
            if ct not in ordered:
                ordered[ct] = v
        g["cab"] = ordered
        g["has_failed"] = any(c["check_status"] != "passed" for c in g["cab"].values())
        glist.append(g)
    glist.sort(key=lambda g: (0 if g["has_failed"] else 1,
                              -max(c["id"] for c in g["cab"].values())))
    total = len(glist)
    return {"total": total, "groups": glist[offset:offset + limit]}


def _find_group_pids(conn: sqlite3.Connection, group_key: Dict[str, str], kind: str) -> List[int]:
    rows = conn.execute(
        "SELECT id, payload, check_status FROM pending_records "
        "WHERE kind=? AND review_status='awaiting'", (kind,)).fetchall()
    out = []
    for r in rows:
        try:
            p = json.loads(r["payload"] or "{}")
        except Exception:
            continue
        if _pending_group_match(_pending_group_key(p, kind), group_key, kind):
            out.append((r["id"], r["check_status"]))
    return out


def update_pending_group(conn: sqlite3.Connection, master: "Master", group_key: Dict[str, str],
                         fields: Dict[str, Any], kind: str = "rates",
                         actor: str = "") -> Dict[str, Any]:
    """改合并行的整组共享字段：循环组内每个柜型格调 update_pending。
    价格字段不要走这里（每格不同，走单格 update）。"""
    matched = _find_group_pids(conn, group_key, kind)
    results = []
    for pid, _ in matched:
        res = update_pending(conn, master, pid, fields, kind=kind, actor=actor, commit=False)
        results.append({"id": pid, "ok": res.get("ok"),
                        "check_status": res.get("check_status"),
                        "rejected": res.get("rejected"), "errors": res.get("errors")})
    conn.commit()
    return {"total": len(matched), "results": results}


def approve_pending_group(conn: sqlite3.Connection, master: "Master", group_key: Dict[str, str],
                          kind: str = "rates", reviewed_by: str = "", viewer: str = "") -> Dict[str, Any]:
    """放行合并行内所有 check_status=passed 的柜型格。"""
    matched = _find_group_pids(conn, group_key, kind)
    approved, skipped = [], []
    for pid, cs in matched:
        if cs == "passed":
            res = approve_pending(conn, master, pid, reviewed_by=reviewed_by, viewer=viewer)
            approved.append({"id": pid, "res": res})
        else:
            skipped.append({"id": pid, "check_status": cs})
    return {"approved": approved, "skipped": skipped}


def approve_pending_ids(conn: sqlite3.Connection, master: "Master", ids: Iterable[Any],
                        kind: str = "", reviewed_by: str = "", viewer: str = "") -> Dict[str, Any]:
    """按前端明确给出的待审 ID 批量放行。

    待审核页面按消息聚合展示，一条消息可同时有不同目的港/ETD。旧的组键会用首条
    记录二次过滤，导致卡片显示“全部放行 N 条”但实际只放行其中一部分；这里以已渲染
    且已通过的 ID 为唯一范围，仍逐条经过放行前复核闸门。
    """
    wanted: List[int] = []
    seen = set()
    for raw in ids or []:
        try:
            pid = int(raw)
        except (TypeError, ValueError):
            continue
        if pid > 0 and pid not in seen:
            seen.add(pid)
            wanted.append(pid)
    approved, skipped = [], []
    for pid in wanted:
        row = conn.execute("SELECT kind, review_status, check_status FROM pending_records WHERE id=?", (pid,)).fetchone()
        if not row:
            skipped.append({"id": pid, "error": "not found"})
            continue
        if kind and row["kind"] != kind:
            skipped.append({"id": pid, "error": "类型不匹配"})
            continue
        if row["kind"] not in ("rates", "space"):
            skipped.append({"id": pid, "error": "该类型不支持批量放行"})
            continue
        if row["review_status"] != "awaiting" or row["check_status"] != "passed":
            skipped.append({"id": pid, "error": "已处理或校验未通过"})
            continue
        res = approve_pending(conn, master, pid, reviewed_by=reviewed_by, viewer=viewer)
        approved.append({"id": pid, "res": res})
    return {"total": len(wanted), "approved": approved, "skipped": skipped}


def reject_pending_group(conn: sqlite3.Connection, group_key: Dict[str, str],
                         kind: str = "rates", note: str = "", reviewed_by: str = "") -> Dict[str, Any]:
    """驳回合并行内所有柜型格。"""
    matched = _find_group_pids(conn, group_key, kind)
    return reject_pending_ids(conn, [pid for pid, _ in matched], kind, note, reviewed_by)


def reject_pending_ids(conn: sqlite3.Connection, ids: Iterable[int],
                       kind: str = "rates", note: str = "", reviewed_by: str = "") -> Dict[str, Any]:
    """Reject only explicit pending IDs; preserve payloads and check results."""
    rejected, skipped = [], []
    for pid in dict.fromkeys(ids):
        row = conn.execute("SELECT kind FROM pending_records WHERE id=?", (pid,)).fetchone()
        if not row or row["kind"] != kind:
            skipped.append({"id": pid, "error": "not found/类型不匹配"})
            continue
        result = reject_pending(conn, pid, note=note, reviewed_by=reviewed_by)
        if result["ok"]:
            rejected.append(pid)
        else:
            skipped.append({"id": pid, "error": result["error"]})
    return {"rejected": rejected, "skipped": skipped}


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
                    reviewed_by: str = "", viewer: str = "", commit: bool = True) -> Dict[str, Any]:
    lock = " FOR UPDATE" if getattr(conn, "engine", "sqlite") == "postgres" else ""
    row = conn.execute("SELECT * FROM pending_records WHERE id = ?" + lock, (pid,)).fetchone()
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
        if commit:
            conn.commit()
        return {"ok": True, "kind": GSUG_KIND, "result": res,
                "registered": {"group": pl["港群"], "added": added}}
    # 单事务放行（2026-09-17 审查修复）：业务写入 + pending 状态 + edit_log + 观察流候选
    # 一次 commit。此前 push_with_review 内部逐条 commit + 状态二次 commit，
    # 崩溃窗内业务表已写但 pending 仍 awaiting → 重复放行重复入库。
    res = push_with_review(conn, master, row["kind"], [json.loads(row["payload"])],
                           origin=row["origin"], commit=False, entry_id=row["entry_id"], viewer=viewer,
                           trusted_publisher_id=row['publisher_id'])
    if res["rejected"]:
        if not commit:
            return {"ok": False, "error": "放行前校验未通过", "errors": res["rejected"][0]["errors"]}
        conn.rollback()
        errs = res["rejected"][0]["errors"]
        # 放行时重校验没过 = 那条"已通过"徽标是过期的；就地刷成真实状态，免得它永远卡在可放行列表里
        conn.execute("UPDATE pending_records SET check_status='failed', errors=? WHERE id=?",
                     (json.dumps(errs, ensure_ascii=False), pid))
        conn.commit()
        return {"ok": False, "error": "放行前校验未通过", "errors": errs}
    cur = conn.execute("UPDATE pending_records SET review_status='approved', final_key=?, "
                       "reviewed_at=?, reviewed_by=? WHERE id=? AND review_status='awaiting'",
                       (",".join(res["keys"]) or None, now_cn(), reviewed_by, pid))
    if not cur.rowcount:
        # 乐观锁：读到 awaiting 后被并发请求抢先处理 → 回滚本次业务写入，绝不重复入库
        if commit:
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
    if commit:
        conn.commit()          # Bulk caller commits or rolls back the complete source.
    # 来源群/发送人的登记已在 push_with_review 内完成；这里还原成前端要的"单条"形状用于放行提示
    reg = res.get("registered") or {}
    g = str(payload.get("来源群") or payload.get("source_group") or "").strip()
    s = str(payload.get("发送人") or payload.get("sender") or "").strip()
    registered = {"group": g or None, "sender": s or None,
                  "group_added": bool(g) and g in (reg.get("added_groups") or []),
                  "sender_added": bool(s) and s in (reg.get("added_senders") or [])}
    return {"ok": True, "kind": row["kind"], "entry_id": row["entry_id"], "result": res, "registered": registered,
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
    """Compatibility hook: approval itself creates no permanent alias suggestions."""
    # approve_diff is an original/final snapshot, not proof of a human edit.
    # Edits already remain in edit_log. Independent human-source learning runs
    # through scan_audit_corrections, never as a side effect of approval.
    return 0


def approve_all_pending(conn: sqlite3.Connection, master: "Master", kind: str = "",
                        reviewed_by: str = "", viewer: str = "") -> PendingBulkApprovalResult:
    """Release only wholly passed source messages, atomically per source."""
    frag, args = _bulk_pending_where(conn, kind, viewer)
    source = _pending_source_key_sql("p")
    groups: Dict[str, List[int]] = {}
    for row in conn.execute("SELECT p.id," + source + " AS source_key FROM pending_records p "
                            + frag + " ORDER BY p.id", args):
        groups.setdefault(row["source_key"], []).append(row["id"])
    conn.commit()
    approved, approved_ids, skipped = 0, [], []
    for key, ids in groups.items():
        try:
            lock = " FOR UPDATE" if getattr(conn, "engine", "sqlite") == "postgres" else ""
            if not lock:
                conn.execute("BEGIN IMMEDIATE")
            rows = conn.execute("SELECT p.id,p.kind,p.check_status FROM pending_records p"
                                " WHERE p.review_status='awaiting' AND p.kind IN ('rates','space')"
                                " AND " + source + "=? ORDER BY p.id" + lock, (key,)).fetchall()
            target_ids = {r["id"] for r in rows if kind not in ("rates", "space") or r["kind"] == kind}
            if target_ids != set(ids) or any(r["check_status"] != "passed" for r in rows):
                conn.rollback()
                skipped.extend({"id": pid, "msg": "整组状态或范围已变化，整组未放行"} for pid in ids)
                continue
            failed = None
            for pid in ids:
                result = approve_pending(conn, master, pid, reviewed_by or "一键放行",
                                         viewer=viewer, commit=False)
                if not result.get("ok"):
                    failed = (pid, result)
                    break
            if failed:
                conn.rollback()
                pid, result = failed
                if result.get("errors"):
                    conn.execute("UPDATE pending_records SET check_status='failed', errors=?"
                                 " WHERE id=? AND review_status='awaiting'",
                                 (json.dumps(result["errors"], ensure_ascii=False), pid))
                    conn.commit()
                reason = "整组未放行：" + str(result.get("error") or "放行失败")
                skipped.extend({"id": item, "msg": reason} for item in ids)
                continue
            conn.commit()
            approved += len(ids)
            approved_ids.extend(ids)
        except Exception:
            conn.rollback()
            raise
    eligible_where, eligible_args = _bulk_pending_where(conn, kind, viewer)
    held_where, held_args = _bulk_pending_where(conn, kind, viewer, eligible=False)
    remaining = conn.execute("SELECT COUNT(*) FROM pending_records p " + eligible_where,
                             eligible_args).fetchone()[0]
    held = conn.execute("SELECT COUNT(*) FROM pending_records p " + held_where, held_args).fetchone()[0]
    return {"ok": True, "total": sum(len(ids) for ids in groups.values()), "approved": approved,
            "approved_ids": approved_ids,
            "skipped": skipped, "remaining_passed": remaining, "held_by_group": held}


# 强匹配字段：闸门在这几个字段上报错 = 提交的值在主数据里对不上号（多半是手打/输入一半）。
# 这类值不允许覆盖暂存记录里的原值——否则随手两个字母就把审核过的正确信息冲掉，事后也看不出来。
STRICT_KEEP_FIELDS = ("起运港", "目的港", "船司", "航线", "柜型", "舱位类型")


def _strict_rejects(patch: Dict[str, Any], orig: Dict[str, Any],
                    v: Dict[str, Any]) -> List[Dict[str, Any]]:
    """闸门报在强匹配字段上、且这次确实改动了该字段 → 要求回退保留原值。
    只改动的字段回退：原本就脏、这次没碰的值不背锅，也绝不能被顺手抹成空。"""
    out: List[Dict[str, Any]] = []
    for e in (v.get("errors") or []):
        f = str(e.get("field") or "")
        if f not in STRICT_KEEP_FIELDS or f not in patch:
            continue
        typed = patch.get(f)
        if typed is None or str(orig.get(f, "")) == str(typed):
            continue
        out.append({"field": f, "input": typed, "kept": orig.get(f) or "",
                    "msg": str(e.get("msg") or "")})
    return out


def update_pending(conn: sqlite3.Connection, master: "Master", pid: int,
                   fields: Dict[str, Any], kind: Optional[str] = None,
                   actor: str = "", commit: bool = True,
                   sync_from: Optional[int] = None, preserve_unresolved: bool = False) -> Dict[str, Any]:
    """审核者就地编辑暂存记录：重跑机械校验 + 重判新增/更新，回写 payload 与校验态。
    编辑后 check_status 实时翻转（failed 修好变 passed，放行解禁）。
    kind 可传入以在运价/舱位间互切（卡片右上角切换），校验/必填随之改判。
    强匹配字段（港口/船司/航线/柜型/舱位类型）提交值对不上主数据 → 该字段回退不写，
    其余字段照常保存，被回退的字段在返回值的 rejected 里报给前端。
    P0 审计：每次字段修改追加 edit_log {ts,actor,action,field,old,new}，供反向沉淀扫描。
    commit=False：批量同步用（多条改动收进一个事务，中途异常整体不生效）。
    sync_from：标记这次改动是从哪条锚点条目同步来的，只作溯源、不进扫描口径。"""
    row = conn.execute("SELECT * FROM pending_records WHERE id = ?", (pid,)).fetchone()
    if not row:
        return {"ok": False, "error": "not found"}
    if row["review_status"] != "awaiting":
        return {"ok": False, "error": "该条已处理，不能编辑"}
    if row["kind"] == GSUG_KIND:
        # 港群候选也需要保留原文规范名可编辑；不能走 rates/space 的 validate_record。
        allowed = {"港群", "标签", "类型", "成员"}
        patch = {k: v for k, v in (fields or {}).items() if v is not None and k in allowed}
        if set(fields or {}) - allowed:
            return {"ok": False, "error": "港群候选只能编辑：港群、标签、类型、成员"}
        base = json.loads(row["payload"] or "{}")
        trial = dict(base); trial.update(patch)
        v = validate_group_suggestion(conn, trial)
        edits = []
        for k, val in patch.items():
            if str(base.get(k, "")) != str(val):
                edits.append({"ts": now_cn(), "actor": actor or "unknown", "action": "update",
                              "field": k, "old": base.get(k), "new": val})
        conn.execute("UPDATE pending_records SET payload=?, resolved=?, action=?, target_key=?, check_status=?, errors=?, warnings=? WHERE id=?",
                     (json.dumps(trial, ensure_ascii=False), json.dumps(v["resolved"], ensure_ascii=False),
                      "update" if v["resolved"].get("动作") == "补充成员" else "new", "",
                      "passed" if v["ok"] else "failed", json.dumps(v["errors"], ensure_ascii=False), "[]", pid))
        _append_edit_log(conn, pid, edits)
        if commit: conn.commit()
        return {"ok": True, "id": pid, "kind": GSUG_KIND,
                "check_status": "passed" if v["ok"] else "failed", "action": "update" if v["resolved"].get("动作") == "补充成员" else "new",
                "resolved": v["resolved"], "errors": v["errors"], "warnings": [], "edits_recorded": len(edits), "rejected": []}
    kind = kind if kind in ("rates", "space") else row["kind"]
    # 以旧 payload 为底，覆盖编辑过的中文键（保留未改字段，如图片引用/消息ID）
    base = json.loads(row["payload"] or "{}")
    orig = dict(base)
    if any(key in fields and fields[key] != base.get(key) for key in SOURCE_FIELDS):
        return {"ok": False, "error": "钉钉来源标识不可编辑，请在账号报价身份页绑定归属"}
    patch = {k: val for k, val in (fields or {}).items() if val is not None}
    trial = dict(base)
    trial.update(patch)
    v = validate_record(conn, master, kind, trial)
    rejected = [] if preserve_unresolved else _strict_rejects(patch, orig, v)
    if rejected:                      # 回退强匹配字段后复算：这一条其余的改动仍然要落库
        keep = {str(r["field"]) for r in rejected}
        patch = {k: val for k, val in patch.items() if k not in keep}
        trial = dict(base)
        trial.update(patch)
        v = validate_record(conn, master, kind, trial)
    # P0 审计：按最终真正写入的 patch 算字段级 diff（被回退的不算改动、不留痕）
    edits: List[Dict[str, Any]] = []
    for k, val in patch.items():
        if str(orig.get(k, "")) != str(val):
            edits.append({"ts": now_cn(), "actor": actor or "unknown", "action": "update",
                          "field": k, "old": orig.get(k), "new": val,
                          **({"sync_from": sync_from} if sync_from else {})})
    if kind != row["kind"]:
        edits.append({"ts": now_cn(), "actor": actor or "unknown", "action": "update",
                      "field": "kind", "old": row["kind"], "new": kind})
    rec = trial
    action, target = classify_new_or_update(conn, kind, rec, v["resolved"])
    # Refresh the route hint only; preserve unrelated batch coverage warnings.
    warnings = [warning for warning in json.loads(row["warnings"] or "[]")
                if not isinstance(warning, dict) or warning.get("field") != "航线归属"]
    warnings.extend(v["warnings"])
    conn.execute(
        "UPDATE pending_records SET kind=?, payload=?, resolved=?, action=?, target_key=?, "
        "check_status=?, errors=?, warnings=? WHERE id=?",
        (kind, json.dumps(rec, ensure_ascii=False), json.dumps(v["resolved"], ensure_ascii=False),
         action, target, "passed" if v["ok"] else "failed",
         json.dumps(v["errors"], ensure_ascii=False), json.dumps(warnings, ensure_ascii=False), pid))
    _append_edit_log(conn, pid, edits)
    if commit:
        conn.commit()
    return {"ok": True, "id": pid, "kind": kind,
            "check_status": "passed" if v["ok"] else "failed",
            "action": action, "target_key": target, "resolved": v["resolved"], "errors": v["errors"], "warnings": warnings,
            "edits_recorded": len(edits), "rejected": rejected}


# ---------------- 待审核队列·批量同步（改一条 → 同问题的其他条目一起改） ----------------
# 「同一个问题」的判定口径：同一类目 + 同一字段 + 该字段当前值完全相同（含都没填=空串）。
# 只放行口径类字段：海运费/价格/亏舱费/箱量/T-T/目免/ETD/备注/消息原文一律不参与同步——
#   这些是每票独立事实，值相同不代表同一个错误；消息原文更是逐字溯源、禁止批量改写。
#   有效期例外：一条消息里的整版报价常常共用同一个区间，识图错一位就是整批同错，故按「起|止」成对同步。
SYNC_SAFE_FIELDS = ("船司", "起运港", "目的港", "航线", "柜型", "舱位类型",
                    "船名航次", "来源群", "发送人", "有效期")
MAX_SYNC = 500        # 单批上限（与队列页 50/页相比已足够宽）；超限要求先收窄筛选，防手滑刷全库
# 复合字段：界面上一个控件、payload 里两个键，必须成对同步（只刷起点会刷出「起晚于止」）。
# 值统一编码成 "起|止"，比较时只取到日，容忍 "2026-09-10" 与 "2026-09-10 00:00" 两种写法。
SYNC_KEYS = {"有效期": ("有效期起", "有效期止")}


def _sync_value(rec: Dict[str, Any], field: str) -> str:
    """取 payload 里的字段现值做比对：统一 strip，缺失/空值都当空串（"都没填"也是一种同一个问题）。
    复合字段（有效期）取 "起|止" 两截。"""
    keys = SYNC_KEYS.get(field)
    if keys:
        return "|".join(str(rec.get(k) or "").strip()[:10] for k in keys)
    return str(rec.get(field) or "").strip()


def _sync_payload_fields(field: str, value: str) -> Dict[str, Any]:
    """把一个同步值还原成要写进 payload 的键值对（复合字段拆两键，缺的一段按空串写）。"""
    keys = SYNC_KEYS.get(field)
    if not keys:
        return {field: value}
    parts = (value.split("|") + ["", ""])[:2]
    return {k: parts[i].strip() for i, k in enumerate(keys)}


def _sync_value_error(field: str, value: str) -> str:
    """新值自身的合法性（闸门只管单字段枚举，管不到区间顺序）：起晚于止直接拒。"""
    if field not in SYNC_KEYS:
        return ""
    parts = value.split("|")
    a, b = (parts + ["", ""])[:2]
    if a and b and a > b:
        return f"有效期起始({a})晚于结束({b})，不批量下发"
    return ""


def _sync_summary(rec: Dict[str, Any], kind: str) -> Dict[str, Any]:
    """候选行摘要：够审核员一眼认出"这条是不是我要改的那批"，不带原文（原文太长且逐字不许改）。"""
    return {
        "来源群": rec.get("来源群") or "", "发送人": rec.get("发送人") or "",
        "船司": rec.get("船司") or "", "起运港": rec.get("起运港") or "",
        "目的港": rec.get("目的港") or "", "柜型": rec.get("柜型") or "",
        "舱位类型": rec.get("舱位类型") or "", "航线": rec.get("航线") or "",
        "有效期起": rec.get("有效期起") or "", "有效期止": rec.get("有效期止") or "",
        "价格": rec.get("海运费USD") if kind == "rates" else rec.get("价格USD") or "",
        "消息ID": rec.get("消息ID") or "",
    }


def _sync_source_identity(row) -> tuple:
    """Use archived source identity so editing source labels cannot widen a sync."""
    original = json.loads(row["orig_payload"] or row["payload"] or "{}")
    message = normalize_identity_query(original.get("消息ID") or "")
    return (row["kind"], str(original.get("来源群") or "").strip(),
            message or f"row-{row['id']}")


def _sync_anchor(conn, anchor_id: int, viewer: str = ""):
    frag, args = _pending_where("awaiting", conn=conn, viewer=viewer)
    return conn.execute("SELECT p.* FROM pending_records p " + frag + " AND p.id=?",
                        [*args, int(anchor_id or 0)]).fetchone()


def scan_sync_targets(conn: sqlite3.Connection, field: str, old: str, new: str,
                      kind: str = "", status: str = "awaiting", check: str = "",
                      exclude_id: int = 0, viewer: str = "") -> Dict[str, Any]:
    """只扫描锚点原始消息组内「该字段现值 == old」的待审条目。只读不改。
    kind 固定与锚点同类目（运价/舱位字段集不同，跨类目同步会往对方 payload 塞无意义键）。
    返回的 ids 就是执行侧要传的 ids —— 预览即所得，中途新到的条目不在其列。"""
    if field not in SYNC_SAFE_FIELDS:
        return {"ok": False, "error": f"字段「{field}」不参与批量同步",
                "allowed": list(SYNC_SAFE_FIELDS)}
    anchor = _sync_anchor(conn, exclude_id, viewer)
    if not anchor:
        return {"ok": False, "error": "当前条目不存在、已处理或无权访问，不能同步"}
    identity = _sync_source_identity(anchor)
    kind = anchor["kind"]
    frag, args = _pending_where(status or "awaiting", kind, check, conn=conn, viewer=viewer)
    want, rows = str(old or "").strip(), []
    source = "COALESCE(NULLIF(p.orig_payload,''),p.payload)"
    if getattr(conn, "engine", "sqlite") == "postgres":
        message_value = f"(CAST({source} AS JSONB)->>'消息ID')"
        group_value = f"(CAST({source} AS JSONB)->>'来源群')"
    else:
        message_value = f"json_extract({source},'$.消息ID')"
        group_value = f"json_extract({source},'$.来源群')"
    message = _normalized_message_id_sql(message_value)
    message = f"COALESCE(NULLIF({message},''),'row-'||p.id)"
    group = f"TRIM(COALESCE({group_value},''))"
    cur = conn.execute(f"SELECT id,kind,payload,orig_payload,check_status,created_at FROM pending_records p "
                       f"{frag}{' AND ' if frag else 'WHERE '}id != ?"
                       f" AND {message}=? AND {group}=? ORDER BY id DESC",
                       [*args, int(exclude_id or 0), identity[2], identity[1]])
    scanned = truncated = 0
    for r in cur:
        scanned += 1
        if scanned > MAX_SYNC * 4:      # 扫描行数硬上限：待审队列堆积到异常量级时截断报告，不无限扫
            truncated = True
            break
        try:
            rec = json.loads(r["payload"] or "{}")
        except Exception:
            continue                    # payload 坏行不参与同步（本来也放不了行），交由体检暴露
        if _sync_source_identity(r) != identity:
            continue
        if _sync_value(rec, field) != want:
            continue
        rows.append({"id": r["id"], "kind": r["kind"], "check_status": r["check_status"],
                     "created_at": r["created_at"], "summary": _sync_summary(rec, r["kind"])})
    over = len(rows) > MAX_SYNC
    return {"ok": True, "field": field, "old": want, "new": str(new or "").strip(),
            "kind": kind, "matched": len(rows), "ids": [x["id"] for x in rows][:MAX_SYNC],
            "rows": rows[:MAX_SYNC], "truncated": truncated or over}


def sync_pending_field(conn: sqlite3.Connection, master: "Master", field: str,
                       old: str, new: str, ids: List[Any], actor: str = "",
                       anchor_id: int = 0, viewer: str = "") -> Dict[str, Any]:
    """把锚点上的一次字段修改，同步到审核员确认过的那批同问题条目。
    逐条复用 update_pending（闸门重校验 + 重判新增/更新 + edit_log 溯源 sync_from 全走同一条逻辑），
    commit=False 收进一个事务：中途任何异常 → 整批不生效。
    只改值不放行：条目仍留在待审队列，放行永远是人工动作。
    预览后被别人放行/改过值的条目显式跳过并回报，绝不覆盖。"""
    if field not in SYNC_SAFE_FIELDS:
        return {"ok": False, "error": f"字段「{field}」不参与批量同步",
                "allowed": list(SYNC_SAFE_FIELDS)}
    anchor = _sync_anchor(conn, anchor_id, viewer)
    if not anchor:
        return {"ok": False, "error": "当前条目不存在、已处理或无权访问，不能同步"}
    identity = _sync_source_identity(anchor)
    new = str(new or "").strip()
    patch = _sync_payload_fields(field, new)
    if not any(str(v or "").strip() for v in patch.values()):
        return {"ok": False, "error": "同步不接受把一批条目改成空值（批量清空请逐条处理）"}
    bad = _sync_value_error(field, new)
    if bad:
        return {"ok": False, "error": bad}
    want = str(old or "").strip()
    ids = [int(i) for i in (ids or []) if str(i).strip().isdigit()]
    if not ids:
        return {"ok": False, "error": "没有要同步的条目"}
    if len(ids) > MAX_SYNC:
        return {"ok": False, "error": f"一次最多同步 {MAX_SYNC} 条，请先用类目/校验筛选收窄"}
    synced: List[Dict[str, Any]] = []
    skipped: List[Dict[str, Any]] = []
    flipped: List[int] = []
    try:
        for pid in ids:
            row = _sync_anchor(conn, pid, viewer)
            if not row:
                skipped.append({"id": pid, "reason": "条目不存在、已处理或无权访问"})
                continue
            if pid == anchor_id or _sync_source_identity(row) != identity:
                skipped.append({"id": pid, "reason": "不属于当前消息组的相关条目"})
                continue
            if row["review_status"] != "awaiting":
                skipped.append({"id": pid, "reason": "已被处理（非待审）"})
                continue
            rec = json.loads(row["payload"] or "{}")
            now_val = _sync_value(rec, field)
            if now_val != want:
                skipped.append({"id": pid, "reason": f"值已变（现「{now_val or '空'}」）"})
                continue
            res = update_pending(conn, master, pid, patch, kind=None,
                                 actor=actor, commit=False, sync_from=anchor_id or None)
            if not res.get("ok"):
                skipped.append({"id": pid, "reason": res.get("error") or "更新失败"})
                continue
            if res.get("rejected"):     # 值对不上主数据 → 服务端已回退，这条其实没改动
                rj = res["rejected"][0]
                skipped.append({"id": pid, "reason": "%s「%s」不在主数据，已保留原值未改"
                                % (rj.get("field"), rj.get("input"))})
                continue
            synced.append({"id": pid, "check_status": res["check_status"]})
            if res["check_status"] != "passed":
                flipped.append(pid)     # 同步后闸门判未过：值已存但放行按钮解禁，需人工再修
        conn.commit()                   # 事务边界：整批生效或（异常时）整批不生效
    except Exception as e:
        conn.rollback()
        return {"ok": False, "error": f"批量同步已回滚：{e}", "rolled_back": True}
    return {"ok": True, "field": field, "old": want, "new": new, "anchor_id": anchor_id,
            "synced": len(synced), "skipped": skipped,
            "failed_check_ids": flipped,
            "error": None if synced or skipped else "没有条目被改动"}


def _record_pending_rejection_learning(conn: sqlite3.Connection, row: "sqlite3.Row",
                                       note: str, reviewed_by: str) -> None:
    """Persist the human disposition independently of the pending row."""
    payload = json.loads(row["payload"] or "{}")
    group = str(payload.get("来源群") or "未分组").strip()
    message_id = str(payload.get("消息ID") or row["entry_id"]).strip()
    reason = str(note or "").strip()
    classification = "rework" if reason else "noise"
    reason_text = reason or "人工标记：垃圾条目；后续不要生成报价"
    reason_text = "pending_rejection:%s｜%s" % (classification, reason_text)
    timestamp = now_cn()
    existing = conn.execute(
        "SELECT reasons FROM review_issues WHERE source_group=? AND message_id=?", (group, message_id)
    ).fetchone()
    reasons = []
    if existing:
        try:
            reasons = json.loads(existing["reasons"] or "[]")
        except (TypeError, ValueError):
            reasons = []
    if reason_text not in reasons:
        reasons.append(reason_text)
    media_ids = payload.get("图片ID") or payload.get("图片IDs") or payload.get("media_ids") or []
    if not isinstance(media_ids, list):
        media_ids = [str(media_ids)] if media_ids else []
    if existing:
        conn.execute("UPDATE review_issues SET message_text=?,media_ids=?,reasons=?,status='open',"
                     "updated_at=?,resolved_by=NULL,resolved_at=NULL WHERE source_group=? AND message_id=?",
                     (str(payload.get("消息原文") or ""), json.dumps(media_ids, ensure_ascii=False),
                      json.dumps(reasons, ensure_ascii=False), timestamp, group, message_id))
    else:
        conn.execute("INSERT INTO review_issues(source_group,message_id,message_text,media_ids,reasons,"
                     "status,created_at,updated_at,resolved_by,resolved_at) VALUES(?,?,?,?,?,'open',?,?,NULL,NULL)",
                     (group, message_id, str(payload.get("消息原文") or ""),
                      json.dumps(media_ids, ensure_ascii=False), json.dumps(reasons, ensure_ascii=False),
                      timestamp, timestamp))


def reject_pending(conn: sqlite3.Connection, pid: int, note: str = "",
                   reviewed_by: str = "") -> Dict[str, Any]:
    """Soft reject; empty note is a durable noise signal, non-empty note is rework input."""
    row = conn.execute("SELECT * FROM pending_records WHERE id=? AND review_status='awaiting'", (pid,)).fetchone()
    if not row:
        return {"ok": False, "error": "not found/已处理"}
    clean_note = str(note or "").strip()
    action = "reject_rework" if clean_note else "reject_noise"
    cur = conn.execute(
        "UPDATE pending_records SET review_status='rejected', note=?, reviewed_at=?, reviewed_by=? "
        "WHERE id=? AND review_status='awaiting'", (clean_note, now_cn(), reviewed_by, pid))
    if cur.rowcount:
        _append_edit_log(conn, pid, [{"ts": now_cn(), "actor": reviewed_by or "unknown",
                                      "action": action, "field": "", "old": None, "new": clean_note}])
        _record_pending_rejection_learning(conn, row, clean_note, reviewed_by)
    conn.commit()
    return {"ok": True, "classification": "rework" if clean_note else "noise"} if cur.rowcount else {"ok": False, "error": "not found/已处理"}


REWORK_MAX_ROUNDS = 2          # 同一行最多回炉几轮，超了只交人工，避免"改了不过→再改"无限刷配额
REWORK_CORE_FIELDS = {"起运港", "目的港", "船司", "航线", "柜型", "海运费USD", "价格USD",
                      "有效期船期", "船名航次", "箱量", "舱位类型", "备注"}


def rework_allowed_fields(row: "sqlite3.Row") -> set:
    """可改字段白名单：校验错在哪就只许改哪；驳回原因里用「」点名的字段也算。"""
    ok = set()
    try:
        for e in json.loads(row["errors"] or "[]"):
            # 另一会话的"组完整性"连坐标记不是真实字段错误（它没有 field），
            # 既不能当可改字段，也不能因为它在就把白名单判空。
            if GROUP_MARK in str(e.get("msg") or "") or str(e.get("field") or "") == GROUP_MARK:
                continue
            f = str(e.get("field") or "").strip()
            if f in REWORK_CORE_FIELDS:
                ok.add(f)
    except (ValueError, TypeError):
        pass
    try:
        for m in re.findall(r"「(.+?)」", row["note"] or ""):
            if m.strip() in REWORK_CORE_FIELDS:
                ok.add(m.strip())
    except TypeError:
        pass
    return ok


def rework_pending(conn: sqlite3.Connection, master: "Master", pid: int,
                   fields: Dict[str, Any], reason: str = "",
                   actor: str = "采集agent") -> Dict[str, Any]:
    """驳回回炉：被驳回的行由 agent 按原因改一版，重过闸门后回到人工队列复核。

    与 reject/restore 的关系：reject 是"带原因退回"，本函数是"改了再交回来"，
    approve/reject 仍是人工的权力，agent 在这里只能改字段与留痕，绝不落业务表。
    三条护栏：字段白名单（见 rework_allowed_fields）、轮次上限、改不过退回 rejected。
    """
    row = conn.execute("SELECT * FROM pending_records WHERE id = ?", (pid,)).fetchone()
    if not row:
        return {"ok": False, "error": "not found"}
    if row["review_status"] != "rejected":
        return {"ok": False, "error": "只有已驳回的行能返修（现态 %s）" % row["review_status"]}
    if not str(row["note"] or "").strip():
        return {"ok": False, "error": "该条已标记为垃圾条目，不进入回炉"}
    try:
        log_list = json.loads(row["edit_log"] or "[]")
    except ValueError:
        log_list = []
    rounds = sum(1 for e in log_list if isinstance(e, dict) and e.get("action") in ("rework", "rework_failed"))
    if rounds >= REWORK_MAX_ROUNDS:
        return {"ok": False, "error": "已返修 %d 轮仍不过，请到「驳回日志」里撤回后人工改" % rounds}
    allowed = rework_allowed_fields(row)
    if not allowed:
        return {"ok": False, "error": "该行没有可定位的字段错误（驳回原因里请用「字段名」点明要改哪个）"}
    patch = {k: v for k, v in (fields or {}).items() if v is not None and str(v).strip() != ""}
    if not patch:
        return {"ok": False, "error": "fields 为空"}
    out = {k: v for k, v in patch.items() if k not in allowed}
    if out:
        return {"ok": False, "error": "只能改驳回原因指向的字段 %s，别的一律不动" % "、".join(sorted(allowed)),
                "rejected_fields": sorted(out)}
    base = json.loads(row["payload"] or "{}")
    reject_note = row["note"] or ""
    conn.execute("UPDATE pending_records SET review_status='awaiting' WHERE id=?", (pid,))
    res = update_pending(conn, master, pid, patch, actor=actor, commit=False)
    if not res.get("ok"):
        conn.execute("UPDATE pending_records SET review_status='rejected' WHERE id=?", (pid,))
        conn.commit()
        return {"ok": False, "error": res.get("error") or "编辑失败", "back": "rejected"}
    after = json.loads(conn.execute(
        "SELECT payload FROM pending_records WHERE id=?", (pid,)).fetchone()[0] or "{}")
    # 真实改动以落库后的 payload 为准：update_pending 会把主数据查无此值的强匹配字段回退掉，
    # 所以"提交了但全被回退"其实等于什么都没改。
    diff = [{"field": k, "old": base.get(k), "new": after.get(k)}
            for k in patch if str(base.get(k)) != str(after.get(k))]
    if not diff:
        _append_edit_log(conn, pid, [{"ts": now_cn(), "actor": actor, "action": "rework_failed",
                                      "field": "、".join(sorted(patch)), "old": reject_note[:60],
                                      "new": json.dumps({"提交": patch, "结果": "全部被回退或与原值相同",
                                                         "轮次": rounds + 1}, ensure_ascii=False)}])
        conn.execute("UPDATE pending_records SET review_status='rejected', note=? WHERE id=?",
                     ("%s｜第%d轮 agent 提交的值主数据里没有或与原值相同，等于没改"
                      % (reject_note, rounds + 1), pid))
        conn.commit()
        return {"ok": False, "error": "提交的值全部被回退（主数据查无此值）或与原值相同，等于没改",
                "status": "rejected", "round": rounds + 1, "strict_rejected": res.get("rejected") or []}
    passed = res.get("check_status") == "passed"
    _append_edit_log(conn, pid, [{"ts": now_cn(), "actor": actor,
                                  "action": "rework" if passed else "rework_failed",
                                  "field": "、".join(sorted({d["field"] for d in diff})),
                                  "old": reject_note[:60],
                                  "new": json.dumps({"改动": diff, "理由": reason, "轮次": rounds + 1,
                                                     "仍不过": (res.get("errors") or [])[:4] if not passed else []},
                                                    ensure_ascii=False)}])
    if passed:
        conn.execute("UPDATE pending_records SET note=? WHERE id=?",
                     ("返修第%d轮｜%s｜agent: %s" % (rounds + 1, reject_note or "（无原驳回原因）", reason or "已按原因修订"), pid))
    else:                          # 仍不合格：退回驳回日志，不占人工队列，但把"试过什么"留在案
        conn.execute("UPDATE pending_records SET review_status='rejected', note=? WHERE id=?",
                     ("%s｜第%d轮 agent 改后仍不过：%s" % (reject_note, rounds + 1,
                        "；".join("%s:%s" % (e.get("field"), e.get("msg")) for e in (res.get("errors") or [])[:3])), pid))
    conn.commit()
    return {"ok": True, "status": "awaiting" if passed else "rejected", "round": rounds + 1,
            "check_status": res.get("check_status"), "errors": res.get("errors") or [],
            "strict_rejected": res.get("rejected") or [], "changes": diff}


def restore_pending(conn: sqlite3.Connection, pid: int) -> Dict[str, Any]:
    """撤回误驳：rejected → awaiting，重回待处理队列。"""
    cur = conn.execute("UPDATE pending_records SET review_status='awaiting', note=NULL "
                       "WHERE id=? AND review_status='rejected'", (pid,))
    conn.commit()
    return {"ok": bool(cur.rowcount)} if cur.rowcount else {"ok": False, "error": "not found/非驳回态"}


def _delete_pending_no_commit(conn: sqlite3.Connection, pid: int, force: bool = False) -> Dict[str, Any]:
    """物理删除：审核人员可删除待审误加条目及驳回日志，默认保护已放行记录。
    force=True 显式强删任意状态（运维/测试用，handler 需 body.force 明确传入）。
    顺带清 pending_shadow 里该 pending 的孤儿影子行。"""
    if force:
        cur = conn.execute("DELETE FROM pending_records WHERE id = ?", (pid,))
    else:
        cur = conn.execute(
            "DELETE FROM pending_records WHERE id = ? AND review_status IN ('awaiting','rejected')", (pid,))
    if cur.rowcount:
        conn.execute("DELETE FROM pending_shadow WHERE source_pending_id = ?", (pid,))
    if not cur.rowcount and not force:
        row = conn.execute("SELECT review_status FROM pending_records WHERE id=?", (pid,)).fetchone()
        if row:
            return {"ok": False, "deleted": 0,
                    "error": f"该条状态是 {row['review_status']}，只能删除待审或已驳回条目"}
    return {"ok": bool(cur.rowcount), "deleted": cur.rowcount}


def delete_pending(conn: sqlite3.Connection, pid: int, force: bool = False) -> Dict[str, Any]:
    result = _delete_pending_no_commit(conn, pid, force=force)
    conn.commit()
    return result


def delete_pending_ids(conn: sqlite3.Connection, ids: Iterable[int], kind: str = "",
                       force: bool = False) -> Dict[str, Any]:
    """Delete a rendered group atomically; never leave a partially deleted group."""
    wanted = []
    for raw in ids or []:
        try:
            pid = int(raw)
        except (TypeError, ValueError):
            continue
        if pid > 0 and pid not in wanted:
            wanted.append(pid)
    if not wanted:
        return {"ok": False, "deleted": 0, "error": "ids required"}
    placeholders = ",".join("?" for _ in wanted)
    rows = conn.execute("SELECT id,kind,review_status FROM pending_records WHERE id IN (" + placeholders + ")", wanted).fetchall()
    by_id = {row["id"]: row for row in rows}
    errors = []
    for pid in wanted:
        row = by_id.get(pid)
        if not row:
            errors.append({"id": pid, "error": "not found"})
        elif kind and row["kind"] != kind:
            errors.append({"id": pid, "error": "类型不匹配"})
        elif not force and row["review_status"] not in ("awaiting", "rejected"):
            errors.append({"id": pid, "error": "只能删除待审或已驳回条目"})
    if errors:
        conn.rollback()
        return {"ok": False, "deleted": 0, "ids": [], "errors": errors}
    for pid in wanted:
        result = _delete_pending_no_commit(conn, pid, force=force)
        if not result.get("ok"):
            conn.rollback()
            return {"ok": False, "deleted": 0, "ids": [], "error": "整组删除失败，已回滚"}
    conn.commit()
    return {"ok": True, "deleted": len(wanted), "ids": wanted}


def _median(vals: List[Any]) -> float:
    xs = sorted(vals)
    n = len(xs)
    if n == 0:
        return 0.0
    return float(xs[n // 2]) if n % 2 else (xs[n // 2 - 1] + xs[n // 2]) / 2.0


def audit_all(conn: sqlite3.Connection, master: "Master", viewer: str = "") -> Dict[str, Any]:
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
    # Audit identity must retain independent service/terminal/date/source offers.
    audit_scope = ("service_name", "destination_terminal", "valid_from", "valid_to", "source_group", "mid")
    id_cols = {"rates": ("pol", "pod", "carrier", "container_type", "etd", "vessel_voyage") + audit_scope,
               "space": ("pol", "pod", "carrier", "container_type", "vessel_voyage", "space_type", "etd") + audit_scope}

    orphan_g: Dict[str, int] = {}
    orphan_s: Dict[str, int] = {}
    orphan_port: List[Dict[str, Any]] = []
    dup_current: List[Dict[str, Any]] = []
    expired: List[Dict[str, Any]] = []
    zero_price: List[Dict[str, Any]] = []
    outlier_price: List[Dict[str, Any]] = []

    account_scope = scope_clause(conn, viewer)
    account_scope = " AND " + account_scope if account_scope else ""
    for kind in ("rates", "space"):
        table = TABLES[kind]["table"]
        inv = {v: k for k, v in TABLES[kind]["map"].items()}
        rows = [dict(r) for r in conn.execute(
            f"SELECT * FROM {table} WHERE record_type=?" + account_scope, (kind,))]
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
                pv = r.get("price_usd")
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
            m = _median([x["price_usd"] for x in rs])
            if m <= 0:
                continue
            for x in rs:
                pv = x["price_usd"]
                if pv < 0.5 * m or pv > 2 * m:
                    outlier_price.append({"kind": kind, "content_key": x["content_key"], "pol": x.get("pol"),
                                          "pod": x.get("pod"), "carrier": x.get("carrier"),
                                          "price_usd": pv, "median": round(m, 1)})

    def _add(t: str, items: List[Dict[str, Any]], detail: str) -> None:
        if items:
            integ.append({"type": t, "count": len(items), "detail": detail, "items": items[:20]})

    _add("来源群未入库", [{"value": k, "count": c} for k, c in
         sorted(orphan_g.items(), key=lambda kv: -kv[1])],
         "业务里有、群聊库没有——此后放行/导入会自动登记，历史存量可去库管理补默认起运港/归属")
    _add("发送人未入库", [{"value": k, "count": c} for k, c in
         sorted(orphan_s.items(), key=lambda kv: -kv[1])], "业务里有、发送人库没有")
    _add("港口不在主数据", orphan_port, "起运/目的港既不是规范港名也不是港群名，疑脏值，需补港口或港群")
    _add("同身份多条当前生效", dup_current, "同来源、服务、码头及有效期内多条当前生效；核对价格和条款后判断，不自动合并/覆盖")
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
        "SELECT COUNT(*) AS total_count, SUM(active) AS active_count, "
        "SUM(CASE WHEN source='auto' THEN 1 ELSE 0 END) AS auto_count FROM ports").fetchone()
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
    rec: Dict[str, Any] = {cn: old[col] for cn, col in spec["map"].items() if old[col] is not None}
    primary_cols = set(spec["map"].values())
    rec.update({cn: old[col] for cn, col in ITEM_CN_MAP.items()
                if col not in primary_cols and old[col] is not None})
    inv = {col: cn for cn, col in spec["map"].items()}
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
    try:
        conn.execute("BEGIN IMMEDIATE")
        upsert_rows(conn, kind, rows, commit=False, trusted_publisher_id=old['publisher_id'])
        for r in rows:
            conn.execute(f"UPDATE {table} SET supersedes = ? WHERE content_key = ?",
                         (content_key, r["content_key"]))
        conn.execute(f"UPDATE {table} SET status = ?, overridden_time = ? WHERE content_key = ?",
                     (COVERED, now, content_key))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    changes = []
    for col in spec["key"]:
        if col == "message_time":
            continue                          # 录入时间每次都变，写进摘要是噪声
        a = "" if old[col] is None else str(old[col])
        b = "" if rows[0].get(col) is None else str(rows[0][col])
        if a != b:
            report_col = "freight_usd" if kind == "rates" and col == "price_usd" else col
            changes.append({"col": report_col, "label": inv.get(col, col), "from": a, "to": b})
    return {"ok": True, "table": kind, "old_key": content_key,
            "new_keys": [r["content_key"] for r in rows], "revised": len(rows),
            "changes": changes, "unmatched": list(master.unmatched)}


def reclassify_record(conn: sqlite3.Connection, master: Master, content_key: str,
                      record_type: str, cn_fields: Dict[str, Any], origin: str = "manual") -> Dict[str, Any]:
    """将已放行记录切换展示分类，并以同表新版本保留原行和完整审计链。"""
    if record_type not in ("rates", "space"):
        return {"ok": False, "error": "record_type must be rates or space"}
    old = conn.execute("SELECT * FROM freight_items WHERE content_key=?", (content_key,)).fetchone()
    if old is None:
        return {"ok": False, "error": "record not found"}
    inv = {v: k for k, v in ITEM_CN_MAP.items()}
    rec = {inv[c]: old[c] for c in old.keys() if c in inv and old[c] is not None}
    rec.update(cn_fields or {})
    v = validate_record(conn, master, record_type, rec)
    if not v["ok"]:
        return {"ok": False, "error": "分类切换后校验未通过", "errors": v["errors"]}
    # revise_record 复用统一表版本链；分类字段不参与 content_key，补充的专属事实才会生成新版本。
    result = revise_record(conn, master, record_type, content_key, rec, origin=origin)
    if result.get("ok"):
        result["record_type"] = record_type
    return result


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


def scope_clause(conn: sqlite3.Connection, viewer: str,
                 group_column: str = "source_group", route_column: str = "route") -> str:
    """Optional 'my entries' display filter; legacy routes/groups are not permissions."""
    if not viewer:
        return ""
    row = conn.execute("SELECT name, role, publisher_id FROM operators WHERE name = ? AND active = 1",
                       (viewer,)).fetchone()
    if row is None:
        return "1=0"
    if row['role'] != 'operator':
        return ""
    if not row['publisher_id']:
        return '1=0'
    if 'json_extract(' in group_column:
        payload = group_column.split('(',1)[1].split(',',1)[0]
        column = payload.rsplit('.',1)[0]+'.publisher_id' if '.' in payload else 'publisher_id'
    else:
        column = group_column.replace('source_group','publisher_id')
    return column + ' = ' + _lit(row['publisher_id'])


def _multi_filter(master: "Master", col: str, raw: Any, expand_groups: bool) -> Tuple[str, List[Any]]:
    """pol/pod 支持逗号分隔多值：每值内部走 port_filter（保留群↔成员双向展开），多值之间 OR。
    单值时等价原行为；跨字段（pol 与 pod 同时有值）由上层 AND 组合 = 交集。"""
    toks = [t.strip() for t in str(raw or "").split(",") if t.strip()]
    if not toks:
        return f"{col} = ?", [""]
    frags: List[str] = []; args: List[Any] = []
    for t in toks:
        if master.group_by_name:
            f, v = master.port_filter(col, t, expand_members=expand_groups)
        else:
            f, v = f"{col} = ?", [t]
        frags.append(f); args += v
    return ("(" + " OR ".join(frags) + ")"), args


def _where(conn: sqlite3.Connection, master: Master, kind: str, q: Dict[str, str],
           with_status: bool = True) -> Tuple[str, List[Any]]:
    spec = TABLES[kind]
    cols = set(spec["map"].values()) | {"entry_id", "origin", "transit_days", "supersedes"}
    expand_groups = (q.get("nogroup") or "") != "1"   # 销售端传 nogroup=1：不展开港群，按单港精确匹配
    where, args = ["record_type = ?"], [kind]
    if q.get('_client_app') == 'prospector':
        if with_status:
            where.append("COALESCE(status, '') <> ?")
            args.append(COVERED)
        if q.get('container'):
            types = ctype_list(q['container'])
            if not types:
                raise ValueError('invalid container')
            # Reuse canonical parser and ctype search, including legacy raw aliases.
            q = dict(q)
            aliases = [value for value in CTYPE_FORMS if norm_ctype(value) in types]
            q['ctype'] = ','.join(dict.fromkeys([*types, *aliases]))
            q.pop('container')
        from core.prospector_contracts import WEB_MATCH
        if q.get('match') != WEB_MATCH:
            from services.prospector_query import clauses
            q, additional, additional_args = clauses(q, kind)
            where.extend(additional)
            args.extend(additional_args)
    for key, col in FILTERS.items():
        if key == "status" and not with_status:
            continue                          # 折叠视图要带出历史行
        val = q.get(key)
        if not val or (val == "all" and key not in (OPS_MESSAGE_ID_PARAM, "entry_id")) or col not in cols:
            continue
        if col in ("pol", "pod"):
            frag, vals = _multi_filter(master, col, val, expand_groups)   # 多值 OR；每值保留群↔成员双向展开
            where.append(frag)
            args += vals
        elif key == OPS_MESSAGE_ID_PARAM:
            value = normalize_identity_query(val)
            where.append(_normalized_message_id_sql(col) + " = ?" if value else "1=0")
            if value:
                args.append(value)
        elif key == "route" and kind == "space":
            # 销售口径里的“航线”同时承接 WSA/MD1 这类服务码；两者都能定位无具体 POD 的舱位。
            where.append("(route = ? OR service_name = ?)")
            args.extend([val, val])
        elif key == "carrier":
            carriers = list(dict.fromkeys(split_list(val)))
            where.append("carrier IN (" + ",".join("?" for _ in carriers) + ")" if carriers else "1=0")
            args.extend(carriers)
        elif key == "ctype":
            types = list(dict.fromkeys(split_list(val)))
            postgres = getattr(conn, "engine", "") == "postgres"
            if not postgres and any(value in SALES_45_HIGH_CUBE_ALIASES for value in types):
                conn.create_function("freight_has_45", 1, has_45_high_cube, deterministic=True)
            fragment, values = container_filter_clause(types, postgres=postgres)
            where.append(fragment)
            args.extend(values)
        else:
            where.append(f"{col} = ?")
            args.append(val)
    for key, column, explicit in (("default_routes", "route", "route"), ("default_groups", "source_group", "group")):
        if not q.get(explicit) and q.get(key):
            values = json.loads(q[key])
            from core.contracts import ACCOUNT_PREFERENCE_MAX_ITEMS
            if not isinstance(values, list) or not values or len(values) > ACCOUNT_PREFERENCE_MAX_ITEMS or any(not isinstance(v, str) for v in values):
                raise ValueError("默认查看范围须为名称数组")
            where.append(column + " IN (" + ",".join("?" for _ in values) + ")")
            args.extend(values)
    from core.contracts import DISPLAY_ROUTES_PARAM, DISPLAY_GROUPS_PARAM
    from services.display_preferences import preferred_values
    for key, column in ((DISPLAY_ROUTES_PARAM, "route"), (DISPLAY_GROUPS_PARAM, "source_group")):
        values = preferred_values(q, key)
        if values:
            where.append(column + " IN (" + ",".join("?" for _ in values) + ")")
            args.extend(values)
    if q.get(OPS_ID_QUERY_PARAM):
        value = normalize_identity_query(q[OPS_ID_QUERY_PARAM])
        where.append("(entry_id = ? OR " + _normalized_message_id_sql("mid") + " = ?)" if value else "1=0")
        if value:
            args.extend([value, value])
    # 有效期覆盖某日：缺一边边界视为不设限（如只报了「至某日」或「自某日起」）；
    # 运价/舱位两表都有 valid_from/valid_to，列不在则跳过该筛选。
    vf = (q.get("valid") or "").strip()
    if vf and "valid_from" in cols and "valid_to" in cols:
        where.append("(valid_from IS NULL OR date(valid_from) <= date(?)) "
                     "AND (valid_to IS NULL OR date(valid_to) >= date(?))")
        args += [vf, vf]
    valid_since = (q.get(SALES_VALID_SINCE_PARAM) or "").strip()
    if valid_since and "valid_to" in cols:
        datetime.strptime(valid_since, "%Y-%m-%d")
        where.append("(valid_to IS NULL OR valid_to = '' OR date(valid_to) >= date(?))")
        args.append(valid_since)
    # 消息时间起止：按日粒度比较，message_time 是 "YYYY-MM-DD HH:MM:SS"。
    tf = (q.get("time_from") or "").strip()
    tt = (q.get("time_to") or "").strip()
    if tf or tt:
        tm = []
        if tf:
            tm.append("substr(message_time, 1, 10) >= ?")
            args.append(tf[:10])
        if tt:
            tm.append("substr(message_time, 1, 10) <= ?")
            args.append(tt[:10])
        where.append("(" + " AND ".join(tm) + ")")
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
    order = sales_order_sql(q)
    total = conn.execute(f"SELECT COUNT(*) FROM {table} {frag}", args).fetchone()[0]
    limit = max(1, min(int(q.get("limit") or 50), 500))
    offset = max(0, int(q.get("offset") or 0))
    rows = conn.execute(f"SELECT * FROM {table} {frag} ORDER BY {order} "
                        f"LIMIT ? OFFSET ?", args + [limit, offset]).fetchall()
    result = {"total": total, "limit": limit, "offset": offset,
              "rows": annotate_publishers(conn, [dict(r) for r in rows])}
    if q.get("include_source_groups") == "1":
        group_where = ["record_type = ?", "source_group IS NOT NULL", "source_group <> ''"]
        group_args = [kind]
        scope = scope_clause(conn, q.get("viewer") or "")
        if scope:
            group_where.append(scope)
        result["source_groups"] = [row[0] for row in conn.execute(
            f"SELECT DISTINCT source_group FROM {table} WHERE {' AND '.join(group_where)} ORDER BY source_group",
            group_args)]
    return result


def sales_group_summary(conn: sqlite3.Connection, master: Master, q: Dict[str, str],
                        user_id: str = "") -> Dict[str, Any]:
    """Aggregate every matching source group without a client-side row cap."""
    groups = {}
    for kind in ("rates", "space"):
        frag, args = _where(conn, master, kind, q)
        rows = conn.execute(
            f"SELECT source_group, COUNT(*) AS count, MAX(message_time) AS latest FROM {TABLES[kind]['table']} {frag} "
            "GROUP BY source_group", args)
        for row in rows:
            name = row["source_group"]
            if not name:
                continue
            item = groups.setdefault(name, {"name": name, "rates": 0, "space": 0, "latest": ""})
            item[kind] = row["count"]
            item["latest"] = max(item["latest"], row["latest"] or "")
    rows = sorted(groups.values(), key=lambda item: (item["latest"], item["name"]), reverse=True)
    if user_id and rows:
        counts = {row["source_group"]: row["click_count"] for row in conn.execute(
            "SELECT source_group, click_count FROM sales_group_visits WHERE user_id=?", (user_id,))}
        rows.sort(key=lambda item: counts.get(item["name"], 0), reverse=True)
    return {"rows": rows, "order": "personal_clicks" if user_id else "latest"}


def record_sales_group_visit(conn, user_id: str, name: str, query: Dict[str, str]) -> Dict[str, int]:
    from core.contracts import SALES_GROUP_NAME_MAX_LENGTH
    if not user_id or not isinstance(name, str) or not name.strip() or len(name) > SALES_GROUP_NAME_MAX_LENGTH:
        raise ValueError("invalid source group")
    visible = sales_group_summary(conn, get_master(conn), dict(query, group=name))["rows"]
    if not any(row["name"] == name for row in visible):
        raise ValueError("source group not available")
    try:
        conn.execute("INSERT INTO sales_group_visits(user_id,source_group,click_count,last_clicked_at) "
                     "VALUES(?,?,1,?) ON CONFLICT(user_id,source_group) DO UPDATE SET "
                     "click_count=sales_group_visits.click_count+1,last_clicked_at=excluded.last_clicked_at",
                     (user_id, name, now_cn()))
        count = conn.execute("SELECT click_count FROM sales_group_visits WHERE user_id=? AND source_group=?",
                             (user_id, name)).fetchone()[0]
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return {"click_count": count}


# 折叠视图缓存：key=(kind, 筛选SQL, 参数, 库内容版本)。data_version 在别的连接写库后 +1，
# 所以任何写库（放行/改价/停用/主数据维护）都会自动让缓存失效，读多写少场景下切页签不再全表重算折叠。
_GROUPED_CACHE: Dict[tuple, Any] = {}
_GROUPED_CACHE_MAX = 64


def _grouped_cache_namespace(conn: sqlite3.Connection) -> str:
    """Keep grouped-query cache entries isolated between database connections.

    SQLite's ``PRAGMA data_version`` is local to a connection and starts from
    the same value for every new in-memory database.  Using it alone allowed a
    result from one disposable/test database to be reused by another database
    with the same query.  Persistent SQLite databases use their resolved file
    path; in-memory databases fall back to the connection identity.
    """
    try:
        row = conn.execute("PRAGMA database_list").fetchone()
        path = str(row[2] or "") if row else ""
        return path if path else f":memory:{id(conn)}"
    except Exception:  # noqa: BLE001 - cache isolation must never break queries
        return f"connection:{id(conn)}"


def _db_version(conn: sqlite3.Connection) -> int:
    try:
        return int(conn.execute("PRAGMA data_version").fetchone()[0])
    except Exception:                                    # noqa: BLE001 - 版本取不到就不缓存（key 恒变）
        return 0


def query_grouped(conn: sqlite3.Connection, master: Master, kind: str,
                  q: Dict[str, str]) -> Dict[str, Any]:
    """折叠：同一条报价的多柜型并成一行；同身份的当前价与历史价分开。

    性能：全量折叠结果按 (kind, 筛选条件, 库内容版本) 缓存；返回体只带分页后的 groups，
    不再带全量 rows（CSV 导出改由前端走扁平接口自行分页拉取），响应体瘦身。
    deepcopy 防止调用方（with_images 就地加 images 字段）污染缓存对象。"""
    parse_sales_sort(q, grouped=True)
    spec = TABLES[kind]
    frag, args = _where(conn, master, kind, q, with_status=False)
    ckey = (_grouped_cache_namespace(conn), kind, frag, tuple(args), _db_version(conn))
    cached = _GROUPED_CACHE.get(ckey)
    if cached is None:
        rows = [dict(r) for r in conn.execute(
            f'SELECT * FROM {spec["table"]} {frag} ORDER BY message_time DESC, id DESC', args)]
        annotate_publishers(conn, rows)
        price_col = "price_usd"
        today = today_cn()
        quotes: Dict[tuple, Dict[str, Any]] = {}
        for r in rows:
            # 聚合前就过滤掉"已被覆盖"的记录——不能让被覆盖的旧价显示在销售端
            if r.get("status") == COVERED:
                continue
            qk = (r.get("publisher_id"), r.get("carrier"), r.get("route"), r.get("pol"), r.get("pod"),
                  r.get("etd") or "", r.get("valid_from") or "", r.get("valid_to") or "",
                  *(r.get(col) or "" for col in QUOTE_VARIANT_FIELDS.values()))
            qt = quotes.get(qk)
            if qt is None:
                qt = {k: r.get(k) for k in GROUP_FIELDS}
                qt["cab"] = {}
                qt["cab_entry_id"] = {}
                quotes[qk] = qt
            ct = norm_ctype(r.get("container_type"))
            pv = r.get(price_col)
            if ct and pv not in (None, ""):
                # 同柜型后写覆盖先写：按 message_time 倒序排，最新价覆盖旧价
                if ct not in qt["cab"] or (r.get("message_time") or "") >= (qt.get("_mt", {}) or {}).get(ct, ""):
                    qt["cab"][ct] = pv
                    qt["cab_entry_id"][ct] = r.get("entry_id")
                    qt.setdefault("_mt", {})[ct] = r.get("message_time") or ""
        byid: Dict[tuple, List[Dict[str, Any]]] = {}
        for qt in quotes.values():
            qt["expired"] = bool(qt.get("valid_to")) and str(qt["valid_to"])[:10] < today
            byid.setdefault((qt["carrier"], qt["route"], qt["pol"], qt["pod"], qt["etd"] or "",
                             qt.get("valid_from") or "", qt.get("valid_to") or "",
                             *(qt.get(col) or "" for col in QUOTE_VARIANT_FIELDS.values())), []).append(qt)
        groups = []
        for idk, lst in byid.items():
            lst.sort(key=lambda x: x.get("message_time") or "", reverse=True)
            alive = [x for x in lst if not x["expired"] and x.get("status") != COVERED]
            cur = alive[0] if alive else None
            if not cur:
                continue        # 无有效价的路由不显示
            groups.append({"id": {"carrier": idk[0], "route": idk[1], "pol": idk[2],
                                  "pod": idk[3], "etd": idk[4], "valid_from": idk[5], "valid_to": idk[6],
                                  **dict(zip(QUOTE_VARIANT_FIELDS.values(), idk[7:]))},
                           "current": cur, "history": [x for x in lst if x is not cur]})
        groups.sort(key=lambda g: (_latest(g), g["id"]["carrier"] or "", g["id"]["pod"] or ""),
                    reverse=True)
        # 空结果不写缓存：历史出现过「无筛选键被缓存成空、此后一直返回 0 条」的坏缓存
        # （写库会失效缓存，但某次空窗口/异常窗口的查询结果会被钉住，直到库版本再变）。
        # 代价：真没匹配时会重算一次全量折叠，量级小，正确性优先。
        if groups:
            cached = groups
            if len(_GROUPED_CACHE) >= _GROUPED_CACHE_MAX:
                _GROUPED_CACHE.clear()
            _GROUPED_CACHE[ckey] = cached
        else:
            cached = groups
    limit = max(1, min(int(q.get("limit") or 50), 200))
    offset = max(0, int(q.get("offset") or 0))
    ordered = sort_sales_groups(cached, q)
    return {"total": len(ordered), "limit": limit, "offset": offset,
            "groups": copy.deepcopy(ordered[offset:offset + limit])}


def _latest(g: Dict[str, Any]) -> str:
    q = g["current"] or (g["history"][0] if g["history"] else None)
    return (q or {}).get("message_time") or ""


OPT_COLS = {"rates": ["pol", "pod", "route", "carrier", "container_type", "source_group"],
            "space": ["pol", "pod", "route", "service_name", "carrier", "container_type", "space_type",
                      "source_group"]}


def options(conn: sqlite3.Connection) -> Dict[str, Any]:
    out = {}
    for kind, cols in OPT_COLS.items():
        table = TABLES[kind]["table"]
        out[kind] = {c: [r[0] for r in conn.execute(
            f"SELECT DISTINCT {c} FROM {table} WHERE record_type=? AND {c} IS NOT NULL AND {c} <> '' ORDER BY {c}", (kind,))]
            for c in cols}
    return out


def stats(conn: sqlite3.Connection) -> Dict[str, Any]:
    def one(sql: str, args=()):
        r = conn.execute(sql, args).fetchone()
        return r[0] if r and r[0] is not None else 0     # 空表安全，不在这上面炸
    latest = str(one("SELECT message_time FROM freight_items ORDER BY message_time DESC LIMIT 1") or "")
    opts = options(conn)
    return {"ratesTotal": one("SELECT COUNT(*) FROM freight_items WHERE record_type='rates'"),
            "ratesCurrent": one("SELECT COUNT(*) FROM freight_items WHERE record_type='rates' AND status = ?",
                                (DEFAULT_STATUS["rates"],)),
            "spaceTotal": one("SELECT COUNT(*) FROM freight_items WHERE record_type='space'"),
            "groups": len(opts["rates"]["source_group"]),
            "carriers": len(opts["rates"]["carrier"]),
            "latestMessage": latest or ""}


def freq(conn: sqlite3.Connection, col: str) -> List[Dict[str, Any]]:
    """使用频次现算不存字段：下拉按它降序排。"""
    return [{"name": r[0], "n": r[1]} for r in conn.execute(
        f"SELECT {col}, COUNT(*) FROM freight_items "
        f"WHERE {col} IS NOT NULL AND {col} <> '' GROUP BY {col} ORDER BY 2 DESC")]


# ---------------- 账号库 / 群聊库 ----------------

def list_operators(conn: sqlite3.Connection) -> List[Dict[str, Any]]:
    out = []
    dingtalk_ids = {}
    for row in conn.execute('SELECT publisher_id,identity_key FROM publisher_dingtalk_ids'):
        dingtalk_ids.setdefault(row['publisher_id'],[]).append(row['identity_key'])
    groups_by_publisher = {}
    for row in conn.execute("SELECT DISTINCT publisher_id,source_group FROM publisher_sources WHERE source_group<>''"):
        groups_by_publisher.setdefault(row['publisher_id'],set()).add(row['source_group'])
    routes_by_sender = {}
    for row in conn.execute("SELECT DISTINCT publisher_id AS sender,route FROM freight_items WHERE publisher_id<>'' AND route<>''"):
        routes_by_sender.setdefault(row['sender'],set()).add(row['route'])
    for row in conn.execute("SELECT DISTINCT publisher_id AS sender, COALESCE(json_extract(resolved,'$.航线'),json_extract(payload,'$.航线')) AS route FROM pending_records WHERE check_status='passed' AND review_status IN ('awaiting','approved')"):
        if row['sender'] and row['route']:
            routes_by_sender.setdefault(row['sender'],set()).add(row['route'])
    for r in conn.execute("SELECT * FROM operators ORDER BY active DESC, id"):
        d = dict(r)
        d['dingtalk_identity_keys'] = sorted(dingtalk_ids.get(d.get('publisher_id'),[]))
        d['publisher_groups'] = sorted(groups_by_publisher.get(d.get('publisher_id'),set()))
        d["routes_list"] = sorted(routes_by_sender.get(d.get('publisher_id'),set())) if d['role']=='operator' else []
        for retired in ('routes','groups','match_mode'):
            d.pop(retired,None)
        d["has_password"] = bool(d.get("password_hash"))
        d["login_enabled"] = bool(d.get("active") and d["has_password"])
        d.pop("password_hash", None)
        out.append(d)
    return out


def ensure_publisher_people(conn, names: Iterable[str]) -> int:
    """Register confirmed people without granting login or changing existing accounts."""
    if hasattr(names,'members'):
        from ops.publisher_identity import sync_confirmed
        created=sync_confirmed(conn,names.members,now_cn())
        conn.commit()
        return created
    created = 0
    for name in dict.fromkeys(str(value).strip() for value in names):
        if not name:
            continue
        if conn.execute("SELECT 1 FROM operators WHERE name=? OR publisher_name=?", (name, name)).fetchone():
            continue
        publisher_id = create_publisher(conn, name, now_cn())
        cursor = conn.execute(
            "INSERT INTO operators(user_id,name,publisher_name,publisher_id,role,active,created_at) VALUES(?,?,?,?,?,?,?)",
            ("usr_" + uuid.uuid4().hex, name, name, publisher_id, ROLE_OPERATOR, 0, now_cn()))
        created += cursor.rowcount
    conn.commit()
    return created


def legacy_user_id(name: str) -> str:
    return "usr_" + uuid.uuid5(uuid.NAMESPACE_URL, "freight-account:" + name).hex


def ensure_account_schema(conn, commit: bool = True) -> Dict[str, Any]:
    """Startup-only role upgrade; preserve account rows, passwords and existing sessions."""
    postgres = getattr(conn, "engine", "sqlite") == "postgres"
    try:
        if postgres:
            checks = conn.execute("SELECT conname,pg_get_constraintdef(oid) AS definition FROM pg_constraint "
                                  "WHERE conrelid='operators'::regclass AND contype='c'").fetchall()
            role_checks = [row for row in checks if "role" in row["definition"]]
            if not role_checks or any("'sales'" not in row["definition"] for row in role_checks):
                for row in role_checks:
                    identifier = '"' + row["conname"].replace('"', '""') + '"'
                    conn.execute("ALTER TABLE operators DROP CONSTRAINT " + identifier)
                conn.execute("ALTER TABLE operators ADD CONSTRAINT operators_role_check "
                             "CHECK(role IN ('admin','operator','sales'))")
            conn.execute("ALTER TABLE operators ADD COLUMN IF NOT EXISTS user_id TEXT")
            conn.execute("ALTER TABLE operators ADD COLUMN IF NOT EXISTS display_name TEXT")
            conn.execute("ALTER TABLE operators ADD COLUMN IF NOT EXISTS preferences_json TEXT")
            conn.execute("ALTER TABLE operators ADD COLUMN IF NOT EXISTS publisher_name TEXT")
        else:
            definition = conn.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='operators'").fetchone()[0]
            if "'sales'" not in definition:
                conn.execute("ALTER TABLE operators RENAME TO operators_auth_legacy")
                conn.execute(DDL["operators"])
                columns = [row[1] for row in conn.execute("PRAGMA table_info(operators_auth_legacy)")]
                shared = ",".join(columns)
                conn.execute(f"INSERT INTO operators({shared}) SELECT {shared} FROM operators_auth_legacy")
                conn.execute("DROP TABLE operators_auth_legacy")
            columns = {row[1] for row in conn.execute("PRAGMA table_info(operators)")}
            for column in ("user_id", "display_name", "preferences_json", "publisher_name"):
                if column not in columns:
                    conn.execute("ALTER TABLE operators ADD COLUMN " + column + " TEXT")
        rows = conn.execute("SELECT name FROM operators WHERE user_id IS NULL OR user_id='' ").fetchall()
        for row in rows:
            conn.execute("UPDATE operators SET user_id=? WHERE name=?", (legacy_user_id(row["name"]), row["name"]))
        conn.execute("UPDATE operators SET publisher_name=name WHERE publisher_name IS NULL OR publisher_name='' ")
        conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_operators_user_id ON operators(user_id)")
        ensure_publisher_schema(conn, now_cn())
        conn.execute(DDL["account_recovery_requests"])
        for definition in ADMIN_DDL.values():
            conn.execute(definition)
        if postgres:
            conn.execute('ALTER TABLE account_security ADD COLUMN IF NOT EXISTS revision INTEGER NOT NULL DEFAULT 0')
        elif 'revision' not in {row[1] for row in conn.execute('PRAGMA table_info(account_security)')}:
            conn.execute('ALTER TABLE account_security ADD COLUMN revision INTEGER NOT NULL DEFAULT 0')
        conn.execute('CREATE INDEX IF NOT EXISTS idx_account_events_user_id ON account_events(user_id)')
        conn.execute('CREATE INDEX IF NOT EXISTS idx_account_events_created_at ON account_events(created_at)')
        conn.execute('CREATE INDEX IF NOT EXISTS idx_account_events_action ON account_events(action)')
        if commit:
            conn.commit()
        return {"ok": True, "data": {"identities_backfilled": len(rows)}}
    except Exception:
        conn.rollback()
        raise


def protect_last_admin(conn, name: str) -> None:
    """Call before deactivating, demoting or deleting a login-capable administrator."""
    lock = " FOR UPDATE" if getattr(conn, "engine", "sqlite") == "postgres" else ""
    administrators = conn.execute("SELECT name FROM operators WHERE role='admin' AND active=1 "
                                  "AND password_hash IS NOT NULL ORDER BY name" + lock).fetchall()
    if len(administrators) == 1 and administrators[0]["name"] == name:
        raise PermissionError("不能停用、降级或删除最后一个可登录的管理员")


def save_operator(conn: sqlite3.Connection, data: Dict[str, Any], commit: bool = True) -> Dict[str, Any]:
    name = str(data.get("name") or "").strip()
    if not name:
        raise ValueError("name required")
    if getattr(conn, "engine", "sqlite") == "sqlite" and not conn.in_transaction:
        conn.execute("BEGIN IMMEDIATE")
    previous = conn.execute("SELECT * FROM operators WHERE name=?", (name,)).fetchone()
    new_password_hash = data.get("password_hash")
    data = {**(dict(previous) if previous else {}), **data}
    legacy = dict(previous) if previous else {}
    mode = legacy.get('match_mode') or 'any'
    cols = {"title": coerce(data.get("title")),
            "routes": legacy.get('routes') or '',
            "groups": legacy.get('groups') or '', "match_mode": mode,
            "active": 1 if str(data.get("active", 1)) in ("1", "true", "True") else 0,
            "role": str(data.get("role") or "operator"), "note": coerce(data.get("note")),
            "publisher_name": coerce(data.get("publisher_name")) or name}
    if cols["role"] not in ACCOUNT_ROLES:
        raise ValueError("role must be admin, operator or sales")
    if previous and previous["role"] == ROLE_ADMIN and (cols["role"] != ROLE_ADMIN or not cols["active"]):
        protect_last_admin(conn, name)
    if not previous or not previous["user_id"]:
        cols["user_id"] = legacy_user_id(name) if previous else "usr_" + uuid.uuid4().hex
    cols["publisher_id"] = legacy.get("publisher_id") or create_publisher(conn, cols["publisher_name"], now_cn())
    if new_password_hash:
        cols["password_hash"] = str(new_password_hash)
        cols["password_changed_at"] = now_cn()
    result = _upsert_named(conn, "operators", name, cols, commit=False)
    result["user_id"] = previous["user_id"] if previous and previous["user_id"] else cols["user_id"]
    from ops.admin_accounts import record_event
    changes = {key:{'before':dict(previous).get(key) if previous else None,'after':cols[key]}
               for key in ('role','active') if not previous or dict(previous).get(key)!=cols[key]}
    if new_password_hash:
        changes['password'] = {'changed':True}
        if previous:
            conn.execute('INSERT INTO account_security(user_id,must_change_password,revision) VALUES(?,0,1) ON CONFLICT(user_id) DO UPDATE SET revision=account_security.revision+1', (result['user_id'],))
    if changes or not previous:
        record_event(conn,result['user_id'],'account_created' if not previous else 'account_updated',
                     data.get('_actor') or '系统',data.get('_actor_id') or 'system',changes,now_cn())
    if commit:
        conn.commit()
    return result


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
                  cols: Dict[str, Any], commit: bool = True) -> Dict[str, Any]:
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
    if commit:
        conn.commit()
    return {"ok": True, "action": action, "name": name}


def delete_named(conn: sqlite3.Connection, table: str, name: str) -> int:
    if table not in ("operators", "chat_groups"):
        raise ValueError("table must be operators or chat_groups")
    if table == "operators":
        if getattr(conn, "engine", "sqlite") == "sqlite" and not conn.in_transaction:
            conn.execute("BEGIN IMMEDIATE")
        protect_last_admin(conn, name)
        target = conn.execute("SELECT user_id FROM operators WHERE name=?", (name,)).fetchone()
        if target:
            conn.execute("DELETE FROM sales_group_visits WHERE user_id=?", (target["user_id"],))
        conn.execute("DELETE FROM auth_sessions WHERE operator_name=?", (name,))
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
                   "r": p.get("route") or "", PORT_ROUTES_COMPACT_FIELD: p[PORT_ROUTES_FIELD],
                   "y": p.get("country") or ""} for p in m.ports],
        "carriers": [{"n": c["code"], "c": c.get("name_cn") or "", "e": c.get("name_en") or "",
                      "a": c.get("aliases") or "", "u": c.get("website_url") or "", "o": c.get("note") or ""} for c in m.carriers],
        "routes": [{"n": r["name"], "g": r.get("region") or "", "a": r.get("aliases") or ""}
                   for r in m.routes],
        "senders": list(m.senders),
        "portGroups": [{"n": g["name"], "l": g.get("label") or "", "m": g.get("members") or "",
                        "a": g.get("aliases") or "",
                        "s": g.get("specials") or "", "k": g.get("kind") or "pod",
                        "o": g.get("note") or ""} for g in m.group_rows],
        "aliasScope": [{"al": r["alias"], "k": r["kind"], "t": r["target"],
                        "g": r["scope_group"] or "*", "n": r["note"] or ""}
                       for r in conn.execute("SELECT * FROM alias_scope ORDER BY alias, kind")],
        "groupFreq": freq(conn, "source_group"),
        "senderFreq": freq(conn, "sender"),
        "carrierFreq": freq(conn, "carrier"),
    }
    from core.master_resolution_contracts import PORT_GROUP_INDEX_FIELD
    out[PORT_GROUP_INDEX_FIELD] = m.name_resolver.group_index()
    if full:
        out["raw"] = {"ports": m.ports, "carriers": m.carriers, "routes": m.routes,
                      "senders": [dict(r) for r in conn.execute(
                          "SELECT * FROM senders ORDER BY active DESC, name")],
                      "port_groups": m.group_rows}
    return out


def master_save(conn: sqlite3.Connection, table: str, row: Dict[str, Any]) -> Dict[str, Any]:
    if table not in MASTER_TABLES:
        raise ValueError("table must be one of ports/carriers/routes/senders/port_groups")
    if table == "port_groups":
        if getattr(conn, "engine", "") == "postgres":
            conn.execute("LOCK TABLE port_groups IN SHARE ROW EXCLUSIVE MODE")
        elif not conn.in_transaction:
            conn.execute("BEGIN IMMEDIATE")
    pk = MASTER_TABLES[table]["pk"]
    name = str(row.get(pk) or "").strip()
    if not name:
        raise ValueError(f"{pk} required")
    memberships = None
    if table == "ports" and (PORT_ROUTES_FIELD in row or "route" in row):
        if PORT_ROUTES_FIELD in row and not isinstance(row[PORT_ROUTES_FIELD], list):
            raise ValueError("routes_list must be an array of route names")
        raw_routes = row.get(PORT_ROUTES_FIELD, row.get("route"))
        master = get_master(conn)
        active_names = {r["name"] for r in master.routes}
        memberships = []
        for token in route_tokens(raw_routes):
            target = token if token in active_names else master.route_idx.get(fold(token))
            if not target or target not in active_names:
                raise ValueError("航线不在启用的主数据里：" + token)
            if target not in memberships:
                memberships.append(target)
        if PORT_ROUTES_FIELD in row and "route" in row:
            legacy = {token if token in active_names else master.route_idx.get(fold(token))
                      for token in route_tokens(row["route"])}
            if legacy != set(memberships):
                raise ValueError("route and routes_list disagree")
    if table == "routes" and "active" in row and str(row["active"]) not in ("1", "true", "True"):
        if conn.execute("SELECT 1 FROM port_routes WHERE route_name=? LIMIT 1", (name,)).fetchone():
            raise ValueError("航线仍被港口引用，先移除归属后再停用")
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
    if memberships is not None:
        cols["route"] = ",".join(memberships)
    if table == "senders":
        cols["source"] = "manual"     # 人工在库维护/新建的发送人 → 认领为人工维护（对齐群聊库语义）
    if table == "port_groups":        # 成员必须命中规范名：写错一个字母就会静默断掉双向展开（2026-09-12 体检教训）
        from services.master_resolution import validate_group_names
        previous = conn.execute("SELECT * FROM port_groups WHERE name=?", (name,)).fetchone()
        proposed = dict(previous) if previous else {"name": name}
        proposed.update(cols)
        validate_group_names(proposed, [dict(r) for r in conn.execute("SELECT * FROM port_groups")],
                             [dict(r) for r in conn.execute("SELECT name,active FROM ports")])
        known = {r[0] for r in conn.execute("SELECT name FROM ports WHERE active=1")}
        bad = [m for f in ("members", "specials") for m in split_list(cols.get(f))
               if m and m not in known]
        if bad:
            raise ValueError("成员港不在 ports 规范名里，双向展开会断链，请先补主数据：" + "、".join(bad))
    if table == "ports" and cols.get("active", 1):
        # 规范名是前端唯一显示名：两条在用港口折叠后同名（如 Vitoria / Vitória）会让别名索引整键丢弃，
        # 联想里两个都点不动、还报"不在主数据"（2026-09-18 事故）。拦在写入口；停用路径放行，否则合并不了。
        mine = {fold(name)} | {fold(x) for x in split_list(cols.get("aliases"))}
        clash: List[str] = []
        for r in conn.execute("SELECT name, aliases FROM ports WHERE active=1 AND name <> ?", (name,)):
            for v in [r["name"]] + split_list(r["aliases"]):
                if fold(v) in mine:
                    clash.append(f"{v}（属于 {r['name']}）")
        if clash:
            raise ValueError("折叠后与在用港口撞名，联想会整键丢弃该写法（选了也点不中）："
                             + "、".join(sorted(set(clash))[:4])
                             + "；请合并成一条（把写法并进 aliases）或改用带国家码的规范名")
    tname = MASTER_TABLES[table]["table"]
    exists = conn.execute(f"SELECT 1 FROM {tname} WHERE {pk} = ?", (name,)).fetchone()
    if exists:
        if cols:
            conn.execute(f'UPDATE {tname} SET {", ".join(f"{k} = ?" for k in cols)} WHERE {pk} = ?',
                         [*cols.values(), name])
        action = "updated"
    else:
        cols[pk] = name
        cols["created_at"] = now_cn()
        conn.execute(f'INSERT INTO {tname} ({",".join(cols)}) VALUES ({",".join("?" * len(cols))})',
                     list(cols.values()))
        action = "created"
    if memberships is not None:
        _replace_port_routes(conn, name, memberships)
    saved_routes = [r[0] for r in conn.execute(
        "SELECT route_name FROM port_routes WHERE port_name=? ORDER BY route_name", (name,))] if table == "ports" else None
    conn.commit()
    invalidate_master()
    return {"ok": True, "table": table, "action": action, "key": name,
            "data": {"key": name, PORT_ROUTES_FIELD: saved_routes} if table == "ports" else {"key": name}}


def master_delete(conn: sqlite3.Connection, table: str, key: str) -> Dict[str, Any]:
    if table not in MASTER_TABLES:
        raise ValueError("table must be one of ports/carriers/routes/senders/port_groups")
    pk, tname = MASTER_TABLES[table]["pk"], MASTER_TABLES[table]["table"]
    if table == "routes" and conn.execute(
            "SELECT 1 FROM port_routes WHERE route_name=? LIMIT 1", (key,)).fetchone():
        raise ValueError("航线仍被港口引用，先移除归属后再删除")
    if table == "ports":
        conn.execute("DELETE FROM port_routes WHERE port_name=?", (key,))
    cur = conn.execute(f"DELETE FROM {tname} WHERE {pk} = ?", (key,))
    conn.commit()
    invalidate_master()
    return {"ok": bool(cur.rowcount), "table": table, "deleted": cur.rowcount, "key": key}


# ---------------- alias_scope 生产写接口（三列联合主键，独立于 master_save 单 pk 通用逻辑） ----------------

ALIAS_KINDS = ("pol", "pod", "carrier", "route", "space_type", "ctype", "price", "remark", "schedule")
ALIAS_SPACES = ("manual", "seed_v1", "candidate_approved", "imported")


def alias_scope_save(conn: sqlite3.Connection, row: Dict[str, Any], commit: bool = True) -> Dict[str, Any]:
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
        group = next((g for g in m.group_rows
                      if g.get("name") == target and g.get("kind") in (kind, "both")), None)
        hit = m.lookup_port(target, "pod", "*") or m.lookup_port(target, "pol", "*")
        if not group and (not hit or not hit[1]):
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
        warnings.append(f"target={target} 不在柜型四档规范名（20GP / 40HQ/HC / 40NOR / 45HQ/HC）内")
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
    if commit:
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
                     group: Optional[str] = None, kind: Optional[str] = None,
                     limit: Optional[int] = None, offset: int = 0):
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
    if limit is not None:
        from core.contracts import ALIAS_PAGE_MAX_SIZE
        limit = max(1, min(int(limit), ALIAS_PAGE_MAX_SIZE))
        offset = max(0, int(offset))
        total = conn.execute("SELECT COUNT(*) FROM alias_scope WHERE 1=1" + sql.split("WHERE 1=1", 1)[1], args).fetchone()[0]
    sql += " ORDER BY scope_group, kind, alias"
    if limit is not None:
        sql += " LIMIT ? OFFSET ?"
        args += [limit, offset]
    rows = [dict(r) for r in conn.execute(sql, args)]
    if limit is None:
        return rows
    return {"rows": rows, "total": total, "limit": limit, "offset": offset}


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
                "INSERT INTO pending_shadow "
                "(source_pending_id, mode, kind, payload, resolved, errors, diff_summary, note, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(source_pending_id, mode) DO UPDATE SET "
                "kind=excluded.kind, payload=excluded.payload, resolved=excluded.resolved, "
                "errors=excluded.errors, diff_summary=excluded.diff_summary, "
                "note=excluded.note, created_at=excluded.created_at",
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

def _candidate_port_identity_conflict(conn, cand: Dict[str, Any]) -> bool:
    """A correction between existing port names is not evidence of an alias."""
    if cand.get("kind") not in ("pol", "pod") or cand.get("target_layer", "alias_scope") not in ("alias_scope", "master_aliases"):
        return False
    old = fold(cand.get("alias"))
    new = fold(cand.get("suggested_target"))
    if not old or not new:
        return False
    names = {r[0] for r in conn.execute("SELECT name FROM ports WHERE active=1")}
    old_names = {name for name in names if fold(name) == old}
    new_names = {name for name in names if fold(name) == new}
    return bool(old_names and new_names and old_names != new_names)


def candidate_noise_reason(conn, cand: Dict[str, Any]) -> str:
    """Reject correction artifacts, not legitimate unresolved name variants."""
    if cand.get("target_layer", "alias_scope") not in ("alias_scope", "master_aliases"):
        return ""
    alias = str(cand.get("alias") or "").strip()
    target = str(cand.get("suggested_target") or "").strip()
    kind = cand.get("kind")
    if _candidate_port_identity_conflict(conn, cand):
        return "两个独立规范港口之间的纠错，不是别名"
    if kind in ("pol", "pod"):
        if re.fullmatch(r"(?:USD|US\$|CNY|RMB|EUR|HKD|GBP|JPY|ENS|THC|GFS|OWS|ISPS|\d+(?:\.\d+)?)", alias, re.I):
            return "币种、金额或费用代码误入港口字段"
        groups = {fold(r[0]) for r in conn.execute("SELECT name FROM port_groups")}
        if fold(alias) in groups and fold(alias) != fold(target):
            return "已登记港群被替换为其他地点，不能沉淀为别名"
        if re.search(r"\b(?:base\s+ports?|rail\s+ramp|via|terminal|terport|caacupemi|puerto\s+seguro)\b", fold(alias)):
            return "港群范围、运输服务或码头描述，须保留字段拆分而非别名"
        master = Master(conn)
        group = str(cand.get("scope_key") or "").split("|")[-1]
        resolved, hit = master.lookup_port(alias, kind, group)
        if hit:
            return "现有规则已正确归一，重复推荐" if fold(resolved) == fold(target) else "已有归属与建议目标不同，不可自动重映射"
    if cand.get("source") == "agent_observation":
        return "放行前后差异不等于人工确认别名；保留修订日志，不生成长期规则"
    return ""


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
    noise_reason = candidate_noise_reason(conn, cand)
    if noise_reason:
        return {"action": "skipped_entity_conflict", "id": None,
                "reason": noise_reason}
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
      - sender+carrier+group 以候选批准记录保存执行范围，不写全群别名；旧批准不自动迁移
    target_layer='master_aliases' → 读 ports/carriers/routes 对应实体 aliases 字段，追加 alias；
      仅完整全局证据可走此路径；群/发送人范围不能扩大为全局别名
    """
    if getattr(conn, 'engine', '') == 'postgres':
        conn.execute('LOCK TABLE rule_candidates IN SHARE ROW EXCLUSIVE MODE')
    elif not conn.in_transaction:
        conn.execute('BEGIN IMMEDIATE')
    row = conn.execute("SELECT * FROM rule_candidates WHERE id=?", (cand_id,)).fetchone()
    if not row:
        raise ValueError(f"候选 {cand_id} 不存在")
    if row["status"] != "pending":
        return {"ok": False, "error": f"候选状态已是 {row['status']}，不能重复批准"}
    cand = dict(row)
    noise_reason = candidate_noise_reason(conn, cand)
    if noise_reason:
        return {"ok": False, "error": noise_reason}
    layer = cand["target_layer"]
    scope_key = cand["scope_key"]
    parts = [p.strip() for p in scope_key.split("|")]
    from core.contracts import CONTEXT_ALIAS_APPROVAL_NOTE, CONTEXT_ALIAS_SCOPE
    from services.learning_policy import candidate_scope, normalized_scope
    if layer == 'master_aliases' and cand['scope_kind'] != 'global':
        return {'ok':False,'error':'非全局证据不能写入全局主数据别名'}
    if layer == 'alias_scope':
        try:
            sender, carrier, group_name = candidate_scope(cand)
        except ValueError as error:
            return {'ok':False,'error':str(error)}
        if override_scope_group and override_scope_group != group_name:
            return {'ok':False,'error':'批准不能改变证据的来源群'}
        scope_master = get_master(conn)
        canonical_carrier = lambda value:scope_master.carrier_idx.get(fold(value)) or value
        conflicts = [dict(r) for r in conn.execute(
            "SELECT * FROM rule_candidates WHERE status='approved' AND target_layer='alias_scope' AND scope_kind=? AND kind=?",
            (cand['scope_kind'],cand['kind']))
            if normalized_scope(dict(r),canonical_carrier) == normalized_scope(cand,canonical_carrier)
            and fold(r['alias']) == fold(cand['alias']) and r['suggested_target'] != cand['suggested_target']]
        if conflicts:
            return {'ok':False,'error':'同作用域已有不同结论，先人工处理冲突','conflict_ids':[r['id'] for r in conflicts]}
        if cand['scope_kind'] == CONTEXT_ALIAS_SCOPE:
            master = get_master(conn)
            pools = {'pol':set(master.name_idx.values()) | set(master.group_by_name),
                     'pod':set(master.name_idx.values()) | set(master.group_by_name),
                     'carrier':set(master.carrier_idx.values()), 'route':set(master.route_idx.values())}
            if cand['suggested_target'] not in pools.get(cand['kind'],set()):
                return {'ok':False,'error':'上下文规则目标必须为有效主数据规范值'}
            conn.execute("UPDATE rule_candidates SET status='approved',reviewer_note=?,reviewed_by=?,reviewed_at=? WHERE id=?",
                         (CONTEXT_ALIAS_APPROVAL_NOTE,reviewer,now_cn(),cand_id))
            conn.commit();invalidate_master()
            return {'ok':True,'candidate_id':cand_id,'layer_action':{'ok':True,'action':'context_alias_saved'},
                    'candidate':{'alias':cand['alias'],'kind':cand['kind'],'scope_key':scope_key,
                                 'target_layer':layer,'suggested_target':cand['suggested_target']}}
        existing = [dict(r) for r in conn.execute('SELECT * FROM alias_scope WHERE kind=? AND scope_group=?',(cand['kind'],group_name))
                    if fold(r['alias']) == fold(cand['alias']) and r['target'] != cand['suggested_target']]
        if existing:
            return {'ok':False,'error':'该群已有不同别名映射，不能覆盖'}
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
        res = alias_scope_save(conn, save_row, commit=False)
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


def _candidate_pools(conn: sqlite3.Connection) -> Dict[str, List[str]]:
    """候选生成的规范名池：pol=港口、pod=港口+目的港群、carrier=船司代码、route=航线。"""
    pool_ports = [r[0] for r in conn.execute("SELECT name FROM ports WHERE active=1")]
    pool_pgs = [r[0] for r in conn.execute("SELECT name FROM port_groups WHERE active=1 AND kind='pod'")]
    pool_carriers = [r[0] for r in conn.execute("SELECT code FROM carriers WHERE active=1")]
    pool_routes = [r[0] for r in conn.execute("SELECT name FROM routes WHERE active=1")]
    return {"pol": pool_ports, "pod": pool_ports + pool_pgs,
            "carrier": pool_carriers, "route": pool_routes}


def suggest_candidates_for_failed(conn: sqlite3.Connection, row: Any,
                                  cutoff: float = 0.75,
                                  pools: Optional[Dict[str, List[str]]] = None,
                                  port_country: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
    """对单条 failed pending 行生成别名候选（difflib 找相近规范名）。
    stage_pending 识别失败落库后自动调用（auto_suggest），scan_failed_for_candidates 逐条复用。
    返回该行统计：{fields_examined, created, updated, skipped_*}。"""
    import difflib
    stats: Dict[str, Any] = {"fields_examined": 0, "created": 0, "updated": 0,
                             "skipped_rejected": 0, "skipped_entity_conflict": 0, "skipped_junk": 0, "skipped_hit": 0,
                             "skipped_no_match": 0, "skipped_geo": 0}
    try:
        errors = json.loads(row["errors"] or "[]")
        payload = json.loads(row["payload"] or "{}")
    except Exception:
        return stats
    group = payload["source_group"] if "source_group" in payload else (payload.get("来源群") or "未登记群")
    if pools is None:
        pools = _candidate_pools(conn)
    if port_country is None:
        port_country = {r[0]: (r[1] or "") for r in conn.execute("SELECT name, country FROM ports")}
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
        # 国家/地理软约束：有地理线索且目标港国家不在线索集合内、相似度又不够高 → 拒绝（防跨洲误配）
        geo = _geo_hint_for(payload)
        pc = port_country.get(best, "")
        if geo and pc and pc not in geo and score < 0.88:
            stats["skipped_geo"] += 1
            continue
        res = candidates_upsert(conn, {
            "scope_kind": "group+column",
            "scope_key": f"{kind}|{group}",  # 约定：群名总是末段
            "target_layer": "alias_scope",
            "pattern_type": "correction",
            "alias": raw,
            "kind": kind,
            "suggested_target": best,
            "reason": f"difflib 相似度 {score:.2f}，pending#{row['id']} errors 里 {field}=\"{raw}\" 未命中主数据"
                      + (f"，目标港国家 {pc}" if pc else ""),
            "confidence": round(score, 3),
            "evidence_refs": [row["id"]],
            "source": "difflib_scan",
        })
        if res["action"] == "created":
            stats["created"] += 1
        elif res["action"] == "updated":
            stats["updated"] += 1
        elif res["action"] == "skipped_entity_conflict":
            stats["skipped_entity_conflict"] += 1
        else:
            stats["skipped_rejected"] += 1
    return stats


def scan_failed_for_candidates(conn: sqlite3.Connection, limit: int = 2000,
                                cutoff: float = 0.75) -> Dict[str, Any]:
    """扫 pending_records 里 check_status='failed' 且 review_status='awaiting' 的行，
    对 errors 里"未命中主数据"类字段值用 difflib 找相近规范名 → 生成候选。
    跳过"含说明文字/占位符"类（那些是原文脏、不是未登记别名）。
    （与自动提出共用 suggest_candidates_for_failed；手动「跑一次扫描」入口保留，幂等累加。）"""
    rows = list(conn.execute(
        "SELECT id, kind, payload, errors FROM pending_records "
        "WHERE check_status='failed' AND review_status='awaiting' "
        "ORDER BY id DESC LIMIT ?", (int(limit),)
    ))
    pools = _candidate_pools(conn)
    port_country = {r[0]: (r[1] or "") for r in conn.execute("SELECT name, country FROM ports")}
    stats = {"scanned": len(rows), "fields_examined": 0,
             "candidates_created": 0, "candidates_updated": 0, "skipped_rejected": 0, "skipped_entity_conflict": 0,
             "skipped_junk": 0, "skipped_hit": 0, "skipped_no_match": 0, "skipped_geo": 0}
    for row in rows:
        st = suggest_candidates_for_failed(conn, row, cutoff, pools, port_country)
        # helper 返回键是 created/updated，此处映射回本函数/API 契约键 candidates_*
        for k in ("fields_examined", "skipped_rejected", "skipped_entity_conflict", "skipped_junk",
                  "skipped_hit", "skipped_no_match", "skipped_geo"):
            stats[k] += st[k]
        stats["candidates_created"] += st["created"]
        stats["candidates_updated"] += st["updated"]
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
    human_actors = {r[0] for r in conn.execute("SELECT name FROM operators WHERE role IN ('admin','operator')")}
    seen_sources = set()
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
            if e.get("action") != "update" or e.get("actor") not in human_actors or e.get("sync_from"):
                continue
            field = str(e.get("field") or "").strip()
            if not field or field == "消息原文":
                continue
            new = e.get("new")
            if new in (None, ""):
                continue
            old = str(e.get("old") or "")
            mid = str(payload.get("消息ID") or "").strip()
            if not mid or not group or old == str(new):
                continue
            if any(change.get("action") == "update" and change.get("field") in ("消息ID", "来源群", "发送人", "消息原文") for change in log_list):
                continue
            evidence_key = (group, sender, carrier, field, old, str(new), mid)
            if evidence_key in seen_sources:
                continue
            seen_sources.add(evidence_key)
            groups[(group, sender, carrier, field, old, str(new))].append(
                (str(e.get("ts") or ""), e.get("old"), row["id"]))
    stats = {"keys_examined": len(groups), "candidates_created": 0, "candidates_updated": 0,
             "skipped_rejected": 0, "skipped_entity_conflict": 0, "below_threshold": 0}
    for (group, sender, carrier, field, old_value, new), hits in groups.items():
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
                "reason": f"实名审核员直接修改「{field}」：{old_top} → {new}；{len(hits)} 条独立来源消息，跨度 {span_days} 天；仍须核对是否同一实体",
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
        elif res["action"] == "skipped_entity_conflict":
            stats["skipped_entity_conflict"] += 1
        else:
            stats["skipped_rejected"] += 1
    return {"ok": True, **stats}


def audit_hints(conn: sqlite3.Connection, group: Optional[str] = None,
                sender: Optional[str] = None, carrier: Optional[str] = None,
                limit: int = 500, groups: Optional[List[str]] = None, viewer: str = "") -> Dict[str, Any]:
    """P2 审核提示：实时聚合 edit_log → 各字段被改统计；再查 field_defaults 该 scope 生效的默认值。
    groups 多群模式：一次返回 {group: {field_stats, defaults}}（前端卡片内联惯例用，一次扫描按群分组）。
    单群/无群保持旧结构 {field_stats, defaults}。'能算出来就别存'：统计不落表，实时算。"""
    gset = [str(x).strip() for x in (groups or []) if str(x).strip()]
    if not gset and group:
        gset = [group]
    # 无 group/groups 时直接返回空（不统计）：前端必传 groups；空桶下循环 buckets[g] 会 KeyError
    if not gset:
        return {"ok": True, "field_stats": {}, "defaults": []}
    buckets: Dict[str, Dict[str, Any]] = {g: {"field_stats": {}, "defaults": []} for g in gset}
    scope_sql = scope_clause(conn, viewer, "json_extract(p.payload,'$.来源群')", "json_extract(p.payload,'$.航线')")
    scope_sql = " AND " + scope_sql if scope_sql else ""
    rows = conn.execute(
        "SELECT id, payload, edit_log FROM pending_records p "
        "WHERE edit_log IS NOT NULL AND edit_log NOT IN ('', '[]') "
        + scope_sql + " ORDER BY id DESC LIMIT ?", (int(limit),)
    )
    for row in rows:
        try:
            payload = json.loads(row["payload"] or "{}")
            log_list = json.loads(row["edit_log"] or "[]")
        except Exception:
            continue
        g = str(payload.get("来源群") or payload.get("source_group") or "").strip()
        if gset and g not in buckets:
            continue
        s = str(payload.get("发送人") or payload.get("sender") or "").strip()
        c = str(payload.get("船司") or "").strip()
        if sender and s != sender:
            continue
        if carrier and c != carrier:
            continue
        b = buckets[g]
        for e in log_list:
            if e.get("action") not in ("update", "approve_diff"):
                continue
            f = str(e.get("field") or "").strip()
            new = e.get("new")
            if not f or f == "消息原文" or new in (None, ""):
                continue
            st = b["field_stats"].setdefault(f, {"count": 0, "values": {}, "last_ts": ""})
            st["count"] += 1
            st["values"][str(new)] = st["values"].get(str(new), 0) + 1
            ts = str(e.get("ts") or "")
            if ts > st["last_ts"]:
                st["last_ts"] = ts
    account = dict(conn.execute("SELECT * FROM operators WHERE name=?", (viewer,)).fetchone() or {}) if viewer else None
    from services.access_policy import record_in_scope
    for g in gset:
        b = buckets[g]
        for f, st in b["field_stats"].items():
            top = sorted(st["values"].items(), key=lambda x: -x[1])[:3]
            st["top"] = [{"value": v, "n": n} for v, n in top]
            del st["values"]
        if (sender or carrier or g) and (not viewer or record_in_scope(account, {"来源群": g})):
            exact = f"{sender or ''}|{carrier or ''}|{g}"
            q = ("SELECT scope_key, field, default_value, evidence_count, source, note "
                 "FROM field_defaults WHERE status='active' AND (scope_key = ? OR scope_key LIKE ?) "
                 "ORDER BY evidence_count DESC LIMIT 20")
            b["defaults"] = [dict(r) for r in conn.execute(q, (exact, f"%|{g}"))]
    if len(gset) == 1:
        b = buckets[gset[0]]
        return {"ok": True, "field_stats": b["field_stats"], "defaults": b["defaults"]}
    return {"ok": True, "hints": {g: buckets[g] for g in gset}}


def group_accuracy_stats(conn: sqlite3.Connection, hours: Optional[int] = None, viewer: str = "") -> Dict[str, Any]:
    """按已登记来源群聚合 pending 历史，产出识别准确率看板数据。
    hours=None 全量；hours=N 只看近 N 小时（按完整 created_at）。
    口径：
      total    pending 总数
      clean    已放行、check=passed 且无字段修改记录；比例分母仅已审（放行+驳回）
      failed   check=failed
      rejected review_status=rejected
      approved review_status=approved
      avg_edits 每条平均被改字段数（update/approve_diff 次数）
      top_fields 老被改字段 TOP3 [{field, n}]

    群聊库是唯一群名单：未登记、空来源群以及库内没有历史样本的群都不进入结果。
    """
    registered_groups = {
        str(row["name"] or "").strip()
        for row in conn.execute("SELECT name FROM chat_groups")
        if str(row["name"] or "").strip()
    }
    scope_sql = scope_clause(conn, viewer, "json_extract(p.payload,'$.来源群')", "json_extract(p.payload,'$.航线')")
    scope_sql = " AND " + scope_sql if scope_sql else ""
    rows = list(conn.execute(
        "SELECT id, kind, payload, check_status, review_status, edit_log, created_at FROM pending_records p WHERE 1=1" + scope_sql
    ))
    # 时间过滤
    if hours:
        from datetime import datetime as _dt, timedelta as _td
        cutoff = (_dt.now(timezone(timedelta(hours=8))) - _td(hours=hours)).strftime("%Y-%m-%d %H:%M:%S")
        rows = [r for r in rows if str(r["created_at"] or "") >= cutoff]

    grp: Dict[str, Dict[str, Any]] = {}
    for r in rows:
        try:
            p = json.loads(r["payload"] or "{}")
        except Exception:
            continue
        g = str(p.get("来源群") or p.get("source_group") or "").strip()
        if not g or g not in registered_groups:
            continue
        b = grp.setdefault(g, {
            "group": g, "total": 0, "clean": 0, "failed": 0,
            "rejected": 0, "approved": 0, "edit_sum": 0,
            "awaiting": 0, "modified_approved": 0, "mechanical_passed": 0,
            "field_counter": {},
        })
        b["total"] += 1
        if r["review_status"] == "rejected":
            b["rejected"] += 1
        elif r["review_status"] == "approved":
            b["approved"] += 1
        elif r['review_status'] == 'awaiting':
            b['awaiting'] += 1
        if r['check_status'] == 'passed':
            b['mechanical_passed'] += 1
        if r["check_status"] == "failed":
            b["failed"] += 1
        # 解析 edit_log 里真正的字段修改
        try:
            logs = json.loads(r["edit_log"] or "[]")
        except Exception:
            logs = []
        n_edit = 0
        for e in logs:
            if e.get("action") in ("update", "approve_diff") and e.get("field"):
                n_edit += 1
                f = str(e["field"]).strip()
                b["field_counter"][f] = b["field_counter"].get(f, 0) + 1
        b["edit_sum"] += n_edit
        if n_edit == 0 and r["check_status"] == "passed" and r['review_status'] == 'approved':
            b["clean"] += 1
        if n_edit and r['review_status'] == 'approved':
            b['modified_approved'] += 1

    out = []
    for g, b in grp.items():
        t = b["total"] or 1
        top = sorted(b["field_counter"].items(), key=lambda x: -x[1])[:3]
        out.append({
            "group": g,
            "total": b["total"],
            "clean": b["clean"],
            "failed": b["failed"],
            "rejected": b["rejected"],
            "approved": b["approved"],
            "awaiting": b['awaiting'],
            "modified_approved": b['modified_approved'],
            "mechanical_passed": b['mechanical_passed'],
            "mechanical_pass_rate": round(b['mechanical_passed'] / t * 100, 1),
            "reviewed_total": b['approved'] + b['rejected'],
            "clean_rate": round(b['clean'] / (b['approved'] + b['rejected']) * 100, 1) if b['approved'] + b['rejected'] else None,
            "failed_rate": round(b["failed"] / t * 100, 1),
            "reject_rate": round(b["rejected"] / t * 100, 1),
            "avg_edits": round(b["edit_sum"] / t, 2),
            "top_fields": [{"field": f, "n": n} for f, n in top],
        })
    out.sort(key=lambda x: -x["total"])
    return {"ok": True, "hours": hours, "groups": out, "total_groups": len(out)}


def sync_rows(conn: sqlite3.Connection, table: str, rows: List[Dict[str, Any]],
              commit: bool = True) -> int:
    """批量导入英文列名行：主数据按主键 merge，业务表按 content_key merge。

    ``load_snapshot`` passes ``commit=False`` so an import cannot expose a
    partially restored database when one later table fails validation.
    """
    # 旧同步调用仍可传 freight_rates / space_records；统一改写到唯一业务表。
    if table in ("freight_rates", "space_records"):
        record_type = "rates" if table == "freight_rates" else "space"
        table = "freight_items"
        rows = [dict(r, record_type=r.get("record_type") or record_type,
                     price_usd=r.get("price_usd", r.get("freight_usd"))) for r in rows]
    if table == PORT_ROUTES_TABLE:
        for r in rows:
            conn.execute("INSERT INTO port_routes(port_name,route_name,source,note,created_at) "
                         "VALUES(?,?,?,?,?) ON CONFLICT(port_name,route_name) DO UPDATE SET "
                         "source=excluded.source,note=excluded.note,created_at=excluded.created_at",
                         (r["port_name"], r["route_name"], r.get("source") or "imported",
                          r.get("note"), r.get("created_at")))
        if commit:
            _mirror_port_routes(conn)
            conn.commit()
        invalidate_master()
        return len(rows)
    composite_keys = {"alias_scope": ("alias", "kind", "scope_group"),
                      "sales_group_visits": ("user_id", "source_group")}
    id_tables = {"pending_records", "pending_shadow", "rule_candidates", "field_defaults"}
    pk: Optional[str] = MASTER_TABLES.get(table, {}).get("pk")
    if pk is None:
        pk = {"operators": "name", "chat_groups": "name", "account_recovery_requests": "name", "announcements": "id",
              NOTIFICATION_TABLE:"notification_id",
              'account_profiles':'user_id','account_security':'user_id','account_events':'event_id',
              'quote_publishers':'publisher_id','publisher_sources':'source_key','publisher_legacy_names':'sender_name','publisher_dingtalk_ids':'identity_key',
              GROWTH_TABLE:'event_id','item_feedback':'id'}.get(table)
    if table in id_tables:
        pk = "id"
    if pk is None and any(s["table"] == table for s in TABLES.values()):
        pk = "content_key"
    if pk is None and table not in composite_keys:
        raise ValueError(f"unknown table: {table}")
    keys = composite_keys.get(table, (pk,))
    cols = [r[1] for r in conn.execute(f"PRAGMA table_info({table})") if r[1] != "id" or pk == "id"]
    ph = ",".join("?" * len(cols))
    upd = ",".join(f"{c}=excluded.{c}" for c in cols if c not in keys)
    sql = (f'INSERT INTO {table} ({",".join(cols)}) VALUES ({ph}) '
           f'ON CONFLICT({",".join(keys)}) DO UPDATE SET {upd}')
    for r in rows:
        if table == 'announcements':
            cleaned = notice_form(dict(r, tags=json.loads(r.get('tags_json') or '[]')), [r.get('group_name')])
            r = dict(r, title=cleaned['title'], body_html=cleaned['body_html'],
                     tags_json=json.dumps(cleaned['tags'], ensure_ascii=False))
            for column in NOTICE_SCOPE_COLUMNS:
                r[column] = r.get(column) or '[]'
        if table == "operators":
            existing = conn.execute("SELECT user_id,publisher_name,publisher_id FROM operators WHERE name=?", (r.get("name"),)).fetchone()
            identity = existing["user_id"] if existing and existing["user_id"] else r.get("user_id") or legacy_user_id(r["name"])
            publisher_name = r.get("publisher_name") or (existing["publisher_name"] if existing else None) or r["name"]
            r = dict(r, user_id=identity, publisher_name=publisher_name, publisher_id=(existing["publisher_id"] if existing else None) or r.get("publisher_id"))
        if table in ('freight_items','pending_records') and not r.get('publisher_id'):
            existing = conn.execute('SELECT publisher_id FROM '+table+' WHERE '+pk+'=?', (r.get(pk),)).fetchone()
            if existing:
                r = dict(r, publisher_id=existing['publisher_id'])
        conn.execute(sql, [r.get(c) for c in cols])
    if table in id_tables and getattr(conn, "engine", "sqlite") == "postgres":
        conn.execute("SELECT setval(pg_get_serial_sequence(?, 'id'), "
                     f"GREATEST((SELECT COALESCE(MAX(id),1) FROM {table}), "
                     "nextval(pg_get_serial_sequence(?, 'id'))), true)", (table, table))
    if table == "freight_items":     # 批量导入也走"写入即登记"，只补不覆盖
        _register_pairs(conn, [{"source_group": r.get("source_group"),
                                "sender": r.get("sender")} for r in rows], commit=commit)
    if table in MASTER_TABLES:
        invalidate_master()      # 主数据一改，归一索引必须重建
    if commit:
        if table in ("ports", "routes"):
            _backfill_port_routes(conn)
        conn.commit()
    return len(rows)


def _mirror_port_routes(conn) -> None:
    memberships: Dict[str, List[str]] = {}
    for row in conn.execute("SELECT port_name,route_name FROM port_routes ORDER BY route_name"):
        memberships.setdefault(row["port_name"], []).append(row["route_name"])
    for row in conn.execute("SELECT name FROM ports").fetchall():
        conn.execute("UPDATE ports SET route=? WHERE name=?",
                     (",".join(memberships.get(row["name"], [])), row["name"]))


def export_all(conn: sqlite3.Connection) -> Dict[str, Any]:
    """整库快照（备份 / 迁移用）。"""
    out: Dict[str, Any] = {"ok": True, "exported_at": now_cn()}
    # 会话令牌摘要属于短期认证状态，不应进入可携带的业务快照；导入后要求重新登录。
    tables = [t for t in DDL if t != "auth_sessions" and t not in PROSPECTOR_PRIVATE_TABLES]
    for t in tables:
        out[t] = [dict(r) for r in conn.execute(f"SELECT * FROM {t}")]
    # 兼容旧快照消费者；新系统的唯一可写业务表仍是 freight_items。
    out["freight_rates"] = [dict(r) for r in conn.execute("SELECT * FROM freight_rates")]
    out["space_records"] = [dict(r) for r in conn.execute("SELECT * FROM space_records")]
    return out


def load_snapshot(conn: sqlite3.Connection, snap: Dict[str, Any]) -> str:
    """导入整库快照，按主键 merge，可重复跑。

    主数据表额外做"对齐"：改过名的旧行留着会让别名索引产生歧义
    （实测残留一条旧 Manzanillo 就把「曼萨尼约」卡成多义、录入端不再归一）。
    """
    if not isinstance(snap, dict):
        raise ValueError("snapshot must be an object")
    snapshot_tables = [t for t in DDL if t != "auth_sessions" and t not in PROSPECTOR_PRIVATE_TABLES]
    missing = [t for t in snapshot_tables if t not in snap and t not in (PORT_ROUTES_TABLE, 'announcements',NOTIFICATION_TABLE,GROWTH_TABLE,ACTIVITY_TABLE,QUOTE_USAGE_TABLE,API_EVENT_TABLE,'sales_group_visits','item_feedback',*ADMIN_TABLES,*PUBLISHER_TABLES)]
    if missing:
        raise ValueError("snapshot missing tables: " + ", ".join(missing))
    parts = []
    try:
        conn.execute("BEGIN IMMEDIATE")
        # 登录态不属于业务快照；导入后统一失效，避免旧 cookie 跨恢复继续可用。
        conn.execute("DELETE FROM auth_sessions")
        for private_table in PROSPECTOR_PRIVATE_TABLES:
            conn.execute("DELETE FROM " + private_table)
        # Relation restoration follows its parents; old snapshots rebuild from legacy route.
        conn.execute("DELETE FROM port_routes")
        for t in snapshot_tables:
            if t == PORT_ROUTES_TABLE:
                continue
            rows = [r for r in (snap.get(t) or []) if r]
            if not rows:
                parts.append(f"{t}=0")
                continue
            parts.append(f"{t}={sync_rows(conn, t, rows, commit=False)}")
            if t in MASTER_TABLES:
                pk = MASTER_TABLES[t]["pk"]
                keep = {r[pk] for r in rows if r.get(pk)}
                if not keep:
                    raise ValueError(f"snapshot {t} has no primary keys")
                gone = conn.execute(f"DELETE FROM {t} WHERE {pk} NOT IN ({','.join('?' * len(keep))})",
                                    list(keep)).rowcount
                if gone:
                    parts[-1] += f"(清旧{gone})"
        if PORT_ROUTES_TABLE in snap:
            sync_rows(conn, PORT_ROUTES_TABLE, snap[PORT_ROUTES_TABLE] or [], commit=False)
            _mirror_port_routes(conn)
        else:
            _backfill_port_routes(conn)
        ensure_publisher_schema(conn, now_cn())
        parts.append("port_routes=" + str(conn.execute("SELECT COUNT(*) FROM port_routes").fetchone()[0]))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    invalidate_master()
    return " ".join(parts)


# Narrow repair repository operations. The transport supplies an authenticated actor.
def agent_master_ensure(conn, body):
    from core.contracts import ok
    from services.agent_repair import master_request
    entity, row, evidence = master_request(body)
    if getattr(conn, "engine", "") == "postgres":
        conn.execute("SELECT pg_advisory_xact_lock(hashtext(?))", ("agent-master:" + entity + ":" + str(row.get("name") or (body["source_group"] + ":" + row["kind"] + ":" + row["alias"])),))
    elif not conn.in_transaction:
        conn.execute("BEGIN IMMEDIATE")
    group = str(body["source_group"]).strip()
    if not conn.execute("SELECT 1 FROM chat_groups WHERE name=?", (group,)).fetchone():
        raise ValueError("source group is not registered")
    note = "Agent verified " + json.dumps({"group": group, "message_id": body["message_id"],
        "evidence": evidence, "actor": body.get("_actor") or "agent"}, ensure_ascii=False)
    if entity == "port":
        previous = conn.execute("SELECT name,country,active FROM ports WHERE name=?", (row["name"],)).fetchone()
        if previous:
            if previous["country"] != row["country"] or not previous["active"]:
                raise ValueError("existing port conflicts; additive repair will not overwrite it")
            return ok({"entity": entity, "action": "existing", "key": row["name"]})
        result = master_save(conn, "ports", dict(row, active=1, source="agent_verified", note=note))
    else:
        master = get_master(conn)
        kind, target = row["kind"], row["target"]
        if kind in ("pol", "pod"):
            # 港群也是正式的目的地实体；lookup_port 只覆盖普通港口/别名，
            # 不能用它单独判断 NEB/ECSA 这类港群目标是否存在。
            port_names = {p["name"] for p in master.ports}
            group_names = set(master.group_by_name)
            valid = target in (port_names | group_names)
            if valid and target in group_names:
                valid = master.group_by_name[target].get("kind") in (kind, "both")
            elif valid:
                resolved, hit = master.lookup_port(target, kind, group)
                valid = hit and resolved == target
        else:
            index = master.carrier_idx if kind == "carrier" else master.route_idx
            valid = target in index.values() and master.lookup(target, index) == target
        if not valid:
            raise ValueError("alias target must be an exact active canonical entity")
        previous = conn.execute("SELECT target FROM alias_scope WHERE alias=? AND kind=? AND scope_group=?",
            (row["alias"], kind, group)).fetchone()
        if previous:
            if previous["target"] != target:
                raise ValueError("existing scoped alias conflicts; will not overwrite it")
            return ok({"entity": entity, "action": "existing", "key": row["alias"]})
        resolved, hit = master.lookup_port(row["alias"], kind, group) if kind in ("pol", "pod") else (index.get(fold(row["alias"]), ""), fold(row["alias"]) in index)
        if hit and resolved != target:
            raise ValueError("alias already identifies a different entity")
        result = alias_scope_save(conn, dict(row, scope_group=group, note=note, source="manual"))
        if result.get("warnings"):
            raise RuntimeError("validated alias unexpectedly produced warnings")
    return ok({"entity": entity, "action": result["action"], "key": result["key"], "source_group": group})


def agent_pending_repair(conn, body):
    from core.contracts import ok
    from services.agent_repair import pending_request, payload_signature
    pid, fields = pending_request(body)
    locked_query = "SELECT * FROM pending_records WHERE id=?"
    if getattr(conn, "engine", "") == "postgres":
        locked_query += " FOR UPDATE"
    elif not conn.in_transaction:
        conn.execute("BEGIN IMMEDIATE")
    row = conn.execute(locked_query, (pid,)).fetchone()
    if not row or row["origin"] != "agent" or row["review_status"] != "awaiting":
        raise ValueError("only unreviewed Agent pending rows can be repaired")
    payload = json.loads(row["payload"])
    if payload_signature(payload) != body["expected_signature"]:
        raise ValueError("pending payload changed; fetch and diagnose again")
    retain_failed = body.get("retain_failed", False)
    if retain_failed:
        image_names = set(str(payload.get("图片名") or "").split(",")) - {""}
        original_image = any(e["kind"] == "original_image" and e["reference"] in image_names for e in body["evidence"])
        text_source = not image_names and bool(str(payload.get("消息ID") or "").strip()) and bool(str(payload.get("消息原文") or "").strip())
        group_rule = any(e["kind"] == "group_spec" for e in body["evidence"])
        if row["check_status"] != "failed" or row["edit_log"] or not (original_image or (text_source and group_rule)):
            raise ValueError("failed source correction requires an untouched failed Agent row and source evidence")
    result = update_pending(conn, get_master(conn), pid, fields, kind=body.get("kind"), actor=body.get("_actor") or "agent", commit=False,
                            preserve_unresolved=retain_failed)
    if not result.get("ok") or result.get("rejected") or (result.get("check_status") != "passed" and not retain_failed):
        conn.rollback()
        raise ValueError("repair still fails validation: " + json.dumps(result.get("errors") or result.get("rejected") or [], ensure_ascii=False))
    conn.commit()
    return ok(result)
