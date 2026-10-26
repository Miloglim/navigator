"""Initialize a local-only Navigator administrator through the server console.

Center-managed deployments must create accounts in the center management app.
Passwords are read interactively and are never accepted as command arguments.
"""
from __future__ import annotations

import argparse
import getpass
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import manual_server  # noqa: E402 - project path must be available first


def load_runtime() -> None:
    path = PROJECT_ROOT / "ops" / "runtime.json"
    config = {}
    if path.exists():
        config = json.loads(path.read_text(encoding="utf-8"))
    manual_server.CFG.update({
        "backend": str(config.get("backend") or "sqlite"),
        "db": str(config.get("db") or manual_server.manualdb.DEFAULT_DB),
        "postgres_env_file": str(config.get("postgres_env_file") or manual_server.postgresdb.DEFAULT_ENV_FILE),
        "center_auth_url": str(config.get("center_auth_url") or "").rstrip("/"),
    })


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Initialize a local Navigator administrator")
    parser.add_argument("--name", help="Local administrator login name")
    args = parser.parse_args(argv)

    load_runtime()
    if manual_server.CFG.get("center_auth_url"):
        print("拒绝：当前是中心身份模式，请在中心管理库创建和管理账号。", file=sys.stderr)
        return 2

    name = (args.name or input("管理员账号: ")).strip()
    password = getpass.getpass("密码: ")
    confirmation = getpass.getpass("确认密码: ")
    if password != confirmation:
        print("两次输入的密码不一致。", file=sys.stderr)
        return 2

    try:
        manual_server.auth_setup({"name": name, "password": password})
    except (ValueError, PermissionError) as error:
        print(f"初始化失败：{error}", file=sys.stderr)
        return 1

    print(json.dumps({"ok": True, "name": name, "role": "admin"}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
