# -*- coding: utf-8 -*-
"""局域网人工链路定稿后的清场：云端旧链路、交付副本、心跳过程文件、归档里的过期快照全送回收站。

    python purge_legacy.py            预演
    python purge_legacy.py --apply    送回收站（不永久删，逐项验确）

唯一坚持保留的是 2026-09-17 10:33 清库前那一对副本（.db + .json）：
那是被清掉的旧数据现在唯一的去处，删了就真没了。归档里其余快照都是它的早期状态，已被覆盖。
"""
import os
import subprocess
import sys
import time
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

ROOT = Path(r"C:\Users\YQN\Documents\正式版运价库")
DB = ROOT / "运价项目" / "Database"
ARC = ROOT / "归档"
KEEP_DIR = ARC / "清库前快照-20260917"
KEEP_NAMES = ["pre-purge-export-20260917-103357.json", "freight_manual.db.pre-purge-20260917-103357"]

# 绝对不许碰的活物（防手滑）
FORBID = {"manual_server.py", "manualdb.py", "manual_check.py", "web_check.py", "js_check.py",
          "index.html", "freight_manual.db", "manual.log", "manual.pid", "token_manual.txt",
          "watermark.json", "SKILL.md", "pipeline.py", "record_builder.py", "validity_parser.py",
          "ledger_client.py", "paths.py", "watermark.py"}

CLOUD_FILES = [DB / "pull_from_cloud.py", DB / "sync_web.py",
               DB / "cloud_snapshot.json", DB / "cloud_url.txt"]
DUP_DIRS = [ROOT / "pack", ROOT / "运价项目" / "Skill" / "heartbeat-data" / ".work"]


def tree_files(top):
    return [p for p in top.rglob("*") if p.is_file()]


def plan():
    """返回 (要送回收站的路径列表, 归档保留列表)。"""
    targets, kept = [], []
    for p in CLOUD_FILES:
        if p.exists():
            targets.append(p)
    for d in DUP_DIRS:
        if d.exists():
            targets.append(d)
    if ARC.exists():
        for p in tree_files(ARC):
            if p.name == "MANIFEST.md":
                continue
            if p.name in KEEP_NAMES or KEEP_DIR in p.parents:
                kept.append(p)
                continue
            targets.append(p)
    for t in targets:
        if t.name in FORBID:
            raise SystemExit("清单里出现活文件 %s，中止" % t)
    return targets, kept


targets, kept = plan()
tot = 0
n = 0
print("=== 预演：拟送回收站 ===")
for t in sorted(targets, key=lambda x: str(x)):
    if t.is_dir():
        fs = tree_files(t)
        sz = sum(f.stat().st_size for f in fs)
        print("  目录 %-58s %4d 个 %7.1f MB" % (str(t.relative_to(ROOT))[:58], len(fs), sz / 1e6))
        tot += sz
        n += len(fs)
    else:
        print("  文件 %-58s       %7.1f K" % (str(t.relative_to(ROOT))[:58], t.stat().st_size / 1024))
        tot += t.stat().st_size
        n += 1
print("\n合计 %d 项 / %.1f MB" % (n, tot / 1e6))
print("保留：%s" % (", ".join(str(k.relative_to(ROOT)) for k in kept) or "（归档里暂无保留项）"))

if "--apply" not in sys.argv:
    print("\n预演完成，未删除。确认无误加 --apply")
    sys.exit(0)

# 1) 先把唯一副本挪进专项目录，避免下一步整目录清空时误伤
for name in KEEP_NAMES:
    for p in tree_files(ARC):
        if p.name == name and KEEP_DIR not in p.parents:
            KEEP_DIR.mkdir(parents=True, exist_ok=True)
            shutil_move = p.rename(KEEP_DIR / name)
            print("保留件挪入 →", str(shutil_move.relative_to(ROOT)))

# 2) 送回收站（PS 5.1 读无 BOM 的 .ps1 会按 GBK 解码，中文路径必坏 → 带 BOM 写）
lines = ["Add-Type -AssemblyName Microsoft.VisualBasic"]
for t in targets:
    if not t.exists():
        continue
    fn = "DeleteDirectory" if t.is_dir() else "DeleteFile"
    lines.append("[Microsoft.VisualBasic.FileIO.FileSystem]::%s('%s', 'OnlyErrorDialogs', 'SendToRecycleBin')"
                 % (fn, str(t).replace("'", "''")))
ps = ROOT / "_purge.ps1"
ps.write_bytes(("\n".join(lines) + "\n").encode("utf-8-sig"))
r = subprocess.run(["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(ps)],
                   capture_output=True)
out = (r.stdout + r.stderr).decode("gbk", "replace").strip()
if out:
    print("powershell:", out[:400])
ps.unlink(missing_ok=True)

gone = [t for t in targets if not t.exists()]
left = [t for t in targets if t.exists()]
print("已送回收站 %d/%d 项" % (len(gone), len(targets)))
if left:
    print("[!] 未收纳：" + ", ".join(str(x.relative_to(ROOT)) for x in left))

# 3) 归档里被清空的桶目录自底向上收掉，只留 清库前快照 专项目录
removed = []
for d in sorted([x for x in ARC.rglob("*") if x.is_dir()], key=lambda x: -len(x.parts)):
    if KEEP_DIR in d.parents or d == KEEP_DIR:
        continue
    if not any(d.iterdir()):
        d.rmdir()
        removed.append(d.name)
if removed:
    print("收掉空目录：%s" % ", ".join(removed))

# 4) 把本轮删除追加进 MANIFEST，说明留了什么、为什么
log = ARC / "MANIFEST.md"
body = ["\n## 2026-09-17 %s 清场（全部进回收站，未永久删除）\n" % time.strftime("%H:%M"),
        "- 云端旧链路：`pull_from_cloud.py`、`sync_web.py`、`cloud_snapshot.json`、`cloud_url.txt` —— "
        "程序已定为局域网 + 人工审核放行，公网链路整体退役（`sync_web.py` 本在新树就跑不起来，它读的源在旧树）。",
        "- `pack/`（对外交付副本，与本体逐字节重复）、`heartbeat-data/.work/`（心跳过程文件，可再生）。",
        "- 归档里除清库前快照之外的全部历史快照与一次性脚本 —— 回滚已改由 git 承担。",
        "- **保留** `%s/`：%s —— 今天 10:33 清库前的唯一副本，`.json` 可用 `manual_server.py --import` 回灌。"
        % (KEEP_DIR.name, " + ".join(KEEP_NAMES)),
        "- 已删除的 `manual_check.py` 提示语指向 `pull_from_cloud.py`，已改为本机 `--init-only` / `--import`。\n"]
log.write_text((log.read_text(encoding="utf-8") if log.exists() else "") + "\n".join(body), encoding="utf-8")
print("MANIFEST 已追加清场记录")
