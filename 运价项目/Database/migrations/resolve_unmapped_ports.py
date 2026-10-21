"""resolve_unmapped_ports.py — 港口未映射自动归一：先只读分析，--write 才补 alias_scope。

策略（按安全度）：
  1) 剥中文/取拉丁部分后大小写/fold 精确命中主数据 ports/routes 规范名或 aliases → 高置信，补别名
  2) 连写(SanAntonio→San Antonio)/带国家码后缀(Cartagena→Cartagena(CO))/空格规整 后命中 → 高置信，补别名
  3) difflib 相似度>=0.82 唯一命中 → 建议，补别名（reason 标注）
  4) 命中歧义(多个) 或 完全不命中 → 不猜，列入"需人工/需补主数据"清单
航线(field=航线)同逻辑匹配 routes。
"""
import sys, io, json, re, sqlite3, difflib, argparse
from pathlib import Path
sys.stdout.reconfigure(encoding='utf-8')
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import manualdb

DB = Path(r'C:\Users\YQN\Documents\正式版运价库\运价项目\Database\freight_manual.db')

def fold(s):
    return re.sub(r"[\s.,\-()（）\u3000/、'\"’]", '', str(s or '')).lower()

def latin_part(s):
    """取拉丁/数字/空格部分，剥掉中文及括号中文：'Mersin 梅尔辛'→'Mersin'；'MALTA 马耳他'→'MALTA'"""
    m = re.match(r'^\s*([A-Za-z0-9][A-Za-z0-9 .\'&/\-]*?)\s*$', str(s or ''))
    if m:
        return m.group(1)
    # 含中文：取中文之前的拉丁前缀
    m2 = re.match(r'^([A-Za-z][A-Za-z0-9 .\'&/\-]*?)(?=[\u4e00-\u9fff]|$)', str(s or ''))
    return m2.group(1).strip() if m2 else str(s or '').strip()

def strip_cn(s):
    """去掉中文字符及其紧邻标点"""
    return re.sub(r'[\u4e00-\u9fff]+', '', str(s or '')).strip(' -—·、,')

def main(write=False):
    conn = sqlite3.connect(str(DB)); conn.row_factory = sqlite3.Row
    m = manualdb.get_master(conn)
    # 主数据可匹配池：name + aliases（英文/LOCODE/中文），带 fold→规范名 反查
    def build_pool(table, name_col, alias_col='aliases'):
        pool = {}  # fold(key) → {canonical}
        names = set()
        try:
            cols = [r[1] for r in conn.execute(f"PRAGMA table_info({table})")]
        except Exception:
            return pool, names
        ac = alias_col if alias_col in cols else None
        cols_needed = [name_col] + ([ac] if ac else [])
        q = f"SELECT {', '.join(cols_needed)} FROM {table}"
        if 'active' in cols:
            q += " WHERE active=1"
        for r in conn.execute(q):
            nm = r[name_col]
            if not nm:
                continue
            names.add(nm)
            pool.setdefault(fold(nm), set()).add(nm)
            if ac and r[ac]:
                for a in str(r[ac]).split(','):
                    a = a.strip()
                    if a:
                        pool.setdefault(fold(a), set()).add(nm)
            # 中文名/英文名列
            for extra in ('name_cn', 'name_en', 'label'):
                if extra in cols:
                    pass
        # 补 name_cn / name_en / label 匹配
        for extra in ('name_cn', 'name_en'):
            if extra in cols:
                for r in conn.execute(f"SELECT {name_col} n, {extra} x FROM {table}"):
                    if r['x']:
                        for tok in re.split(r'[,，、]', str(r['x'])):
                            tok = tok.strip()
                            if tok:
                                pool.setdefault(fold(tok), set()).add(r['n'])
        return pool, names

    ports_pool, ports_names = build_pool('ports', 'name')
    routes_pool, routes_names = build_pool('routes', 'name')
    # port_groups 也算 pod 目标
    for r in conn.execute("SELECT name n, label l FROM port_groups WHERE active=1"):
        ports_pool.setdefault(fold(r['n']), set()).add(r['n'])
        if r['l']:
            for tok in re.split(r'[/、（(]', str(r['l'])):
                tok = tok.strip().rstrip('）)')
                if tok:
                    ports_pool.setdefault(fold(tok), set()).add(r['n'])

    def match(kind_pool, kind_names, inp):
        """返回 (matched_canonical, how) 或 (None, reason)"""
        cand = fold(inp)
        if len(kind_pool.get(cand, set())) == 1:
            return next(iter(kind_pool[cand])), 'exact'
        if len(kind_pool.get(cand, set())) > 1:
            return None, 'ambiguous-direct'
        # 拉丁部分 / 剥中文
        for probe in {latin_part(inp), strip_cn(inp), inp.replace(' ', '')}:
            fp = fold(probe)
            hits = kind_pool.get(fp, set())
            if len(hits) == 1:
                return next(iter(hits)), f'strip→{probe!r}'
            if len(hits) > 1:
                return None, f'ambiguous({probe!r}:{len(hits)})'
        # difflib 唯一高置信
        lp = fold(latin_part(inp))
        names_folded = {fold(n): n for n in kind_names}
        close = difflib.get_close_matches(lp, list(names_folded.keys()), n=3, cutoff=0.82)
        uniq = {names_folded[c] for c in close}
        if len(uniq) == 1:
            return uniq.pop(), 'difflib'
        if len(uniq) > 1:
            return None, f'ambiguous-difflib({uniq})'
        return None, 'not-found'

    # 收集未映射值
    rows = list(conn.execute(
        "SELECT id, kind, payload, errors FROM pending_records "
        "WHERE check_status='failed' AND review_status='awaiting'"))
    from collections import defaultdict
    unmapped = {}  # (field, inp) → {pids, group}
    field_kind = {"目的港": "pod", "起运港": "pol", "航线": "route"}
    for row in rows:
        try:
            errs = json.loads(row["errors"] or "[]"); payload = json.loads(row["payload"] or "{}")
        except Exception:
            continue
        grp = str(payload.get("来源群") or "")
        for e in errs:
            f = e.get("field"); inp = str(e.get("input") or "").strip(); msg = str(e.get("msg") or "")
            if f not in field_kind or "不在" not in msg and "主数据" not in msg:
                continue
            key = (f, inp)
            rec = unmapped.setdefault(key, {"pids": [], "group": grp})
            rec["pids"].append(row["id"])

    auto, manual, review = [], [], []
    for (f, inp), info in unmapped.items():
        kind = field_kind[f]
        pool, names = (routes_pool, routes_names) if kind == 'route' else (ports_pool, ports_names)
        matched, how = match(pool, names, inp)
        if matched and (how == 'exact' or how.startswith('strip→')):
            auto.append((f, kind, inp, matched, how, info))       # 精确/剥中文命中主数据唯一名：安全
        elif matched:
            review.append((f, kind, inp, matched, how, info))     # difflib 命中：跨国风险，人工过目
        else:
            manual.append((f, inp, how, info))

    print(f'未映射值 {len(unmapped)} 个｜A 可安全自动补 {len(auto)}｜B 主数据查无/歧义需人工 {len(manual)}｜C difflib命中需确认 {len(review)}')
    print()
    print('=== A. 可安全补别名（exact/剥中文 命中主数据唯一规范名）===')
    for f, kind, inp, matched, how, info in sorted(auto, key=lambda x: (x[0], x[1])):
        print(f'  [{f}] {inp!r}  →  {matched!r}   ({how})   ×{len(info["pids"])} 群={info["group"]}')
    print()
    print('=== C. difflib 命中但跨国风险，需你确认后才补 ===')
    for f, kind, inp, matched, how, info in sorted(review, key=lambda x: (x[0], x[1])):
        print(f'  [{f}] {inp!r}  ~?  {matched!r}   ({how})   ×{len(info["pids"])} 群={info["group"]}')
    print()
    print('=== B. 需人工处理（主数据查无此港/航线，或归属歧义，不自动猜）===')
    for f, inp, how, info in sorted(manual, key=lambda x: (x[0], x[1])):
        print(f'  [{f}] {inp!r}  ({how})  ×{len(info["pids"])} 群={info["group"]}  pids={info["pids"][:5]}')

    if not write:
        print()
        print('（只读预演，未写库。加 --write 只补 A 组到 alias_scope，并对 A 组对应 pending 重跑归一改判）')
        return

    print()
    print('=== 写入 A 组别名到 alias_scope（scope_group 用来源群）===')
    conn2 = sqlite3.connect(str(DB)); conn2.row_factory = sqlite3.Row
    for f, kind, inp, matched, how, info in auto:
        grp = info["group"] or "*"
        if grp == "*":
            print(f'  跳过 {inp!r}：无明确来源群'); continue
        try:
            manualdb.alias_scope_save(conn2, {
                "alias": inp, "kind": kind, "target": matched,
                "scope_group": grp, "source": "manual",
                "note": f"港口未映射批量修复({how})，2026-09-17",
            })
            print(f'  +{grp} [{kind}] {inp!r}→{matched!r}')
        except Exception as e:
            print(f'  失败 {inp!r}: {e}')
    manualdb.invalidate_master()
    print()
    print('=== 重跑 A 组命中的 pending：用 payload 重新归一+校验，转 passed 的直接改判 ===')
    m = manualdb.get_master(conn2)
    flipped = 0
    for f, kind, inp, matched, how, info in auto:
        for pid in info["pids"]:
            row = conn2.execute("SELECT review_status FROM pending_records WHERE id=?", (pid,)).fetchone()
            if not row or row["review_status"] != "awaiting":
                continue
            kind_row = conn2.execute("SELECT kind FROM pending_records WHERE id=?", (pid,)).fetchone()["kind"]
            res = manualdb.update_pending(conn2, m, pid, {}, kind=kind_row, actor="港口批量修复")
            if res.get("check_status") == "passed":
                flipped += 1
    print(f'  重跑后转 passed（其余仍 failed=还含别的未映射字段或多错，正常）：{flipped} 条')
    conn2.close()

if __name__ == '__main__':
    ap = argparse.ArgumentParser()
    ap.add_argument('--write', action='store_true')
    a = ap.parse_args()
    main(write=a.write)
