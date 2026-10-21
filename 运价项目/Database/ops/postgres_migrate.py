# -*- coding: utf-8 -*-
"""Create and verify a PostgreSQL copy of the freight ledger.

Default mode deliberately migrates configuration only. Business data requires
an explicit flag, and an existing non-empty target is refused unless the
operator explicitly permits replacement.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sqlite3
import sys
from pathlib import Path
from typing import Any, Dict, Iterable, List, Sequence

HERE = Path(__file__).resolve().parent
DATABASE_DIR = HERE.parent
sys.path.insert(0, str(DATABASE_DIR))
import manualdb  # noqa: E402

POSTGRES_DSN_ENV = "FREIGHT_POSTGRES_DSN"
SESSION_TABLE = "auth_sessions"
BUSINESS_TABLES = ("freight_items", "pending_records", "pending_shadow", "rule_candidates", "announcements", "account_notifications", "account_events", "account_experience_events", "item_feedback")
CONFIGURATION_TABLES = ("operators", "chat_groups", "ports", "carriers", "routes", "senders",
                        "port_groups", "alias_scope", "field_defaults", "port_routes", "account_profiles", "account_security", *manualdb.PUBLISHER_TABLES)
MIGRATION_TABLES = CONFIGURATION_TABLES + BUSINESS_TABLES
DATE_GLOB = "'[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'"


def postgres_schema() -> List[str]:
    """Translate the controlled SQLite schema into PostgreSQL DDL.

    Runtime SQL remains SQLite-bound today. This schema/migration tool is the
    verified handoff boundary; it does not claim that the HTTP server can use
    PostgreSQL until its persistence adapter is implemented and integration
    tested against a real PostgreSQL service.
    """
    statements: List[str] = []
    for ddl in manualdb.DDL.values():
        pg = ddl.replace("id INTEGER PRIMARY KEY AUTOINCREMENT", "id BIGSERIAL PRIMARY KEY")
        pg = pg.replace("created_at TEXT DEFAULT CURRENT_TIMESTAMP",
                        "created_at TEXT DEFAULT to_char(CURRENT_TIMESTAMP AT TIME ZONE 'Asia/Shanghai', 'YYYY-MM-DD HH24:MI:SS')")
        pg = re.sub(r"\(typeof\((\w+)\) = 'integer' AND \1 >= 0\)", r"\1 >= 0", pg)
        pg = pg.replace(f"valid_from GLOB {DATE_GLOB}", "valid_from ~ '^[0-9]{4}-[0-9]{2}-[0-9]{2}$'")
        pg = pg.replace(f"valid_to GLOB {DATE_GLOB}", "valid_to ~ '^[0-9]{4}-[0-9]{2}-[0-9]{2}$'")
        statements.append(pg)
    statements.extend([
        "ALTER TABLE port_groups ADD COLUMN IF NOT EXISTS aliases TEXT",
        "ALTER TABLE announcements ADD COLUMN IF NOT EXISTS expires_on TEXT",
        "ALTER TABLE pending_records ADD COLUMN IF NOT EXISTS orig_payload TEXT",
        "ALTER TABLE pending_records ADD COLUMN IF NOT EXISTS edit_log TEXT",
        "ALTER TABLE freight_items ADD COLUMN IF NOT EXISTS entry_id TEXT",
        "UPDATE freight_items SET entry_id='frt_' || substr(md5('freight:' || id::text),1,32) WHERE entry_id IS NULL OR entry_id=''",
        "ALTER TABLE freight_items ALTER COLUMN entry_id SET NOT NULL",
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_freight_items_entry_id ON freight_items(entry_id)",
        "ALTER TABLE pending_records ADD COLUMN IF NOT EXISTS entry_id TEXT",
        "UPDATE pending_records SET entry_id='frt_' || substr(md5('pending:' || id::text),1,32) WHERE entry_id IS NULL OR entry_id=''",
        "ALTER TABLE pending_records ALTER COLUMN entry_id SET NOT NULL",
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_pending_records_entry_id ON pending_records(entry_id)",
        "CREATE OR REPLACE VIEW freight_rates AS "
        "SELECT *, price_usd AS freight_usd FROM freight_items WHERE record_type='rates'",
        "CREATE OR REPLACE VIEW space_records AS "
        "SELECT * FROM freight_items WHERE record_type='space'",
    ])
    for table, columns in manualdb.INDEX_PLAN.items():
        for column in columns:
            statements.append(f"CREATE INDEX IF NOT EXISTS idx_{table}_{column} ON {table}({column})")
    from ops.item_feedback import ACTIVE_INDEX, ENTRY_INDEX
    statements.extend((ACTIVE_INDEX, ENTRY_INDEX))
    return statements


def selected_tables(include_business_data: bool) -> Sequence[str]:
    return MIGRATION_TABLES if include_business_data else CONFIGURATION_TABLES


def rows_for_table(conn: sqlite3.Connection, table: str) -> tuple[List[str], List[Dict[str, Any]]]:
    columns = [row[1] for row in conn.execute(f"PRAGMA table_info({table})")]
    if table in ('announcements','account_notifications',manualdb.GROWTH_TABLE,'item_feedback',*manualdb.ADMIN_TABLES, *manualdb.PUBLISHER_TABLES) and not columns:
        # Pre-board archives contain no announcements; derive columns from the contract.
        with sqlite3.connect(':memory:') as schema:
            schema.execute(manualdb.DDL[table])
            return [row[1] for row in schema.execute('PRAGMA table_info('+table+')')], []
    if table == "port_routes" and not columns:
        # Compatibility with pre-membership SQLite archives; source stays read-only.
        columns = ["port_name", "route_name", "source", "note", "created_at"]
        known = {row[0] for row in conn.execute("SELECT name FROM routes")}
        rows = []
        for port in conn.execute("SELECT name,route FROM ports"):
            for route in manualdb.route_tokens(port["route"]):
                if route not in known:
                    raise ValueError("legacy port route is unknown: " + port["name"])
                rows.append(dict(port_name=port["name"], route_name=route, source="legacy",
                                 note=None, created_at=None))
        return columns, rows
    rows = [dict(row) for row in conn.execute(f"SELECT * FROM {table}")]
    if table == "operators" and "user_id" not in columns:
        columns.append("user_id")
        rows = [dict(row, user_id=manualdb.legacy_user_id(row["name"])) for row in rows]
    return columns, rows


def digest_rows(rows: Iterable[Dict[str, Any]]) -> str:
    """Order-independent data digest used to verify a table after migration."""
    serialized = sorted(json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
                        for row in rows)
    payload = "\n".join(serialized).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def migration_plan(sqlite_path: Path, include_business_data: bool) -> Dict[str, Any]:
    source = sqlite3.connect(str(sqlite_path))
    source.row_factory = sqlite3.Row
    try:
        tables = selected_tables(include_business_data)
        return {
            "source": str(sqlite_path.resolve()),
            "mode": "configuration_and_business" if include_business_data else "configuration_only",
            "tables": {table: len(rows_for_table(source, table)[1]) for table in tables},
            "excluded": [SESSION_TABLE] + ([] if include_business_data else list(BUSINESS_TABLES)),
        }
    finally:
        source.close()


def _load_psycopg():
    try:
        import psycopg
        from psycopg import sql
    except ModuleNotFoundError as exc:
        raise RuntimeError("缺少 psycopg。请用 requirements-postgres.txt 安装 PostgreSQL 驱动。") from exc
    return psycopg, sql


def _create_schema(cur: Any) -> None:
    for statement in postgres_schema():
        cur.execute(statement)


def _target_counts(cur: Any, sql: Any, tables: Sequence[str]) -> Dict[str, int]:
    counts: Dict[str, int] = {}
    for table in tables:
        cur.execute(sql.SQL("SELECT COUNT(*) FROM {}").format(sql.Identifier(table)))
        counts[table] = int(cur.fetchone()[0])
    return counts


def _truncate_target(cur: Any, sql: Any, tables: Sequence[str]) -> None:
    identifiers = sql.SQL(", ").join(sql.Identifier(table) for table in tables)
    cur.execute(sql.SQL("TRUNCATE TABLE {} RESTART IDENTITY CASCADE").format(identifiers))


def _copy_table(cur: Any, sql: Any, table: str, columns: Sequence[str], rows: Sequence[Dict[str, Any]]) -> None:
    if not rows:
        return
    query = sql.SQL("INSERT INTO {} ({}) VALUES ({})").format(
        sql.Identifier(table),
        sql.SQL(", ").join(sql.Identifier(column) for column in columns),
        sql.SQL(", ").join(sql.Placeholder() for _ in columns),
    )
    cur.executemany(query, [tuple(row.get(column) for column in columns) for row in rows])
    if "id" in columns and table != 'announcements':
        cur.execute(sql.SQL(
            "SELECT setval(pg_get_serial_sequence({}, 'id'), GREATEST((SELECT COALESCE(MAX(id), 1) FROM {}), 1), true)"
        ).format(sql.Literal(table), sql.Identifier(table)))


def _target_rows(cur: Any, sql: Any, table: str, columns: Sequence[str]) -> List[Dict[str, Any]]:
    cur.execute(sql.SQL("SELECT {} FROM {}").format(
        sql.SQL(", ").join(sql.Identifier(column) for column in columns), sql.Identifier(table)))
    return [dict(zip(columns, row)) for row in cur.fetchall()]


def migrate(sqlite_path: Path, dsn: str, include_business_data: bool, replace_target: bool) -> Dict[str, Any]:
    psycopg, sql = _load_psycopg()
    source = sqlite3.connect(str(sqlite_path))
    source.row_factory = sqlite3.Row
    tables = selected_tables(include_business_data)
    try:
        source_data = {table: rows_for_table(source, table) for table in tables}
        with psycopg.connect(dsn) as target:
            with target.cursor() as cur:
                _create_schema(cur)
                existing = _target_counts(cur, sql, tables)
                if any(existing.values()):
                    if not replace_target:
                        raise RuntimeError("目标 PostgreSQL 已有数据，拒绝覆盖。确认后使用 --replace-target。")
                    _truncate_target(cur, sql, tables)
                for table, (columns, rows) in source_data.items():
                    _copy_table(cur, sql, table, columns, rows)
                verification: Dict[str, Dict[str, Any]] = {}
                for table, (columns, rows) in source_data.items():
                    copied = _target_rows(cur, sql, table, columns)
                    source_digest, target_digest = digest_rows(rows), digest_rows(copied)
                    verification[table] = {
                        "source_rows": len(rows), "target_rows": len(copied),
                        "source_digest": source_digest, "target_digest": target_digest,
                        "ok": len(rows) == len(copied) and source_digest == target_digest,
                    }
                failed = [table for table, result in verification.items() if not result["ok"]]
                if failed:
                    raise RuntimeError("迁移校验失败：" + ", ".join(failed))
        return {"ok": True, "mode": "configuration_and_business" if include_business_data else "configuration_only",
                "tables": verification, "sessions_migrated": False}
    finally:
        source.close()


def main() -> int:
    parser = argparse.ArgumentParser(description="SQLite 运价库迁移到 PostgreSQL（默认只迁配置）")
    parser.add_argument("--sqlite", type=Path, default=manualdb.DEFAULT_DB)
    parser.add_argument("--include-business-data", action="store_true", help="明确要求迁移报价、待审和候选历史")
    parser.add_argument("--replace-target", action="store_true", help="危险：清空目标中的本项目表后重迁")
    parser.add_argument("--dry-run", action="store_true", help="仅显示计划和源库行数，不连接 PostgreSQL")
    args = parser.parse_args()
    if not args.sqlite.is_file():
        raise SystemExit(f"SQLite 文件不存在：{args.sqlite}")
    plan = migration_plan(args.sqlite, args.include_business_data)
    if args.dry_run:
        print(json.dumps({"ok": True, "dry_run": True, **plan}, ensure_ascii=False, indent=2))
        return 0
    dsn = os.environ.get(POSTGRES_DSN_ENV, "").strip()
    if not dsn:
        raise SystemExit(f"未设置 {POSTGRES_DSN_ENV}。请在本机受控环境变量中配置，不要写入代码或对话。")
    result = migrate(args.sqlite, dsn, args.include_business_data, args.replace_target)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
