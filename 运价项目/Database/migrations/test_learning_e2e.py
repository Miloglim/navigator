"""test_learning_e2e.py — P1/P2/P3-B 联合端到端：
造 3 条同维度 pending → update 留痕（值替换+补默认两类）→ SQL 造 ts 跨 3 天
→ scan 两源 → 三维候选生成 → 批准 alias_scope 类（验证规范.md 追加）
→ 批准 field_default 类（验证 field_defaults 表）→ audit-hints 验证 → 清理
"""
import sys, io, json, sqlite3, urllib.request, urllib.parse
from pathlib import Path
sys.stdout.reconfigure(encoding='utf-8')

DB = Path(r'C:\Users\YQN\Documents\正式版运价库\运价项目\Database\freight_manual.db')
TOKEN = (DB.parent / "token_manual.txt").read_text(encoding='utf-8').strip()
SPEC = Path(r'C:\Users\YQN\Documents\正式版运价库\运价项目\Skill\freight-rate-heartbeat\references\群聊规范\土耳其价格更新\规范.md')


def post(path, body):
    req = urllib.request.Request('http://127.0.0.1:8795' + path,
        data=json.dumps(body, ensure_ascii=False).encode('utf-8'),
        headers={'Content-Type': 'application/json', 'x-push-token': TOKEN}, method='POST')
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read().decode('utf-8'))


def get(path):
    req = urllib.request.Request('http://127.0.0.1:8795' + path, headers={'x-push-token': TOKEN})
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read().decode('utf-8'))


spec_before = SPEC.read_text(encoding='utf-8') if SPEC.exists() else ''

# --- Step 1：造 3 条同维度 pending（船期ETD 各不同避免 identity 相同被撤旧留新）---
pids = []
for i, day in enumerate(["2026-09-15", "2026-09-16", "2026-09-17"]):
    rec = {
        "来源群": "土耳其价格更新", "发送人": "学习测试员", "消息时间": f"{day} 10:00",
        "消息原文": f"学习测试#{i}：MSC 宁波→Izmit 40HQ/HC 3400",
        "起运港": "Ningbo", "目的港": "Izmit", "船司": "MSC", "柜型": "40HQ/HC",
        "海运费USD": 3400 + i, "有效期起": "2026-09-20", "有效期止": "2026-09-28",
        "船期ETD": f"2026-09-2{i+1}", "备注": "",
    }
    res = post('/api/pending', {"table": "rates", "records": [rec], "origin": "agent"})
    pids.append((res.get('ids') or [None])[0])
print(f'Step1 造 3 条 pending: {pids}')

# --- Step 2：每条 update 两类修改（值替换 目的港 + 补默认 船期ETD 统一改 09-25）---
for pid in pids:
    u1 = post('/api/pending/update', {"id": pid, "fields": {"目的港": "Ambarli"}, "reviewer": "学习测试员"})
    u2 = post('/api/pending/update', {"id": pid, "fields": {"船期ETD": "2026-09-25"}, "reviewer": "学习测试员"})
    print(f'  update {pid}: edits={u1.get("edits_recorded")}+{u2.get("edits_recorded")} check={u2.get("check_status")}')

# --- Step 3：SQL 把 edit_log ts 改成跨 3 天（模拟真实时间分布）---
c = sqlite3.connect(str(DB)); c.row_factory = sqlite3.Row
for pid, day in zip(pids, ["2026-09-15", "2026-09-16", "2026-09-17"]):
    row = c.execute('SELECT edit_log FROM pending_records WHERE id=?', (pid,)).fetchone()
    if not row:
        print(f'  !! pending {pid} 不存在（被撤旧留新删掉？）')
        continue
    log_list = json.loads(row['edit_log'] or '[]')
    for e in log_list:
        e['ts'] = f"{day} 10:30:00"
    c.execute('UPDATE pending_records SET edit_log=? WHERE id=?', (json.dumps(log_list, ensure_ascii=False), pid))
c.commit()
print('Step3 edit_log ts 已改为跨 3 天')

# --- Step 4：scan 两源 ---
sc = post('/api/candidates/scan', {"limit": 500, "cutoff": 0.75})
print(f'\nStep4 scan: difflib={sc.get("difflib", {}).get("candidates_created", "?")} '
      f'audit 新建={sc.get("audit", {}).get("candidates_created", "?")} '
      f'累加={sc.get("audit", {}).get("candidates_updated", "?")} '
      f'低于门槛={sc.get("audit", {}).get("below_threshold", "?")}')

# --- Step 5：查候选 ---
cd = get('/api/candidates?status=pending&source=audit_correction')
print(f'\nStep5 audit_correction 候选 {cd["total"]} 条:')
alias_cand = default_cand = None
for r in cd['rows']:
    print(f'  id={r["id"]} layer={r["target_layer"]} scope_key={r["scope_key"]!r}')
    print(f'    {r["alias"]!r} ({r["kind"]}) → {r["suggested_target"]!r} conf={r["confidence"]} 证据={r["evidence_count"]}')
    if r['target_layer'] == 'alias_scope' and not alias_cand:
        alias_cand = r
    if r['target_layer'] == 'field_default' and not default_cand:
        default_cand = r

# --- Step 6：批准 alias_scope 类 → 验证 alias_scope + 规范.md 追加 ---
if alias_cand:
    ap = post('/api/candidates/approve', {"id": alias_cand['id'], "reviewer": "学习测试员"})
    print(f'\nStep6 批准 alias 候选: ok={ap.get("ok")} action={ap.get("layer_action", {}).get("action")}')
    al = get(f'/api/alias?q={alias_cand["alias"]}')
    print(f'  alias_scope 命中: {al["total"]} 条')
    spec_after = SPEC.read_text(encoding='utf-8') if SPEC.exists() else ''
    added = '别名登记日志' in spec_after and spec_after != spec_before
    print(f'  P3-B 规范.md 追加日志: {"OK" if added else "未追加（检查群名→目录名映射）"}')
    if added:
        tail = [l for l in spec_after.splitlines() if '别名登记' in l or l.startswith('- 2026')]
        print(f'  日志行: {tail[-1] if tail else "?"}')
else:
    print('\nStep6 跳过：无 alias_scope 类候选')

# --- Step 7：批准 field_default 类 → 验证 field_defaults 表 ---
if default_cand:
    ap = post('/api/candidates/approve', {"id": default_cand['id'], "reviewer": "学习测试员"})
    print(f'\nStep7 批准 default 候选: ok={ap.get("ok")} action={ap.get("layer_action", {}).get("action")}')
    fd = list(c.execute("SELECT scope_key, field, default_value, evidence_count, status FROM field_defaults"))
    print(f'  field_defaults 表 {len(fd)} 行:')
    for r in fd:
        print(f'    {r["scope_key"]!r} {r["field"]}={r["default_value"]!r} 证据={r["evidence_count"]} {r["status"]}')
else:
    print('\nStep7 跳过：无 field_default 类候选')

# --- Step 8：audit-hints 验证 ---
h = get('/api/audit-hints?group=' + urllib.parse.quote('土耳其价格更新') + '&sender=' + urllib.parse.quote('学习测试员'))
print(f'\nStep8 audit-hints: field_stats 键={list((h.get("field_stats") or {}).keys())} defaults={len(h.get("defaults") or [])} 条')
for f, st in (h.get('field_stats') or {}).items():
    print(f'  {f}: 被改 {st["count"]} 次 top={st["top"]}')

# --- Step 9：清理（先 SQL 后 HTTP，SQL 连接用完即关避免锁冲突）---
print('\nStep9 清理:')
for r in cd['rows']:
    c.execute('DELETE FROM rule_candidates WHERE id=?', (r['id'],))
c.execute('DELETE FROM field_defaults')
c.commit()
c.close()
if alias_cand:
    post('/api/alias/delete', {"alias": alias_cand['alias'], "kind": alias_cand['kind'],
                               "scope_group": alias_cand['scope_key'].split('|')[-1]})
for pid in pids:
    post('/api/pending/delete', {**{"id": pid}, 'force': True})
# 规范.md 回滚（去掉测试追加的日志节）
if SPEC.exists() and spec_before:
    SPEC.write_text(spec_before, encoding='utf-8')
    print('  规范.md 已回滚')
c2 = sqlite3.connect(str(DB))
n_pd = c2.execute('SELECT COUNT(*) FROM pending_records').fetchone()[0]
n_cd = c2.execute('SELECT COUNT(*) FROM rule_candidates').fetchone()[0]
n_fd = c2.execute('SELECT COUNT(*) FROM field_defaults').fetchone()[0]
n_al = c2.execute('SELECT COUNT(*) FROM alias_scope').fetchone()[0]
c2.close()
print(f'  清理验证: pending={n_pd} candidates={n_cd} field_defaults={n_fd} alias_scope={n_al}（应 0/0/0/36）')
