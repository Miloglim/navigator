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
import argparse, json, os, sys
from datetime import datetime, timedelta
sys.stdout.reconfigure(encoding='utf-8')
import paths as P

FMT = "%Y-%m-%d %H:%M:%S"


def load_groups():
    return {"groups": P.chat_groups()}


def load_wm():
    if os.path.exists(P.WATERMARK_F):
        return json.load(open(P.WATERMARK_F, encoding="utf-8"))
    return {}


def cmd_get(back):
    groups, wm = load_groups(), load_wm()
    floor = (datetime.now() - back) if back else datetime.now().replace(
        hour=0, minute=0, second=0, microsecond=0)
    out = {}
    for g in groups["groups"]:
        ts = wm.get(g["slug"])
        if not ts or datetime.strptime(ts, FMT) < floor:
            ts = floor.strftime(FMT)
        out[g["slug"]] = {"since": ts, "cid": g["cid"], "name": g["name"],
                          "default_pol": g["default_pol"]}
    print(json.dumps(out, ensure_ascii=False, indent=1))
    return out


def cmd_set(mapping):
    wm = load_wm()
    bad = []
    for slug, ts in mapping.items():
        try:
            datetime.strptime(ts, FMT)
        except ValueError:
            bad.append((slug, ts))
            continue
        if slug not in wm or datetime.strptime(ts, FMT) > datetime.strptime(wm[slug], FMT):
            wm[slug] = ts
    P.ensure(os.path.dirname(P.WATERMARK_F))
    json.dump(wm, open(P.WATERMARK_F, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print("watermark updated:", len(mapping) - len(bad), "skipped(bad ts):", bad)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("action", choices=["get", "set"])
    ap.add_argument("--back", help="回退窗口，如 3d / 12h")
    ap.add_argument("--map", help='set 时用：{"slug":"YYYY-MM-DD HH:MM:SS"}')
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
        cmd_set(json.loads(a.map))
