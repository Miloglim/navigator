"""Audit-only connections: detect the live backend, never migrate or fall back."""
import json
import os
import sqlite3
import sys
import urllib.request
from pathlib import Path

DATABASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(DATABASE_DIR))
BASE = os.environ.get("FRT_BOARD_URL", "http://127.0.0.1:8795")


def backend():
    with urllib.request.urlopen(BASE + "/api/health/live", timeout=10) as response:
        payload = json.load(response)
        name = (payload.get("data") or payload).get("backend")
    if name not in ("sqlite", "postgres"):
        raise RuntimeError("无法确认当前数据库后端，停止审查，不回退旧库")
    return name


def connect(name=None):
    name = name or backend()
    if name == "postgres":
        import postgresdb
        c = postgresdb.connect()
        c.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
        return c
    if name != "sqlite":
        raise ValueError("unknown audit backend")
    path = Path(os.environ.get("FRT_DB") or DATABASE_DIR / "freight_manual.db")
    c = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA query_only=ON")
    c.execute("BEGIN")
    return c


def duplicate_key(row, payload):
    # Only exact payload duplicates from an identified source are candidates.
    # Service, terminal, price, dates, terms and all future fields are included.
    if not payload.get("消息ID") or not payload.get("来源群"):
        return None
    return (row["kind"], json.dumps(payload, ensure_ascii=False, sort_keys=True))
