# -*- coding: utf-8 -*-
"""ops/backup.py — 运价台账自动备份（计划任务 / 手动）。

用法：
    python ops/backup.py             # 在线热备份到 Database/backup/，保留最近 7 份
    python ops/backup.py --keep 30   # 保留份数自定义
    python ops/backup.py --check     # 只验证最近一份备份可打开、行数与主库一致（不写）

备份用 sqlite3 backup API：不锁库、WAL 内容一并写入，全程不影响正在跑的看板。
"""
import argparse
import hashlib
import json
import os
import shutil
import sqlite3
import sys
import tempfile
from datetime import datetime
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
HERE = Path(__file__).resolve().parent
DB = HERE.parent / "freight_manual.db"
BACKUP_DIR = HERE.parent / "backup"
STATUS_FILE = HERE / "backup-status.json"
BACKUP_TARGET_ENV = "FREIGHT_BACKUP_TARGET"
CHECK_TABLES = ("freight_items", "pending_records", "ports", "carriers", "routes",
                "senders", "port_groups", "alias_scope", "chat_groups", "operators")


def _table_counts(path: Path) -> dict:
    c = sqlite3.connect("file:%s?mode=ro" % path.as_posix(), uri=True)
    try:
        return {table: c.execute("SELECT COUNT(*) FROM " + table).fetchone()[0]
                for table in CHECK_TABLES}
    finally:
        c.close()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def verify_backup(db_path: Path, backup_path: Path) -> dict:
    """Verify integrity and exercise a full restore into a disposable database."""
    source_counts = _table_counts(db_path)
    backup_counts = _table_counts(backup_path)
    c = sqlite3.connect("file:%s?mode=ro" % backup_path.as_posix(), uri=True)
    try:
        integrity = c.execute("PRAGMA integrity_check").fetchone()[0]
        with tempfile.TemporaryDirectory(prefix="freight-restore-") as temp_dir:
            restored = Path(temp_dir) / "restored.db"
            dst = sqlite3.connect(restored)
            try:
                c.backup(dst)
            finally:
                dst.close()
            restored_counts = _table_counts(restored)
    finally:
        c.close()
    ok = integrity == "ok" and source_counts == backup_counts == restored_counts
    return {"ok": ok, "integrity": integrity, "source_counts": source_counts,
            "backup_counts": backup_counts, "restored_counts": restored_counts}


def copy_offhost(backup_path: Path, target_dir: Path) -> dict:
    """Copy only after local restore validation; never creates an unverified target path."""
    if not target_dir.is_dir():
        raise RuntimeError("异机备份目录不存在或不可访问：%s" % target_dir)
    target = target_dir / backup_path.name
    temp = target_dir / ("." + backup_path.name + ".tmp")
    shutil.copy2(backup_path, temp)
    if _sha256(backup_path) != _sha256(temp):
        temp.unlink(missing_ok=True)
        raise RuntimeError("异机备份副本摘要不一致")
    os.replace(temp, target)
    return {"path": str(target), "sha256": _sha256(target)}


def _write_status(status: dict, status_path: Path = STATUS_FILE) -> None:
    status_path.write_text(json.dumps(status, ensure_ascii=False, indent=2), encoding="utf-8")


def backup(db_path: Path, keep: int, backup_dir: Path = BACKUP_DIR,
           target_dir: Path | None = None, status_path: Path = STATUS_FILE) -> dict:
    backup_dir.mkdir(parents=True, exist_ok=True)
    dst = backup_dir / ("freight_manual-%s.db" % datetime.now().strftime("%Y%m%d-%H%M%S"))
    src = sqlite3.connect(db_path)
    out = sqlite3.connect(dst)
    try:
        src.backup(out)
        out.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        out.close()
        src.close()
    olds = sorted(backup_dir.glob("freight_manual-*.db"))
    for f in olds[:-keep]:
        try:
            f.unlink()
            print("  prune", f.name)
        except OSError:
            pass
    verification = verify_backup(db_path, dst)
    if not verification["ok"]:
        raise RuntimeError("本地备份恢复校验失败：%s" % verification)
    status = {"ok": True, "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
              "local_path": str(dst), "local_sha256": _sha256(dst), "verification": verification,
              "offhost": None}
    if target_dir:
        status["offhost"] = copy_offhost(dst, target_dir)
    _write_status(status, status_path)
    print("backup ->", dst)
    return status


def check(db_path: Path, backup_dir: Path = BACKUP_DIR) -> dict:
    backs = sorted(backup_dir.glob("freight_manual-*.db"))
    if not backs:
        raise RuntimeError("无备份文件可验证")
    latest = backs[-1]
    result = verify_backup(db_path, latest)
    result["backup_path"] = str(latest)
    return result


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--keep", type=int, default=7)
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--db", type=Path, default=DB)
    ap.add_argument("--target", type=Path, help="现有的异机/网络备份目录；缺省读取 FREIGHT_BACKUP_TARGET")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()
    try:
        target = args.target or (Path(os.environ[BACKUP_TARGET_ENV]) if os.environ.get(BACKUP_TARGET_ENV) else None)
        result = check(args.db) if args.check else backup(args.db, args.keep, target_dir=target)
        if args.json:
            print(json.dumps(result, ensure_ascii=False, indent=2))
        elif args.check:
            print("最新备份:", result["backup_path"], "->", "一致 OK" if result["ok"] else "不一致")
        sys.exit(0 if result["ok"] else 1)
    except Exception as exc:
        _write_status({"ok": False, "checked_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                       "error": str(exc)[:500]})
        print("backup failed:", exc, file=sys.stderr)
        sys.exit(1)
