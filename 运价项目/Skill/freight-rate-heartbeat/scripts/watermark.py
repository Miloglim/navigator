# -*- coding: utf-8 -*-
"""
watermark.py —— 拉取式心跳的水位线管理（替代失效的本地事件队列）

为什么不用事件队列：`dws event consume` 要 fork 本地 bus 守护进程，Agent 宿主执行起不来，
只能靠用户在自己终端跑 start.ps1；一旦没人跑，events/inbox 永远是空的，
定时任务每次都报 rate_candidates=0，看起来"在运行"其实什么都没抓。
改成定时任务自己用 `dws chat message list --time <水位线> --direction newer` 拉，
全程无需人工，代价是轮询而非推送。

用法：
  python watermark.py get                 # 打印 {slug: 起始时间}，缺水位线的群回落到"今天 00:00:00"
  python watermark.py get --back 3d       # 全部回退 3 天（首次建线或补漏时用）
  python watermark.py set --map '{"qingdao":"2026-09-01 15:49:41", ...}'
"""
import argparse, json, os, sys, time
from datetime import datetime, timedelta
sys.stdout.reconfigure(encoding='utf-8')
import paths as P

FMT = "%Y-%m-%d %H:%M:%S"
LOCK_RETRY_SECONDS = 0.05
LOCK_TIMEOUT_SECONDS = 10


def load_groups():
    return {"groups": P.chat_groups()}


def load_wm():
    if os.path.exists(P.WATERMARK_F):
        return json.load(open(P.WATERMARK_F, encoding="utf-8"))
    return {}


def _lock_watermark():
    """Serialize read-modify-write updates from all heartbeat workers."""
    P.ensure(os.path.dirname(P.WATERMARK_F))
    lock_path = P.WATERMARK_F + ".lock"
    f = open(lock_path, "a+b")
    f.seek(0, os.SEEK_END)
    if f.tell() == 0:
        f.write(b"0")
        f.flush()
    deadline = time.monotonic() + LOCK_TIMEOUT_SECONDS
    while True:
        try:
            if os.name == "nt":
                import msvcrt
                f.seek(0)
                msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return f
        except OSError:
            if time.monotonic() >= deadline:
                f.close()
                raise TimeoutError("watermark lock timeout")
            time.sleep(LOCK_RETRY_SECONDS)


def _unlock_watermark(f):
    try:
        if os.name == "nt":
            import msvcrt
            f.seek(0)
            msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)
    finally:
        f.close()


def cmd_get(back):
    groups, wm = load_groups(), load_wm()
    floor = back or datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    out = {}
    for g in groups["groups"]:
        ts = wm.get(g["slug"])
        # Keep the last committed cursor across days. A review hold or downtime
        # must not silently discard the prior day's messages.
        if not ts or (back and datetime.strptime(ts, FMT) > floor):
            ts = floor.strftime(FMT)
        out[g["slug"]] = {"since": ts, "cid": g["cid"], "name": g["name"],
                          "default_pol": g["default_pol"]}
    print(json.dumps(out, ensure_ascii=False, indent=1))
    return out


def cmd_set(mapping, expected=None):
    bad = []
    valid = {}
    for slug, ts in mapping.items():
        try:
            datetime.strptime(ts, FMT)
        except ValueError:
            bad.append((slug, ts))
            continue
        valid[slug] = ts
    lock = _lock_watermark()
    try:
        wm = load_wm()
        for slug, before in (expected or {}).items():
            current = wm.get(slug)
            if current not in (before, mapping.get(slug)):
                raise ValueError('watermark changed concurrently: ' + slug)
        for slug, ts in valid.items():
            if slug not in wm or datetime.strptime(ts, FMT) > datetime.strptime(wm[slug], FMT):
                wm[slug] = ts
        tmp = P.WATERMARK_F + ".tmp.%s" % os.getpid()
        with open(tmp, "w", encoding="utf-8") as out:
            json.dump(wm, out, ensure_ascii=False, indent=1)
            out.flush()
            os.fsync(out.fileno())
        os.replace(tmp, P.WATERMARK_F)
    finally:
        _unlock_watermark(lock)
    print("watermark updated:", len(mapping) - len(bad), "skipped(bad ts):", bad)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("action", choices=["get", "set"])
    ap.add_argument("--back", help="回退窗口，如 3d / 12h")
    ap.add_argument("--map", help='set 时用：{"slug":"YYYY-MM-DD HH:MM:SS"}')
    ap.add_argument('--expect', help='可选：锁内核对提交前的群水位')
    a = ap.parse_args()
    if a.action == "get":
        back = None
        if a.back:
            n, u = int(a.back[:-1]), a.back[-1]
            back = datetime.now() - (timedelta(days=n) if u == "d" else timedelta(hours=n))
        cmd_get(back)
    else:
        if not a.map:
            sys.exit("set 需要 --map")
        cmd_set(json.loads(a.map), json.loads(a.expect) if a.expect else None)
