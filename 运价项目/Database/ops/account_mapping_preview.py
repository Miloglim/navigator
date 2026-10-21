# -*- coding: utf-8 -*-
"""Read-only Navigator account mapping preview for central identity migration.

This tool never writes either database and never returns password hashes,
sessions, tokens, or private connection details.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

from ops.postgres_sync import load_dsn  # noqa: E402


EMAIL_ALIAS_PATTERN = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
def _is_email_alias(alias: str) -> bool:
    return bool(EMAIL_ALIAS_PATTERN.fullmatch(alias))


def _status(row: dict[str, Any], duplicate_names: set[str], is_email_alias: bool) -> str:
    alias = str(row.get("name") or "").strip().lower()
    if not is_email_alias:
        return "out_of_scope"
    if not alias or alias in duplicate_names or not row.get("has_password"):
        return "needs_review"
    return "pending"


def preview(dsn: str) -> dict[str, Any]:
    try:
        import psycopg
    except ImportError as exc:
        raise RuntimeError("缺少 PostgreSQL 驱动 psycopg") from exc
    with psycopg.connect(dsn, options="-c default_transaction_read_only=on") as connection:
        with connection.cursor() as cursor:
            cursor.execute("""
                SELECT user_id::text AS legacy_user_id, name, display_name, role,
                       (active=1) AS active,
                       (password_hash IS NOT NULL AND btrim(password_hash)<>'') AS has_password
                FROM operators ORDER BY user_id
            """)
            rows = [dict(zip([column.name for column in cursor.description], row)) for row in cursor.fetchall()]
            cursor.execute("""
                SELECT lower(btrim(name)) FROM operators
                GROUP BY lower(btrim(name)) HAVING count(*) > 1
            """)
            duplicate_rows = [row[0] for row in cursor.fetchall()]
    duplicate_names = set(duplicate_rows)
    accounts = []
    for row in rows:
        alias = str(row.get("name") or "").strip().lower()
        is_email_alias = _is_email_alias(alias)
        legacy_display_name = str(row.get("display_name") or "").strip()
        target_display_name = legacy_display_name or alias
        accounts.append({
            "source_product": "navigator",
            "legacy_user_id": row["legacy_user_id"],
            "legacy_login_alias": str(row.get("name") or "").strip().lower(),
            "display_name": legacy_display_name,
            "target_display_name": target_display_name,
            "display_name_defaulted": not bool(legacy_display_name),
            "role": row.get("role"),
            "active": bool(row.get("active")),
            "has_password": bool(row.get("has_password")),
            "is_email_alias": is_email_alias,
            "migration_scope": "email_alias" if is_email_alias else "out_of_scope",
            "status": _status(row, duplicate_names, is_email_alias),
            "requires_password_reset": not bool(row.get("has_password")),
        })
    return {
        "source_product": "navigator",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "read_only": True,
        "accounts": accounts,
        "summary": {
            "total": len(accounts),
            "needs_review": sum(item["status"] == "needs_review" for item in accounts),
            "pending": sum(item["status"] == "pending" for item in accounts),
            "out_of_scope": sum(item["status"] == "out_of_scope" for item in accounts),
            "email_aliases": sum(item["is_email_alias"] for item in accounts),
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="生成 Navigator 到中心账号的只读映射预览")
    parser.add_argument("--env-file", type=Path, default=Path(r"C:\ProgramData\FreightRate\freight-postgres.env"))
    parser.add_argument("--output", type=Path, help="可选：将不含密码、会话或 Token 的预览清单写入 JSON 文件")
    args = parser.parse_args()
    payload = json.dumps(preview(load_dsn(args.env_file)), ensure_ascii=False, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(payload + "\n", encoding="utf-8")
    print(payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
