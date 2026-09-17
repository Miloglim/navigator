# -*- coding: utf-8 -*-
"""云端人工维护库 → 本地 SQLite 的一次性迁移/续灌。

    python pull_from_cloud.py            # 拉快照并灌进本地库 freight_manual.db
    python pull_from_cloud.py --pull-only  # 只拉成 cloud_snapshot.json，不写本地库

拉的是云端页面的公开读接口（GET /api/master?full=1、/api/rates、/api/space、
/api/operators、/api/chat-groups），不需要任何数据库凭据。
重复跑安全：全部按主键 merge，本地已有内容不会被搞乱。
"""
from __future__ import annotations

import argparse
import json
import sys
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.stdout.reconfigure(encoding="utf-8")

CLOUD_URL_FILE = HERE / "cloud_url.txt"      # 一次性迁移的入口地址，本目录自持
PAGE = 500


def get(url: str) -> dict:
    req = urllib.request.Request(url, method="GET")
    req.add_header("User-Agent", "manual-pull")
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read().decode("utf-8"))


def paged(url: str, key: str = "rows") -> list:
    """按 limit/offset 翻页拉全量，带派生字段（images）在这里剥掉。"""
    out, offset = [], 0
    while True:
        d = get(f"{url}?limit={PAGE}&offset={offset}")
        rows = d.get(key) or []
        out.extend({k: v for k, v in r.items() if k != "images"} for r in rows)
        if len(rows) < PAGE:
            return out
        offset += PAGE


def seed_sorts(snap: dict) -> None:
    """云端 carriers 表还没有 sort 列（迁移权限被挡），用本地 data/carriers.json 的序号补上。"""
    data = HERE.parent / "data"
    for table, fname, pk in (("carriers", "carriers.json", "code"), ("routes", "routes.json", "name")):
        path = data / fname
        if not path.exists():
            continue
        order = {r[pk]: r.get("sort") for r in json.loads(path.read_text(encoding="utf-8"))}
        for row in snap.get(table) or []:
            if row.get("sort") in (None, "") and row.get(pk) in order:
                row["sort"] = order[row[pk]]


def pull(url: str) -> dict:
    master = get(url + "/api/master?full=1").get("raw") or {}
    snap = {
        "source": url,
        "ports": master.get("ports") or [],
        "carriers": master.get("carriers") or [],
        "routes": master.get("routes") or [],
        "port_groups": master.get("port_groups") or [],
        "senders": master.get("senders") or [],
        "operators": get(url + "/api/operators").get("rows") or [],
        "chat_groups": get(url + "/api/chat-groups").get("rows") or [],
        "freight_rates": paged(url + "/api/rates"),
        "space_records": paged(url + "/api/space"),
    }
    seed_sorts(snap)
    return snap


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pull-only", action="store_true", help="只拉快照文件，不写本地库")
    args = ap.parse_args()
    if not CLOUD_URL_FILE.exists():
        raise SystemExit(f"缺少 {CLOUD_URL_FILE.name}（里面写云端看板地址）")
    url = CLOUD_URL_FILE.read_text(encoding="utf-8").strip().rstrip("/")
    print("从云端拉取:", url)
    snap = pull(url)
    out = HERE / "cloud_snapshot.json"
    out.write_text(json.dumps(snap, ensure_ascii=False), encoding="utf-8")
    print("  快照已存:", out.name, "|",
          " ".join(f"{k}={len(v)}" for k, v in snap.items() if isinstance(v, list)))
    if args.pull_only:
        return 0
    import manualdb
    c = manualdb.connect()          # 建库建表（业务表 + 主数据 + 港群）
    try:
        print("  导入本地库:", manualdb.load_snapshot(c, snap))
    finally:
        c.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
