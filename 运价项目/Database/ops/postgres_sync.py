# -*- coding: utf-8 -*-
"""Synchronize SQLite master configuration into the local PostgreSQL staging database.

This is a pre-cutover safety mirror, not a runtime database adapter. It never
copies sessions or business rows, and refuses to overwrite PostgreSQL after
business data appears there.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
DATABASE_DIR = HERE.parent
sys.path.insert(0, str(DATABASE_DIR))

import manualdb  # noqa: E402
from ops.postgres_migrate import BUSINESS_TABLES, POSTGRES_DSN_ENV, _load_psycopg, migrate  # noqa: E402

DEFAULT_ENV_FILE = Path(os.environ.get("ProgramData", r"C:\ProgramData")) / "FreightRate" / "freight-postgres.env"


def load_dsn(env_file: Path) -> str:
    """Read only the expected DSN key from a local, access-controlled env file."""
    try:
        lines = env_file.read_text(encoding="ascii").splitlines()
    except OSError as exc:
        raise RuntimeError("PostgreSQL 本机连接配置不可读：%s" % env_file) from exc
    values = [line.partition("=")[2].strip() for line in lines
              if line.partition("=")[0].strip() == POSTGRES_DSN_ENV]
    if len(values) != 1 or not values[0]:
        raise RuntimeError("连接配置必须且只能包含一个 %s" % POSTGRES_DSN_ENV)
    return values[0]


def target_has_business_data(dsn: str) -> bool:
    """Do not turn a configuration mirror into destructive production sync."""
    psycopg, sql = _load_psycopg()
    with psycopg.connect(dsn) as target:
        with target.cursor() as cur:
            for table in BUSINESS_TABLES:
                cur.execute(sql.SQL("SELECT EXISTS (SELECT 1 FROM {} LIMIT 1)").format(sql.Identifier(table)))
                if cur.fetchone()[0]:
                    return True
    return False


def sync_configuration(sqlite_path: Path, dsn: str) -> dict[str, Any]:
    if target_has_business_data(dsn):
        raise RuntimeError("PostgreSQL 已有业务数据，拒绝配置镜像覆盖；请走正式适配器发布流程。")
    return migrate(sqlite_path, dsn, include_business_data=False, replace_target=True)


def main() -> int:
    parser = argparse.ArgumentParser(description="自动同步 SQLite 主数据到 PostgreSQL 预切换镜像")
    parser.add_argument("--sqlite", type=Path, default=manualdb.DEFAULT_DB)
    parser.add_argument("--env-file", type=Path, default=DEFAULT_ENV_FILE)
    args = parser.parse_args()
    if not args.sqlite.is_file():
        raise SystemExit("SQLite 文件不存在：%s" % args.sqlite)
    result = sync_configuration(args.sqlite, load_dsn(args.env_file))
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
