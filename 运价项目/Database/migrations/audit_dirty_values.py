"""audit_dirty_values.py — 深挖业务表脏值：pol/pod/carrier/route/ctype/space_type 归一失败影响面"""
import sys, io, sqlite3, re
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')

DB = r'C:\Users\YQN\Documents\正式版运价库\运价项目\Database\freight_manual.db'
c = sqlite3.connect(DB); c.row_factory = sqlite3.Row

ports = {r['name'] for r in c.execute('SELECT name FROM ports WHERE active=1')}
port_aliases = set()
for r in c.execute('SELECT aliases FROM ports WHERE active=1'):
    for a in (r['aliases'] or '').split(','):
        if a.strip(): port_aliases.add(a.strip())
port_groups = {r['name'] for r in c.execute('SELECT name FROM port_groups WHERE active=1')}
carriers = {r['code'] for r in c.execute('SELECT code FROM carriers WHERE active=1')}
carrier_aliases = set()
for r in c.execute('SELECT aliases FROM carriers WHERE active=1'):
    for a in (r['aliases'] or '').split(','):
        if a.strip(): carrier_aliases.add(a.strip())
routes = {r['name'] for r in c.execute('SELECT name FROM routes WHERE active=1')}
# 规范档名三档（对齐 manualdb.py:273 CTYPE_ENUM），40HQ/HC 就是一个档名，不是复合值
ctype_ok = {'20GP', '40HQ/HC', '40NOR'}
st_ok = {'现舱','加班船','放舱开放','售罄','舱位紧张','截关截单','撤载改期','箱子动态','空班','跳港','首航'}


def fold(s):
    return re.sub(r"[\s.,\-()（）\u3000/、'\"]", '', str(s or '')).lower()


port_folded = {fold(x) for x in ports} | {fold(x) for x in port_groups} | {fold(x) for x in port_aliases}
carrier_folded = {fold(x) for x in carriers} | {fold(x) for x in carrier_aliases}
routes_folded = {fold(x) for x in routes}

print('=== BUG 1：pol/pod/carrier/route 存 alias 代号或脏值 ===')
for tbl, cols in [('freight_rates', ('pol','pod','carrier','route')), ('space_records', ('pol','pod','carrier','route'))]:
    for col in cols:
        if col in ('pol','pod'):
            folded = port_folded
        elif col == 'carrier':
            folded = carrier_folded
        else:
            folded = routes_folded
        bad = []
        total = 0
        for r in c.execute(f'SELECT {col} v, COUNT(*) n FROM {tbl} WHERE {col} IS NOT NULL AND {col}!=\"\" GROUP BY {col}'):
            total += r['n']
            if fold(r['v']) not in folded:
                bad.append((r['v'], r['n']))
        bad_n = sum(n for _, n in bad)
        bad.sort(key=lambda x: -x[1])
        pct = (bad_n/total*100) if total else 0
        flag = '✓' if not bad else '⚠️'
        print(f'  {flag} {tbl}.{col:10s} 总 {total:>4} 脏值 {bad_n:>4} ({pct:.1f}%)  top: {bad[:6]}')

print()
print('=== BUG 2：柜型 container_type 值分布 ===')
for tbl in ('freight_rates', 'space_records'):
    total = c.execute('SELECT COUNT(*) FROM ' + tbl).fetchone()[0]
    vals = list(c.execute(f'SELECT container_type v, COUNT(*) n FROM {tbl} WHERE container_type IS NOT NULL AND container_type!=\"\" GROUP BY container_type ORDER BY n DESC'))
    if not vals:
        print(f'  {tbl}: 无 ctype 值')
        continue
    top = [f"{r['v']}×{r['n']}" for r in vals[:10]]
    ctype_folded = {fold(x) for x in ctype_ok}
    # 含斜杠但不是规范档名（40HQ/HC 本身含斜杠却是合法的）→ 才算复合值
    combo = [(r['v'], r['n']) for r in vals if '/' in (r['v'] or '') and fold(r['v']) not in ctype_folded]
    non_std = [(r['v'], r['n']) for r in vals if fold(r['v']) not in ctype_folded]
    print(f'  {tbl} 总 {total}: 值分布 {top}')
    if combo:
        print(f'    ⚠️  含斜杠且非规范档名（异常写法）: {combo[:5]}')
    if non_std:
        print(f'    ⚠️  非三档白名单值: {non_std[:5]}')
    if not combo and not non_std:
        print(f'    ✓ 全部命中三档规范名（20GP / 40HQ/HC / 40NOR）')

print()
print('=== BUG 3：space_type 值分布（应严格 11 档内）===')
vals = list(c.execute('SELECT space_type v, COUNT(*) n FROM space_records WHERE space_type IS NOT NULL AND space_type!=\"\" GROUP BY space_type ORDER BY n DESC'))
if not vals:
    print('  无 space_type 值')
for r in vals:
    ok = r['v'] in st_ok
    mark = '✓' if ok else '⚠️'
    print(f'  {mark} {r["v"]:14s} ×{r["n"]:>4}')

print()
print('=== BUG 4：route 值分布（航线是否命中主数据 routes）===')
for tbl in ('freight_rates', 'space_records'):
    vals = list(c.execute(f'SELECT route v, COUNT(*) n FROM {tbl} WHERE route IS NOT NULL AND route!=\"\" GROUP BY route ORDER BY n DESC'))
    if not vals: continue
    bad = [(r['v'], r['n']) for r in vals if fold(r['v']) not in routes_folded]
    top = [f"{r['v']}×{r['n']}" for r in vals[:10]]
    print(f'  {tbl}: 值分布 {top}')
    if bad:
        print(f'    ⚠️  未命中 routes 主数据: {bad[:8]}')
