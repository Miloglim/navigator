# -*- coding: utf-8 -*-
"""spec_alias_diff.py —— 人读的「群聊规范」↔ 机器读的台账 alias_scope 一致性体检。

规范 README 里写着"两边冲突时以本目录为准并回写配置"，但没有任何工具能执行这句话——
现实是 README 还说 alias_scope「尚未建」，而库里已有 36 行。本脚本把这句话变成可跑的差集：

  · 覆盖性：规范缩写表里的每个「写法」在该群该作用域下有没有对应 alias_scope 行（只有两条可靠方向）；
  · 目标一致性：仅当规范行的「归一目标」是单一明确值时才比对，抽不出单值的记为「目标待人工核」，不瞎报；
  · 断链：chat_sources.spec_dir 是否指得到文件、规范头部的 slug/群名与配置是否一致、
          规范/全局口径里引用的 *.md 是否真的存在。

    python spec_alias_diff.py                    人读报告（读默认台账库，只读模式）
    python spec_alias_diff.py --json --strict     给回归脚本用：断链/路由不一致 → 退出码 1
本脚本不写库、不改文件；以 sqlite 只读 URI 打开，服务在跑也能读。
"""
import argparse
import json
import os
import re
import sqlite3
import sys

import paths as P
import spec_index

SCOPE_KIND = {"起运港": "pol", "目的港": "pod", "船司": "carrier", "航线": "route",
              "柜型": "ctype", "舱位类型": "space_type"}
MD_REF = re.compile(r"`?([A-Za-z0-9_\-\u4e00-\u9fff]+\.md)`?")


def default_db():
    return os.path.join(P.PROJECT_ROOT, "Database", "freight_manual.db")


def read_alias(db):
    """alias_scope → {(群名, kind, 别名小写): target}。只读打开，绝不建库。"""
    if not os.path.isfile(db):
        return None
    c = sqlite3.connect("file:%s?mode=ro" % db.replace("\\", "/"), uri=True)
    try:
        return {(r[0], r[1], r[2].strip().lower()): r[3] for r in
                c.execute("select scope_group, kind, alias, target from alias_scope")}
    finally:
        c.close()


NO_REG = ("留空", "不是港口", "进 `备注`", "进备注", "交人工", "不入库", "省略", "无绝对价", "待补")


def intent_of(alias, target):
    """这条规范行是要"登记别名"，还是本来就叫你别登记 / 写法即规范值本身。"""
    t = target or ""
    if any(k in t for k in NO_REG):
        return "不登记"
    if alias.strip().lstrip("+").upper() in t.upper():     # ECSA → 港群 `ECSA`/`WCSA`
        return "自映射"
    if "规范名" in t:                                   # Hamburg → "对应规范名"，写法本身就是规范值
        return "自映射"
    return "登记"


def spec_rows():
    """11 份规范 → [{group, kind, alias, target_raw, single, intent}]，作用域不承载别名的行跳过。"""
    rows = []
    idx = spec_index.index()
    for slug, s in idx.items():
        if not s.get("exists"):
            continue
        gname = s.get("group") or ""
        for a in s.get("abbrev", []):
            kind = SCOPE_KIND.get((a.get("scope") or "").strip())
            if not kind:
                continue
            for w in a["writes"]:
                cand = re.findall(r"`([^`]+)`", a.get("target") or "")
                single = cand[0] if len(cand) == 1 and len(a["writes"]) == 1 else ""
                rows.append({"group": gname, "slug": slug, "kind": kind, "alias": w,
                             "target_raw": (a.get("target") or "")[:60], "single": single,
                             "intent": intent_of(w, a.get("target") or "")})
    return rows


def broken_links():
    """配置路由、规范头部声明、被引用却不存在的 .md。"""
    problems = []
    idx = spec_index.index(refresh=True)
    for g in P.chat_groups():
        slug, rel = g.get("slug"), g.get("spec_dir")
        s = idx.get(slug) or {}
        if not rel:
            problems.append("chat_sources 群 %s 没填 spec_dir" % slug)
        elif s.get("route") != "config":
            problems.append("spec_dir=%s 指不到文件（实际路由=%s）" % (rel, s.get("route")))
        elif s.get("path"):
            head = spec_index._read(s["path"])
            m = re.search(r"slug[：:]\s*`?([A-Za-z0-9_]+)`?", head)
            if m and m.group(1) != slug:
                problems.append("%s 头部声明 slug=%s 与配置 %s 不一致" % (rel, m.group(1), slug))
            n = re.search(r"群名[：:]\s*([^\s　]+)", head)
            if n and n.group(1) not in (g.get("name") or ""):
                problems.append("%s 头部群名「%s」与配置「%s」不一致（alias_scope 按群名挂）"
                                % (rel, n.group(1), g.get("name")))
    seen = set()
    roots = [os.path.dirname(spec_index.GLOBAL_RULES_F), spec_index.REFS, spec_index.GROUP_SPECS,
             P.PKG_ROOT, os.path.join(spec_index.REFS, ".."),
             os.path.join(P.PROJECT_ROOT, "Database", "docs"), os.path.join(P.PROJECT_ROOT, "Database")]
    for f in [spec_index.GLOBAL_RULES_F] + [os.path.join(spec_index.GROUP_SPECS, d, "规范.md")
                                            for d in (os.listdir(spec_index.GROUP_SPECS)
                                                      if os.path.isdir(spec_index.GROUP_SPECS) else [])]:
        for name in MD_REF.findall(spec_index._read(f)):
            if name in seen:
                continue
            if not any(os.path.isfile(os.path.join(r, name)) for r in roots if os.path.isdir(r)):
                problems.append("引用了不存在的文件：%s" % name)   # 例如 rule 1 里的 memory.md
                seen.add(name)
    return problems


def norm(a):
    return str(a or "").strip().lstrip("+").lower()


def diff(db=None):
    db = db or default_db()
    alias = read_alias(db)
    rows = spec_rows()
    out = {"db": db, "db_found": alias is not None, "n_spec_rows": len(rows),
           "n_alias_rows": len(alias or {}), "only_in_spec": [], "only_in_db": [],
           "target_mismatch": [], "target_unchecked": 0, "spec_no_register": 0,
           "spec_self_map": 0, "broken_links": broken_links()}
    if alias is None:
        return out
    spec_keys = set()
    for r in rows:
        k = (r["group"], r["kind"], norm(r["alias"]))
        spec_keys.add(k)
        if r["intent"] == "不登记":
            out["spec_no_register"] += 1
            continue
        if r["intent"] == "自映射":
            out["spec_self_map"] += 1
            continue
        if k not in alias:
            out["only_in_spec"].append({"group": r["group"], "kind": r["kind"], "alias": r["alias"],
                                        "target": r["target_raw"]})
            continue
        if r["single"] and str(alias[k]).strip() != r["single"].strip():
            out["target_mismatch"].append({"group": r["group"], "kind": r["kind"], "alias": r["alias"],
                                           "spec": r["single"], "db": alias[k]})
        elif not r["single"]:
            out["target_unchecked"] += 1
    for (grp, kind, al), tgt in sorted(alias.items()):
        if (grp, kind, norm(al)) not in spec_keys:
            out["only_in_db"].append({"group": grp, "kind": kind, "alias": al, "target": tgt})
    return out


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description="规范缩写表 ↔ alias_scope 一致性体检")
    ap.add_argument("--db", default=None, help="台账库路径（默认 <project_root>/Database/freight_manual.db）")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--strict", action="store_true", help="有断链/路由不一致时退出码 1（供回归调用）")
    a = ap.parse_args()
    r = diff(a.db)
    if a.json:
        print(json.dumps(r, ensure_ascii=False, indent=1))
    else:
        print("库：%s（%s）· 规范缩写行 %d · alias_scope %d"
              % (r["db"], "可读" if r["db_found"] else "打不开", r["n_spec_rows"], r["n_alias_rows"]))
        for key, label in (("only_in_spec", "规范有、库里没登记"), ("only_in_db", "库里有、规范没写"),
                           ("target_mismatch", "两边目标不一致")):
            print("\n[%s] %d 条" % (label, len(r[key])))
            for x in r[key][:15]:
                print("   %-22s %-10s %-16s %s" % (x.get("group", "")[:22], x.get("kind"),
                                                    x.get("alias", "")[:16], (x.get("target") or x.get("spec") or "")[:34]))
        print("\n目标待人工核（规范行抽不出单值，不判对错）：%d 条" % r["target_unchecked"])
        print("已按规范语义跳过：叫你别登记的 %d 条 · 写法即规范值/港群的 %d 条"
              % (r["spec_no_register"], r["spec_self_map"]))
        print("[断链] %d 条" % len(r["broken_links"]))
        for x in r["broken_links"]:
            print("   - %s" % x)
    hard = bool(r["broken_links"]) or bool(r["target_mismatch"])
    sys.exit(1 if (a.strict and hard) else 0)
