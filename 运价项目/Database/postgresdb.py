# -*- coding: utf-8 -*-
"""PostgreSQL connection adapter for the ledger's existing DB-API surface."""
from __future__ import annotations

import re
import time
import logging
from queue import LifoQueue, Empty
from threading import Lock
from pathlib import Path
from typing import Any, Iterable

import psycopg

from ops.postgres_sync import load_dsn

DEFAULT_ENV_FILE = Path(r"C:\ProgramData\FreightRate\freight-postgres.env")
ID_TABLES = frozenset({"pending_records", "rule_candidates"})
TABLE_INFO = re.compile(r"^PRAGMA\s+table_info\((\w+)\)$", re.IGNORECASE)
INSERT_TABLE = re.compile(r"^\s*INSERT\s+INTO\s+(\w+)\b", re.IGNORECASE)
DATE_VALUE = re.compile(r"\bdate\(valid_(from|to)\)", re.IGNORECASE)
MESSAGE_ID_JSON = re.compile(r"\bjson_extract\(((?:\w+\.)?(?:payload|resolved)),'\$\.(消息ID|来源群|航线|发送人)'\)", re.IGNORECASE)


class CompatRow(dict):
    """Allow both sqlite3.Row-style numeric and named access."""

    def __init__(self, names: list[str], values: tuple[Any, ...]):
        super().__init__(zip(names, values))
        self._values = values

    def __getitem__(self, key: Any) -> Any:
        return self._values[key] if isinstance(key, int) else super().__getitem__(key)


def _row_factory(cursor: Any):
    if cursor.description is None:
        return lambda values: values
    names = [column.name for column in cursor.description]
    return lambda values: CompatRow(names, values)


def _placeholders(statement: str) -> str:
    """Convert qmarks and escape SQL percent signs for psycopg binding.

    Input uses SQLite qmarks. Every existing percent sign is literal SQL;
    psycopg requires it doubled even inside strings when parameters are passed.
    Generated %s placeholders remain single, and bound values are untouched.
    """
    out: list[str] = []
    quoted = False
    index = 0
    while index < len(statement):
        char = statement[index]
        if char == "'":
            out.append(char)
            if quoted and index + 1 < len(statement) and statement[index + 1] == "'":
                out.append("'")
                index += 2
                continue
            quoted = not quoted
        elif char == "%":
            out.append("%%")
        elif char == "?" and not quoted:
            out.append("%s")
        else:
            out.append(char)
        index += 1
    return "".join(out)


class CompatCursor:
    def __init__(self, cursor: Any, lastrowid: int | None = None):
        self._cursor = cursor
        self.lastrowid = lastrowid
        self.rowcount = cursor.rowcount

    def fetchone(self):
        return self._cursor.fetchone()

    def fetchall(self):
        return self._cursor.fetchall()

    def __iter__(self):
        return iter(self._cursor)


class Connection:
    engine = "postgres"

    def __init__(self, dsn: str):
        self._conn = psycopg.connect(dsn, row_factory=_row_factory)

    def ensure_identity_schema(self) -> None:
        """Startup-only upgrade. Request connections must never acquire DDL locks."""
        from core.notice_contracts import NOTICE_DDL
        from core.notification_contracts import NOTIFICATION_DDL, NOTIFICATION_TABLE
        from core.growth_contracts import GROWTH_DDL, GROWTH_TABLE, GROWTH_SYNC_DDL, GROWTH_SYNC_TABLE
        from core.activity_contracts import (ACTIVITY_DDL, ACTIVITY_INDEX_COLUMNS, ACTIVITY_TABLE,
                                             QUOTE_USAGE_DDL, QUOTE_USAGE_INDEX_COLUMNS, QUOTE_USAGE_TABLE)
        from core.api_monitor_contracts import API_EVENT_DDL, API_EVENT_INDEX_COLUMNS, API_EVENT_TABLE
        from core.message_board_contracts import MESSAGE_BOARD_DDL, MESSAGE_BOARD_TABLE
        from core.prospector_contracts import DDL as PROSPECTOR_DDL
        from core.company_rates_contracts import COMPANY_RATE_DDL
        for statement in PROSPECTOR_DDL.values():
            self._conn.execute(statement)
        for statement in COMPANY_RATE_DDL.values():
            self._conn.execute(statement)
        # Company business-user identifiers can exceed signed 32-bit integers.
        for column in ('business_user_id', 'company_id'):
            self._conn.execute(f'ALTER TABLE company_mappings ALTER COLUMN {column} TYPE BIGINT')
        for column in ("user_id", "kind", "query_signature", "created_at"):
            self._conn.execute(f"CREATE INDEX IF NOT EXISTS idx_company_rate_tasks_{column} ON company_rate_tasks({column})")
        self._conn.execute("CREATE INDEX IF NOT EXISTS idx_company_rate_results_expires_at ON company_rate_results(expires_at)")
        self._conn.execute(NOTICE_DDL)
        self._conn.execute('ALTER TABLE announcements ADD COLUMN IF NOT EXISTS expires_on TEXT')
        from core.notice_contracts import NOTICE_SCOPE_COLUMNS
        for column in NOTICE_SCOPE_COLUMNS:
            self._conn.execute(f"ALTER TABLE announcements ADD COLUMN IF NOT EXISTS {column} TEXT NOT NULL DEFAULT '[]'")
        self._conn.execute(NOTIFICATION_DDL)
        for column in ('recipient_id', 'created_at', 'read_at', 'entity_id'):
            self._conn.execute(f"CREATE INDEX IF NOT EXISTS idx_{NOTIFICATION_TABLE}_{column} ON {NOTIFICATION_TABLE}({column})")
        self._conn.execute(GROWTH_DDL)
        self._conn.execute(GROWTH_SYNC_DDL)
        self._conn.execute(f"CREATE INDEX IF NOT EXISTS idx_{GROWTH_TABLE}_user_id ON {GROWTH_TABLE}(user_id)")
        self._conn.execute(f"CREATE INDEX IF NOT EXISTS idx_{GROWTH_TABLE}_created_at ON {GROWTH_TABLE}(created_at)")
        self._conn.execute(f"CREATE INDEX IF NOT EXISTS idx_{GROWTH_SYNC_TABLE}_status ON {GROWTH_SYNC_TABLE}(status,created_at)")
        self._conn.execute(ACTIVITY_DDL)
        for column in ACTIVITY_INDEX_COLUMNS:
            self._conn.execute(f"CREATE INDEX IF NOT EXISTS idx_{ACTIVITY_TABLE}_{column} ON {ACTIVITY_TABLE}({column})")
        self._conn.execute(QUOTE_USAGE_DDL)
        for column in QUOTE_USAGE_INDEX_COLUMNS:
            self._conn.execute(f"CREATE INDEX IF NOT EXISTS idx_{QUOTE_USAGE_TABLE}_{column} ON {QUOTE_USAGE_TABLE}({column})")
        self._conn.execute(API_EVENT_DDL)
        for column in API_EVENT_INDEX_COLUMNS:
            self._conn.execute(f"CREATE INDEX IF NOT EXISTS idx_{API_EVENT_TABLE}_{column} ON {API_EVENT_TABLE}({column})")
        from core.contracts import SOURCE_MESSAGE_ID_PREFIX
        message_id_text = "TRIM(CAST((payload::jsonb)->>'消息ID' AS TEXT))"
        prefix_length = len(SOURCE_MESSAGE_ID_PREFIX)
        source_key_sql = (
            f"COALESCE(NULLIF(RTRIM(CASE WHEN SUBSTR({message_id_text},1,{prefix_length})='{SOURCE_MESSAGE_ID_PREFIX}' "
            f"THEN SUBSTR({message_id_text},{prefix_length + 1}) ELSE {message_id_text} END,'='),''),'row-'||id::text)"
        )
        self._conn.execute(
            f"CREATE INDEX IF NOT EXISTS idx_pending_records_bulk_source ON pending_records (({source_key_sql})) "
            "WHERE review_status='awaiting'")
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_pending_records_bulk_gate "
            "ON pending_records(review_status,kind,check_status,id) WHERE review_status='awaiting'")
        self._conn.execute(MESSAGE_BOARD_DDL)
        for column in ('created_at', 'user_id'):
            self._conn.execute(f"CREATE INDEX IF NOT EXISTS idx_{MESSAGE_BOARD_TABLE}_{column} ON {MESSAGE_BOARD_TABLE}({column})")
        from manualdb import DDL
        self._conn.execute(DDL["sales_group_visits"])
        from ops.item_feedback import POSTGRES_DDL, ACTIVE_INDEX, ENTRY_INDEX
        self._conn.execute(POSTGRES_DDL)
        self._conn.execute(ACTIVE_INDEX)
        self._conn.execute(ENTRY_INDEX)
        statements = (
            "ALTER TABLE port_groups ADD COLUMN IF NOT EXISTS aliases TEXT",
            "ALTER TABLE carriers ADD COLUMN IF NOT EXISTS website_url TEXT",
            "ALTER TABLE freight_items ADD COLUMN IF NOT EXISTS service_name TEXT",
            "ALTER TABLE freight_items ADD COLUMN IF NOT EXISTS destination_terminal TEXT",
            "ALTER TABLE freight_items ADD COLUMN IF NOT EXISTS currency TEXT NOT NULL DEFAULT 'USD'",
            "UPDATE freight_items SET currency='USD' WHERE currency IS NULL OR BTRIM(currency)=''",
            "ALTER TABLE freight_items ADD COLUMN IF NOT EXISTS entry_id TEXT",
            "UPDATE freight_items SET entry_id='frt_' || substr(md5('freight:' || id::text),1,32) WHERE entry_id IS NULL OR entry_id=''",
            "ALTER TABLE freight_items ALTER COLUMN entry_id SET NOT NULL",
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_freight_items_entry_id ON freight_items(entry_id)",
            "ALTER TABLE pending_records ADD COLUMN IF NOT EXISTS entry_id TEXT",
            "UPDATE pending_records SET entry_id='frt_' || substr(md5('pending:' || id::text),1,32) WHERE entry_id IS NULL OR entry_id=''",
            "ALTER TABLE pending_records ALTER COLUMN entry_id SET NOT NULL",
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_pending_records_entry_id ON pending_records(entry_id)",
        )
        for statement in statements:
            self._conn.execute(statement)
        self._conn.commit()

    def execute(self, statement: str, parameters: Iterable[Any] = ()) -> CompatCursor:
        query = statement.strip()
        if query.upper() == "BEGIN IMMEDIATE":
            return CompatCursor(self._conn.execute("SELECT 1 WHERE FALSE"))
        table_info = TABLE_INFO.fullmatch(query)
        if table_info:
            return CompatCursor(self._conn.execute(
                "SELECT ordinal_position-1 AS cid, column_name AS name "
                "FROM information_schema.columns WHERE table_schema=current_schema() "
                "AND table_name=%s ORDER BY ordinal_position", (table_info.group(1),)))
        if query.upper() == "PRAGMA DATA_VERSION":
            # Do not cache grouped results across independent PG writers.
            return CompatCursor(self._conn.execute("SELECT %s AS version", (time.time_ns(),)))
        if query.upper() == "PRAGMA DATABASE_LIST":
            # manualdb uses SQLite's database_list to scope its grouped-query cache.
            # PostgreSQL must answer this compatibility probe without aborting the transaction.
            database = self._conn.info.dbname or "postgres"
            return CompatCursor(self._conn.execute(
                "SELECT 0 AS seq,'main' AS name,%s AS file", (f"postgres:{database}",)))
        if " GLOB " in query:
            query = query.replace("name GLOB '*[0-9][0-9][0-9][0-9][0-9][0-9][0-9]*'",
                                  "name ~ '[0-9]{7}'")
        query = DATE_VALUE.sub(lambda m: f"CAST(NULLIF(valid_{m.group(1)}, '') AS date)", query)
        query = query.replace("date(?)", "CAST(? AS date)")
        query = MESSAGE_ID_JSON.sub(lambda m: f"(CAST({m.group(1)} AS jsonb)->>'{m.group(2)}')", query)
        query = _placeholders(query)
        inserted = INSERT_TABLE.match(query)
        return_id = bool(inserted and inserted.group(1).lower() in ID_TABLES and
                         "RETURNING" not in query.upper())
        if return_id:
            query += " RETURNING id"
        cursor = self._conn.execute(query, tuple(parameters))
        lastrowid = cursor.fetchone()[0] if return_id else None
        return CompatCursor(cursor, lastrowid)

    def commit(self) -> None:
        self._conn.commit()

    def rollback(self) -> None:
        self._conn.rollback()

    def close(self) -> None:
        self._conn.close()


POOL_MAX_CONNECTIONS = 32
POOL_WAIT_SECONDS = 30


class ConnectionPool:
    """Bounded IO adapter; each checkout owns one independent transaction."""
    def __init__(self, dsn: str):
        self._dsn = dsn
        self._idle = LifoQueue()
        self._lock = Lock()
        self._count = 0
        self._closed = False

    def acquire(self) -> Connection:
        while True:
            try:
                raw = self._idle.get_nowait()
            except Empty:
                with self._lock:
                    if self._closed:
                        raise RuntimeError("database pool is closed")
                    create = self._count < POOL_MAX_CONNECTIONS
                    if create:
                        self._count += 1
                if create:
                    try:
                        raw = psycopg.connect(self._dsn, row_factory=_row_factory)
                    except Exception:
                        with self._lock:
                            self._count -= 1
                        raise
                else:
                    try:
                        raw = self._idle.get(timeout=POOL_WAIT_SECONDS)
                    except Empty as exc:
                        raise TimeoutError("database connection wait exceeded") from exc
            if raw.closed or raw.broken:
                raw.close()
                with self._lock:
                    self._count -= 1
                continue
            return PooledConnection(raw, self)

    def release(self, raw) -> None:
        try:
            raw.rollback()  # Never return an open/failed transaction to another request.
        except psycopg.Error:
            logging.getLogger(__name__).warning("[postgres-pool] discarded unusable connection")
            raw.close()
        with self._lock:
            if self._closed or raw.closed or raw.broken:
                raw.close()
                self._count -= 1
            else:
                self._idle.put(raw)

    def close(self) -> None:
        with self._lock:
            self._closed = True
            while not self._idle.empty():
                self._idle.get_nowait().close()
                self._count -= 1


class PooledConnection(Connection):
    def __init__(self, raw, pool):
        self._conn = raw
        self._pool = pool
        self._released = False

    def close(self) -> None:
        if not self._released:
            self._released = True
            self._pool.release(self._conn)


_POOLS = {}
_POOL_LOCK = Lock()


def pooled_connect(dsn: str) -> Connection:
    with _POOL_LOCK:
        pool = _POOLS.get(dsn)
        if pool is None:
            pool = _POOLS[dsn] = ConnectionPool(dsn)
    return pool.acquire()


def close_pool(dsn: str) -> None:
    with _POOL_LOCK:
        pool = _POOLS.pop(dsn, None)
    if pool is not None:
        pool.close()


def connect(env_file: Path = DEFAULT_ENV_FILE) -> Connection:
    return pooled_connect(load_dsn(env_file))


def database_probe(env_file: Path = DEFAULT_ENV_FILE) -> dict[str, Any]:
    try:
        connection = connect(env_file)
        try:
            connection.execute("SELECT 1").fetchone()
            return {"connected": True, "engine": "postgresql"}
        finally:
            connection.close()
    except (OSError, RuntimeError, psycopg.Error) as exc:
        return {"connected": False, "engine": "postgresql", "error_type": type(exc).__name__}
