# -*- coding: utf-8 -*-
"""skill 源目录 ↔ 运行目录 字节比对（默认只报告；带 sync 才拷贝）。

为什么要有这个：台账批准候选会把「别名登记日志」写进项目内 Skill 源目录，副本会漂；
铁律是「改 skill 后必须源→运行全量字节比对同步」，靠记忆容易漏。

    python ops/skill_diff.py          # 只报差异
    python ops/skill_diff.py sync     # 把两边都有、内容不同的文件 源→运行 覆盖，然后复验
"""
import hashlib
import os
import sys

sys.stdout.reconfigure(encoding="utf-8")
SRC = r"C:\Users\YQN\Documents\正式版运价库\运价项目\Skill\freight-rate-heartbeat"
DST = r"C:\Users\YQN\.qwenworkcn\skills\freight-rate-heartbeat"
SKIP_DIRS = {"__pycache__", ".work", ".git", ".trash"}
SKIP_EXT = {".pyc", ".log"}


def walk(root):
    out = {}
    for d, ds, fs in os.walk(root):
        ds[:] = [x for x in ds if x not in SKIP_DIRS]
        for f in fs:
            if os.path.splitext(f)[1] in SKIP_EXT:
                continue
            p = os.path.join(d, f)
            out[os.path.relpath(p, root).replace("\\", "/")] = hashlib.sha256(open(p, "rb").read()).hexdigest()[:16]
    return out


def main():
    a, b = walk(SRC), walk(DST)
    diff = [k for k in sorted(set(a) | set(b)) if a.get(k) != b.get(k)]
    print(f"源 {len(a)} 文件 / 运行 {len(b)} 文件；差异 {len(diff)} 个")
    for k in diff:
        tag = "仅源有" if k not in b else ("仅运行有" if k not in a else "内容不同")
        print(f"  [{tag}] {k}")
    if "sync" not in sys.argv:
        return 0 if not diff else 1
    import shutil
    moved = 0
    for k in diff:
        if k in a and k in b:                 # 只同步两边都存在、内容不同的；只增不删，交人工判断
            dst = os.path.join(DST, k)
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            shutil.copyfile(os.path.join(SRC, k), dst)
            moved += 1
            print("  已同步:", k)
    a2, b2 = walk(SRC), walk(DST)
    left = [k for k in sorted(set(a2) | set(b2)) if a2.get(k) != b2.get(k)]
    print(f"同步 {moved} 个；复验剩余差异 {len(left)} 个", left)
    return 0 if not left else 1


if __name__ == "__main__":
    sys.exit(main())
