# -*- coding: utf-8 -*-
"""spec_index.py —— 「群聊规范」的机器入口（只读，不判定、不写库、不改文件）。

以前"每轮先读该群规范"只写在 references/群聊规范/README.md 里靠 Agent 自觉，脚本一行都不引用它。
本文件把三件事变成机器可用：
  1. 路由：slug → 规范文件路径（chat_sources.json 的 spec_dir 为准，缺失时按文件头 slug 反查）；
  2. 抽取：该群「缩写表」的机器可读行（写法/含义/归一目标/作用域）+ 标「待补」的条目；
  3. 锚点：可被 llm_out 的 rule_basis 引用的锚点集合（本群规范小节 + extraction-rules.md 条款号）。

    python spec_index.py            冒烟：打印 11 个群的解析结果
    python spec_index.py ningbo_space   打印单群明细
"""
import json
import os
import re
import sys

import paths

REFS = os.path.join(paths.PKG_ROOT, "references")
GROUP_SPECS = os.path.join(REFS, "群聊规范")
GLOBAL_RULES_F = os.path.join(REFS, "extraction-rules.md")
KNOWLEDGE_FILE = "业务知识沉淀.md"

TODO_MARK = ("待补", "待核", "待确认")


def _read(p):
    try:
        with open(p, encoding="utf-8") as f:
            return f.read()
    except OSError:
        return ""


def _cells(line):
    return [c.strip() for c in line.strip().strip("|").split("|")]


def parse_spec(text):
    """规范.md → {headers, abbrev[], todo[]}。缩写表按「## 缩写表」小节下的第一张表解析。"""
    headers = [m.group(2).strip() for m in re.finditer(r"^(#{2,3})\s+(.+?)\s*$", text, re.M)]
    abbrev, todo, in_tbl = [], [], False
    for ln in text.split("\n"):
        if ln.startswith("## "):
            in_tbl = "缩写表" in ln
            continue
        if not ln.strip().startswith("|"):
            continue
        if not in_tbl:
            continue
        c = _cells(ln)
        if len(c) < 3 or set("".join(c)) <= set("-: "):
            continue
        if c[0] in ("写法", "发送人"):
            continue
        writes = re.findall(r"`([^`]+)`", c[0]) or [c[0]]
        abbrev.append({"writes": [w.strip() for w in writes if w.strip()],
                       "meaning": c[1] if len(c) > 1 else "",
                       "target": c[2] if len(c) > 2 else "",
                       "scope": c[3] if len(c) > 3 else ""})
    for ln in text.split("\n"):
        if any(k in ln for k in TODO_MARK):
            todo.append(ln.strip()[:120])
    return {"headers": headers, "abbrev": abbrev, "todo": todo}


def global_anchors():
    """extraction-rules.md 的顶层条款号（`N. **标题**`）→ 'extraction-rules.md#N'。"""
    return sorted({"extraction-rules.md#%s" % m.group(1)
                   for m in re.finditer(r"^(?:#{1,6}\s+)?(\d+)\.\s+(?:\*\*|\S)",
                                        _read(GLOBAL_RULES_F), re.M)})


def _groups():
    try:
        return paths.chat_groups()
    except Exception:
        return []


_by_slug = {}


def index(refresh=False):
    """slug → 规范档案。整轮只算一次（缓存），改文件后重启进程即生效。"""
    global _by_slug
    if _by_slug and not refresh:
        return _by_slug
    # 先按文件头 slug 建反查表，兼容配置里没写 spec_dir 的情况
    by_header = {}
    if os.path.isdir(GROUP_SPECS):
        for d in sorted(os.listdir(GROUP_SPECS)):
            p = os.path.join(GROUP_SPECS, d, "规范.md")
            if not os.path.isfile(p):
                continue
            m = re.search(r"slug[：:]\s*`?([A-Za-z0-9_]+)`?", _read(p))
            if m:
                by_header.setdefault(m.group(1), []).append(d)
    out = {}
    for g in _groups():
        slug = g.get("slug") or ""
        rel = g.get("spec_dir") or ""
        cand = os.path.join(paths.PKG_ROOT, rel.replace("/", os.sep)) if rel else ""
        path = os.path.join(cand, "规范.md") if cand else ""
        src = "config"
        if not path or not os.path.isfile(path):
            dirs = by_header.get(slug) or []
            path = os.path.join(GROUP_SPECS, dirs[0], "规范.md") if len(dirs) == 1 else ""
            src = "header" if path else ("ambiguous" if dirs else "missing")
        rec = {"slug": slug, "group": g.get("name", ""), "path": path, "route": src,
               "config_dir": rel, "exists": bool(path) and os.path.isfile(path),
               "base_path": "", "headers": [], "abbrev": [], "todo": [], "anchors": []}
        # 共享基础：spec_base 指向的规范.md 与本群差异合并（基础在前、本群在后），
        # 这样 rule_basis 能引用共享小节、缩写表/待补也自动带上公共项。
        base_rel = g.get("spec_base") or ""
        base_path = os.path.join(paths.PKG_ROOT, base_rel.replace("/", os.sep), "规范.md") if base_rel else ""
        if base_path and os.path.isfile(base_path):
            rec["base_path"] = base_path
            bp = parse_spec(_read(base_path))
            rec["headers"] = bp["headers"] + rec["headers"]
            rec["abbrev"] = bp["abbrev"] + rec["abbrev"]
            rec["todo"] = bp["todo"] + rec["todo"]
        if rec["exists"]:
            own = parse_spec(_read(path))
            rec["headers"] = rec["headers"] + own["headers"]
            rec["abbrev"] = rec["abbrev"] + own["abbrev"]
            rec["todo"] = rec["todo"] + own["todo"]
            rec["anchors"] = ["规范.md#%s" % h for h in rec["headers"]]
        elif rec["base_path"]:
            # 本群差异文件缺失但共享基础在：仍用基础锚点，路由标 base_only 供冒烟发现
            rec["route"] = "base_only"
            rec["anchors"] = ["规范.md#%s" % h for h in rec["headers"]]
        knowledge_path = os.path.join(os.path.dirname(path), KNOWLEDGE_FILE) if path else ""
        rec["knowledge_path"] = knowledge_path if os.path.isfile(knowledge_path) else ""
        rec["knowledge_headers"] = []
        if rec["knowledge_path"]:
            knowledge = parse_spec(_read(rec["knowledge_path"]))
            rec["knowledge_headers"] = knowledge["headers"]
            rec["todo"].extend(knowledge["todo"])
            rec["anchors"].extend(KNOWLEDGE_FILE + "#" + h for h in knowledge["headers"])
        out[slug] = rec
    _by_slug = out
    return out


def spec_for(slug):
    return index().get(slug or "") or {"slug": slug, "exists": False, "path": "", "route": "unknown",
                                       "abbrev": [], "todo": [], "anchors": []}


def brief(slug):
    """塞进 llm_queue 队列项的最小信息：路径 + 共享基础路径 + 待补条数 + 缩写条数。"""
    s = spec_for(slug)
    return {"spec_path": s["path"] or "", "spec_route": s["route"],
            "spec_base_path": s.get("base_path") or "",
            "spec_knowledge_path": s.get("knowledge_path") or "",
            "spec_abbrev_n": len(s["abbrev"]), "spec_todo": s["todo"]}


def resolve_basis(slug, basis):
    """rule_basis 能否落到真实锚点。返回 (ok, 说明)。"""
    b = (basis or "").strip()
    if not b:
        return False, "缺 rule_basis"
    s = spec_for(slug)
    file_part, _, frag = b.partition("#")
    file_part = file_part.replace("\\", "/").split("/")[-1]
    if file_part in ("extraction-rules.md", "extraction-rules"):
        anchors = global_anchors()
        want = "extraction-rules.md#%s" % frag
        if want in anchors:
            return True, ""
        return False, "全局条款号不存在：%s（现有 1…%d）" % (b, len(anchors))
    if not s["exists"]:
        return False, "该群规范未定位（slug=%s），无法校验依据：%s" % (slug, b)
    if file_part == KNOWLEDGE_FILE:
        return (True, "") if frag in s.get("knowledge_headers", []) else (False, "本群业务知识无该小节：%s" % b)
    if not file_part or file_part in ("规范.md", "rules.md", "memory.md"):
        heads = {h for h in s["headers"]}
        return (True, "") if frag in heads else (False, "本群规范无该小节：%s（可用：%s）" % (b, "、".join(sorted(heads))[:70]))
    return False, "依据指向未知文件：%s" % b


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    idx = index(refresh=True)
    if len(sys.argv) > 1:
        print(json.dumps(idx.get(sys.argv[1], {}), ensure_ascii=False, indent=1)[:2200])
    else:
        print("全局条款锚点 %d 个；群规范 %d 份" % (len(global_anchors()), len(idx)))
        for slug, s in sorted(idx.items()):
            print("  %-22s 路由=%-7s 缩写行=%2d 待补=%d 小节=%d" %
                  (slug, s["route"], len(s["abbrev"]), len(s["todo"]), len(s["headers"])))
        bad = [k for k, v in idx.items() if not v["exists"] or v["route"] != "config"]
        print("路由异常（缺文件或非 config）：%s" % (", ".join(bad) or "无"))
