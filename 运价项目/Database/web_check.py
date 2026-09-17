# -*- coding: utf-8 -*-
"""局域网版前端结构体检：括号/引号平衡 + 关键功能在位断言。

bash 里内联 Python 会被转义搞坏（反斜杠、中文路径都是坑），所以固化成文件。
    python web_check.py
"""
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
F = HERE / "web" / "index.html"
BS = "\\"
NL = "\n"
FAILS = []


def strip_js(js):
    """去掉注释与字符串内容，只留结构字符，便于配对检查（含模板字面量与正则字面量）。"""
    out, i, n, errs = [], 0, len(js), []
    RQP = set("=(,:[!&|?{};")

    def looks_regex(buf):
        for ch in reversed(buf):
            if ch in " \t\r\n":
                continue
            return ch in RQP
        return True

    while i < n:
        c = js[i]
        if c == "/" and i + 1 < n and js[i + 1] == "/":
            j = js.find(NL, i)
            i = n if j < 0 else j
            continue
        if c == "/" and i + 1 < n and js[i + 1] == "*":
            j = js.find("*/", i + 2)
            i = n if j < 0 else j + 2
            continue
        if c == "/" and looks_regex(out):
            j, in_class = i + 1, False
            while j < n:
                if js[j] == BS:
                    j += 2
                    continue
                if js[j] == "[":
                    in_class = True
                elif js[j] == "]":
                    in_class = False
                elif js[j] == "/" and not in_class:
                    break
                elif js[j] == NL:
                    break
                j += 1
            j += 1
            while j < n and js[j].isalpha():
                j += 1
            i = j
            out.append(" ")
            continue
        if c in "\"'":
            j = i + 1
            while j < n:
                if js[j] == BS:
                    j += 2
                    continue
                if js[j] == c:
                    break
                if js[j] == NL:
                    errs.append("字符串未闭合 @ 第 %d 行" % js[:i].count(NL))
                    break
                j += 1
            i = j + 1
            continue
        if c == "`":
            j = i + 1
            while j < n:
                if js[j] == BS:
                    j += 2
                    continue
                if js[j] == "$" and j + 1 < n and js[j + 1] == "{":
                    d, j = 1, j + 2
                    while j < n and d:
                        if js[j] == "{":
                            d += 1
                        elif js[j] == "}":
                            d -= 1
                        j += 1
                    continue
                if js[j] == "`":
                    break
                j += 1
            i = j + 1
            continue
        out.append(c)
        i += 1
    return "".join(out), errs


def ck(name, ok, detail=""):
    print(("  OK   " if ok else "  FAIL ") + name + ("  | " + str(detail)[:110] if detail else ""))
    if not ok:
        FAILS.append(name)


src = F.read_text(encoding="utf-8")
js = src[src.index("<script"):]
code, errs = strip_js(js)
stack, pairs = [], {"(": ")", "[": "]", "{": "}"}
close = {v: k for k, v in pairs.items()}
for ch in code:
    if ch in pairs:
        stack.append(ch)
    elif ch in close:
        if not stack or stack[-1] != close[ch]:
            errs.append("括号不匹配 " + ch)
        else:
            stack.pop()
ck("括号/引号平衡", not errs and not stack, (errs[:3], stack[:3]))

for probe, desc in [
    ("function gpCell", "群名显示为可悬停查看成员的样式"),
    ("function acGroups", "录入端候选含港群"),
    ("function renderGroupHint", "筛选栏下方群快捷筛选"),
    ("function btParse", "批量导入解析器"),
    ("function openRangePicker", "有效期区间选择器"),
    ("function hardNormalizePort", "录入归一（港口硬匹配命中主数据）"),
    ("data-ac=", "自定义候选列表输入框"),
    ("port_groups", "后台港群维护"),
    ("/api/local-token", "本机免口令"),
]:
    ck(desc, probe in src)
# 原断言 gPod.concat(pod.sort()) 随旧筛选实现一起下线，换成现架构下等价的两条在位断言
ck("枚举下拉以库里真实值为准（fieldOptions 合并 rates+space）",
   "function fieldOptions" in src and "new Set([...((o.rates || {})[col] || []), ...((o.space || {})[col] || [])])" in src)
ck("销售端筛选框走统一联想组件且不列港群",
   'data-nogroup="1"' in src and "dataset.nogroup" in src)

# 括号平衡放过过一次"注释写坏"（整段脚本不执行、所有页签点不动），所以必须再做一次真语法解析
import subprocess
proc = subprocess.run([sys.executable, str(HERE / "js_check.py")], capture_output=True,
                      text=True, encoding="utf-8", errors="replace")
ck("JS 语法解析通过（js_check）", proc.returncode == 0,
   ((proc.stdout or "") + (proc.stderr or "")).strip().replace("\n", " ⏎ ")[:170])

print("\n结果:", "全部通过" if not FAILS else "失败 %d 项 -> %s" % (len(FAILS), FAILS))
sys.exit(1 if FAILS else 0)
