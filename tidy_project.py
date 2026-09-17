# -*- coding: utf-8 -*-
"""精简 正式版运价库：快照/备份/一次性脚本搬进 归档/，重复与 .trash 进回收站。

    python tidy_project.py             预演：只打印将要搬/丢的清单与引用自查
    python tidy_project.py --apply     真搬（写 归档/MANIFEST.md，逐条可回退）
    python tidy_project.py --recycle   真丢回收站（仅限清单里 .trash 与 0 字节垃圾）

安全边界：
  · 活文件（服务/校验/迁移/心跳脚本正在用）在 PROTECT 里，出现即中止
  · 搬前对每个候选做引用自查：被任何「留下的代码」按文件名提到，就不搬、只登记为待确认
  · 删除一律走回收站，绝不永久删
"""
import hashlib
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

ROOT = Path(r"C:\Users\YQN\Documents\正式版运价库")
DB = ROOT / "运价项目" / "Database"
ARC = ROOT / "归档"

# 绝不能动的活文件（名字级，含今天另一个 agent 在改的那批）
PROTECT = {
    "manualdb.py", "manual_server.py", "manual_check.py", "web_check.py", "js_check.py",
    "index.html", "freight_manual.db", "manual.log", "manual.pid", "token_manual.txt",
    "start-manual.bat", "stop-manual.bat", "运价看板-启动并打开.bat", "运价看板-只开网页.url",
    "DB变更汇总.md", "ID key changes.md", "README.md", "SKILL.md", "spec.json",
    "run.py", "pipeline.py", "learnings.md", "watermark.json",
}

# 归档分桶：bucket -> (目录, [glob])
PLAN = [
    ("库快照", ROOT, ["freight_manual.db.pre-*", "freight_manual.db.bak-*", "pre-*-export-*.json",
                      "pre-p*-export-*.json", "post-seed-master.json", ".last_baseline_ts"]),
    ("前端快照", ROOT, ["index.html.bak-*", "manual_server.py.bak-*", "DB变更汇总.md.bak-*",
                        "运价看板-启动并打开.bat.bak-*"]),
    ("一次性脚本", ROOT, ["_seed.py", "_seed.txt", "_seedrun.py", "_seedrun.txt",
                          "_extract_portable.py", "fix_launcher.py", "split_ops_view.py",
                          "verify_launcher.py", "inventory.py", "inventory2.py"]),
    ("报告", ROOT, ["portable-vs-current-comparison.md"]),
    ("库快照", DB, ["freight_manual.backup-*.db", "freight_manual.db.bak-*", "export-pre-*.json"]),
    ("前端快照", DB, ["served.html", "web/index.html.bak-*"]),
]

# 进回收站的固定项（.trash 整目录 + 0 字节误生成文件）+ 动态项（归档桶里 md5 完全相同的重复份）
RECYCLE_DIRS = [DB / ".trash"]
RECYCLE_FILES = [DB / "powershell"]


def md5(p):
    h = hashlib.md5()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def collect():
    out = []                      # (bucket, src)
    seen = set()
    for bucket, base, pats in PLAN:
        for pat in pats:
            for p in sorted(base.glob(pat)):
                if not p.is_file():
                    continue
                if p.name in PROTECT:
                    raise SystemExit("清单里出现了活文件 %s，中止" % p)
                if p in seen:
                    continue
                seen.add(p)
                out.append((bucket, p))
    return out


moves = collect()

# 引用自查：只有「留下的代码」按名字提到才算依赖；.md 里提到 .bak 是变更日志叙述不是依赖，
# 本脚本自己的 PLAN 里写满了候选名，必须排除自己
SELF = Path(__file__).resolve()
CODE_EXT = {".py", ".html", ".bat", ".ps1", ".js", ".json"}
keep_text = []
for dirpath, dirnames, filenames in os.walk(ROOT):
    d = Path(dirpath)
    dirnames[:] = [x for x in dirnames if x not in {".git", "__pycache__", ".codegraph", ".trash", "归档", "pack"}]
    for fn in filenames:
        p = d / fn
        if p in {m[1] for m in moves} or p.resolve() == SELF:
            continue
        if p.suffix.lower() in CODE_EXT and p.stat().st_size < 4_000_000:
            try:
                keep_text.append((p, p.read_text(encoding="utf-8", errors="ignore")))
            except Exception:
                pass

ref_hits = {}
for bucket, p in moves:
    who = [str(k.relative_to(ROOT)) for k, body in keep_text if p.name in body]
    if who:
        ref_hits[p.name] = who[:4]

# md5 完全相同的：归档时只留一份，多余份进回收站
by_hash = {}
for bucket, p in moves:
    try:
        by_hash.setdefault(md5(p), []).append((bucket, p))
    except Exception:
        pass
dup_recycle = [(b, p) for h, grp in by_hash.items() if len(grp) > 1 for b, p in grp[1:]]

tot = sum(p.stat().st_size for _, p in moves)
print("=== 预演 ===")
print("拟归档 %d 个文件，共 %.1f MB；其中 md5 重复只留一份，多余 %d 个进回收站"
      % (len(moves), tot / 1e6, len(dup_recycle)))
cur = None
for bucket, p in sorted(moves, key=lambda x: (x[0], -x[1].stat().st_size)):
    if bucket != cur:
        cur = bucket
        print("\n[%s]" % bucket)
    mark = "  ≡重复→回收站" if (bucket, p) in dup_recycle else ""
    print("  %8.1fK  %-56s%s" % (p.stat().st_size / 1024, str(p.relative_to(ROOT))[:56], mark))

print("\n拟进回收站：")
for d in RECYCLE_DIRS:
    n = sum(1 for f in d.rglob("*") if f.is_file()) if d.exists() else 0
    sz = sum(f.stat().st_size for f in d.rglob("*") if f.is_file()) if d.exists() else 0
    print("  目录 %s  (%d 个文件, %.1f MB)" % (d.relative_to(ROOT), n, sz / 1e6))
for f in RECYCLE_FILES:
    print("  文件 %s  (%d 字节)" % (f.relative_to(ROOT), f.stat().st_size if f.exists() else -1))
for b, p in dup_recycle:
    print("  重复 %s" % p.relative_to(ROOT))

if ref_hits:
    print("\n[!] 引用自查：这些候选仍被留下的代码按名字提到 → 本次不搬，只登记待确认")
    for name, who in ref_hits.items():
        print("  %-40s 被 %s 提到" % (name, ", ".join(x[:40] for x in who)))
else:
    print("\n引用自查：干净，没有候选被留下的代码提到")

if "--apply" in sys.argv:
    skip = set(ref_hits)
    ARC.mkdir(exist_ok=True)
    log = []
    for bucket, p in moves:
        if p.name in skip:
            continue
        if (bucket, p) in dup_recycle:          # 重复份先集中待回收，不留在根上碍眼
            dst = ARC / "_待回收" / p.name
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(p), str(dst))
            log.append(("_待回收", str(p.relative_to(ROOT)), str(dst.relative_to(ROOT))))
            continue
        dst = ARC / bucket / p.name if p.parent == ROOT else ARC / bucket / p.relative_to(ROOT)
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(p), str(dst))
        log.append((bucket, str(p.relative_to(ROOT)), str(dst.relative_to(ROOT))))
    hdr = ("# 归档清单\n\n生成 %s。每一项都是「从哪搬到哪」，原路径已不存在；"
           "回退按目标路径搬回即可。`_待回收` 是 md5 与另一份完全相同的重复件，--recycle 会送进回收站。\n\n"
           ) % time.strftime("%Y-%m-%d %H:%M:%S")
    ARC.joinpath("MANIFEST.md").write_text(
        hdr + "\n".join("| %s | `%s` → `%s` |" % t for t in log) + "\n", encoding="utf-8")
    print("\n已归档 %d 个，清单见 归档/MANIFEST.md" % len(log))

if "--recycle" in sys.argv:
    targets = []
    for d in RECYCLE_DIRS:
        if d.exists():
            targets.append(("dir", d))
    for f in RECYCLE_FILES:
        if f.exists():
            targets.append(("file", f))
    pend = ARC / "_待回收"
    if pend.exists():
        for f in sorted(pend.iterdir()):
            if f.is_file():
                targets.append(("file", f))
    if not targets:
        print("回收站步骤：没有待删项")
        sys.exit(0)
    ps = ROOT / "_recycle.ps1"
    lines = ["Add-Type -AssemblyName Microsoft.VisualBasic"]
    for kind, t in targets:
        fn = "DeleteDirectory" if kind == "dir" else "DeleteFile"
        lines.append("[Microsoft.VisualBasic.FileIO.FileSystem]::%s('%s', 'OnlyErrorDialogs', 'SendToRecycleBin')"
                     % (fn, str(t).replace("'", "''")))
    # PS 5.1 读无 BOM 的 .ps1 会按 GBK 解码，中文路径必坏 → 必须带 BOM
    ps.write_bytes(("\n".join(lines) + "\n").encode("utf-8-sig"))
    r = subprocess.run(["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(ps)],
                       capture_output=True)
    print(r.stdout.decode("gbk", "replace").strip(), r.stderr.decode("gbk", "replace").strip())
    gone, left = 0, []
    for kind, t in targets:
        if not t.exists():
            gone += 1
        else:
            left.append(str(t.relative_to(ROOT)))
    ps.unlink(missing_ok=True)
    if left:
        print("回收站未收纳 %d 项: %s" % (len(left), ", ".join(left)))
    print("已送回收站 %d/%d 项" % (gone, len(targets)))
