# -*- coding: utf-8 -*-
"""Create a verified PostgreSQL custom-format backup without exposing credentials."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path

from psycopg.conninfo import conninfo_to_dict

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from ops.postgres_sync import DEFAULT_ENV_FILE, load_dsn  # noqa: E402

BACKUP_DIR = ROOT / "backup"
STATUS_FILE = Path(__file__).resolve().parent / "postgres-backup-status.json"
PG_BIN = Path(r"C:\Program Files\PostgreSQL\18\bin")
KEEP = 7


def backup(env_file: Path = DEFAULT_ENV_FILE, backup_dir: Path = BACKUP_DIR,
           pg_bin: Path = PG_BIN, keep: int = KEEP) -> dict:
    if keep < 1:
        raise ValueError("keep must be positive")
    dsn = load_dsn(env_file)
    params = conninfo_to_dict(dsn)
    environment = os.environ.copy()
    for key in ("PGDATABASE", "PGHOST", "PGPORT", "PGUSER", "PGPASSWORD"):
        environment.pop(key, None)
    for key, variable in (("dbname", "PGDATABASE"), ("host", "PGHOST"),
                          ("port", "PGPORT"), ("user", "PGUSER"),
                          ("password", "PGPASSWORD")):
        if key in params:
            environment[variable] = str(params[key])
    environment["PGCONNECT_TIMEOUT"] = "5"
    backup_dir.mkdir(parents=True, exist_ok=True)
    target = backup_dir / ("freight-postgres-%s.dump" % datetime.now().strftime("%Y%m%d-%H%M%S"))
    temporary = target.with_suffix(".dump.tmp")
    try:
        with temporary.open("wb") as output:
            dump = subprocess.run([str(pg_bin / "pg_dump.exe"), "--format=custom", "--no-owner",
                                   "--no-acl"], env=environment, stdout=output,
                                  stderr=subprocess.PIPE, timeout=300)
        if dump.returncode:
            detail = dump.stderr.decode("utf-8", errors="replace")
            for secret in (params.get("password"), dsn):
                if secret:
                    detail = detail.replace(secret, "[redacted]")
            raise RuntimeError("pg_dump failed: " + detail[:300])
        with temporary.open("rb") as archive:
            listing = subprocess.run([str(pg_bin / "pg_restore.exe"), "--list"], stdin=archive,
                                     capture_output=True, check=True, timeout=60)
        if not temporary.stat().st_size or b"TABLE DATA public" not in listing.stdout:
            raise RuntimeError("PostgreSQL backup archive is empty or missing table data")
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)
    with target.open("rb") as handle:
        digest = hashlib.file_digest(handle, "sha256").hexdigest()
    status = {"ok": True, "created_at": datetime.now().isoformat(timespec="seconds"),
              "path": str(target), "sha256": digest, "bytes": target.stat().st_size,
              "verification": "pg_restore --list; full restore drill pending"}
    STATUS_FILE.write_text(json.dumps(status, ensure_ascii=False, indent=2), encoding="utf-8")
    for old in sorted(backup_dir.glob("freight-postgres-*.dump"))[:-keep]:
        old.unlink()
    return status


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--env-file", type=Path, default=DEFAULT_ENV_FILE)
    parser.add_argument("--keep", type=int, default=KEEP)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    try:
        result = backup(args.env_file, keep=args.keep)
    except Exception as exc:
        STATUS_FILE.write_text(json.dumps({"ok": False, "error_type": type(exc).__name__}), encoding="utf-8")
        raise
    print(json.dumps(result, ensure_ascii=False) if args.json else result["path"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
