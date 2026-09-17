# -*- coding: utf-8 -*-
"""前端 JS 语法体检（局域网版）：把 <script> 里的脚本交给 esprima 解析，报第一个错误及上下文。

    python js_check.py

为什么要有这个：本项目前端是单文件页面，一处注释/引号写坏，整段脚本就不执行，
表现是"页面能打开但所有页签点不动"，只看接口和数据根本发现不了。
改完 web/index.html 必须跑这个（依赖：python -m pip install --user esprima）。
"""
import sys
from pathlib import Path

try:
    import esprima
except ImportError:
    raise SystemExit("缺 esprima：先跑 python -m pip install --user esprima")

F = Path(__file__).resolve().parent / "web" / "index.html"
sys.stdout.reconfigure(encoding="utf-8")

src = F.read_text(encoding="utf-8")
start = src.index("<script>") + len("<script>")
end = src.rindex("</script>")
js = src[start:end]
file_offset = src[:start].count("\n")
# esprima 只到 ES2017，可选链 ?. 与空值合并 ?? 会误报：解析前做等价替换（不动磁盘文件）
js = js.replace("?.", ".").replace("??", "||")

try:
    esprima.parseScript(js, tolerant=False)
    print("JS 语法：通过（脚本 %d 行）" % js.count("\n"))
    sys.exit(0)
except Exception as e:
    print("JS 语法错误：", e)
    ln = getattr(e, "lineNumber", None)
    if ln:
        lines = js.split("\n")
        lo, hi = max(0, ln - 4), min(len(lines), ln + 3)
        for n in range(lo, hi):
            mark = ">>" if n == ln - 1 else "  "
            print("%s 文件第 %4d 行 | %s" % (mark, n + 1 + file_offset, lines[n][:160]))
    sys.exit(1)
