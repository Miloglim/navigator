"""test_audit_e2e.py — P0/P3-A 端到端：造 pending → update 改字段 → 查 edit_log → approve → 查 approve_diff + 观察流候选 → 清理"""
import sys, io, json, sqlite3, urllib.request
from pathlib import Path
sys.stdout.reconfigure(encoding='utf-8')

DB = Path(r'C:\Users\YQN\Documents\正式版运价库\运价项目\Database\freight_manual.db')
TOKEN = (DB.parent / "token_manual.txt").read_text(encoding='utf-8').strip()


def post(path, body):
    req = urllib.request.Request(
        'http://127.0.0.1:8795' + path,
        data=json.dumps(body, ensure_ascii=False).encode('utf-8'),
        headers={'Content-Type': 'application/json', 'x-push-token': TOKEN},
        method='POST')
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read().decode('utf-8'))


def get(path):
    req = urllib.request.Request('http://127.0.0.1:8795' + path, headers={'x-push-token': TOKEN})
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read().decode('utf-8'))


# --- Step 1：造一条正常 pending（会 passed）---
rec = {
    "来源群": "土耳其价格更新", "发送人": "审计测试员", "消息时间": "2026-09-17 12:00",
    "消息原文": "审计测试：MSC 宁波→Ambarli 40HQ/HC 3400",
    "起运港": "Ningbo", "目的港": "Izmit", "船司": "MSC", "柜型": "40HQ/HC",
    "海运费USD": 3400, "有效期起": "2026-09-18", "有效期止": "2026-09-25",
    "船期ETD": "2026-09-20", "备注": "",
}
res = post('/api/pending', {"table": "rates", "records": [rec], "origin": "agent"})
pid = (res.get('ids') or [None])[0]
print(f'Step1 pending id={pid} staged={res.get("staged")}')

c = sqlite3.connect(str(DB)); c.row_factory = sqlite3.Row
row = c.execute('SELECT check_status, orig_payload IS NOT NULL has_orig, edit_log FROM pending_records WHERE id=?', (pid,)).fetchone()
print(f'  check_status={row["check_status"]}  orig_payload 已存={bool(row["has_orig"])}  edit_log={row["edit_log"]!r}')

# --- Step 2：update 改目的港 Izmit→Ambarli（模拟审核员修正），带 reviewer ---
u = post('/api/pending/update', {"id": pid, "fields": {"目的港": "Ambarli"}, "reviewer": "审计测试员"})
print(f'\nStep2 update: ok={u.get("ok")} check={u.get("check_status")} edits_recorded={u.get("edits_recorded")}')
row = c.execute('SELECT edit_log FROM pending_records WHERE id=?', (pid,)).fetchone()
log = json.loads(row['edit_log'] or '[]')
print(f'  edit_log {len(log)} 条:')
for e in log:
    print(f'    {e}')

# --- Step 3：approve（放行）→ 应记 approve + approve_diff（目的港 Izmit→Ambarli 对比 orig）→ 观察流候选 ---
a = post('/api/pending/approve', {"id": pid, "reviewed_by": "审计测试员"})
print(f'\nStep3 approve: ok={a.get("ok")} audit_diffs={a.get("audit_diffs")} observed_candidates={a.get("observed_candidates")}')
row = c.execute('SELECT edit_log FROM pending_records WHERE id=?', (pid,)).fetchone()
log = json.loads(row['edit_log'] or '[]')
print(f'  edit_log 累计 {len(log)} 条:')
for e in log:
    print(f'    {e}')

# --- Step 4：观察流候选应出现在 rule_candidates（sender+carrier+group 三维）---
cd = get('/api/candidates?status=pending&source=agent_observation')
print(f'\nStep4 agent_observation 候选 {cd["total"]} 条:')
cand_id = None
for r in cd['rows']:
    print(f'  id={r["id"]} scope_kind={r["scope_kind"]} scope_key={r["scope_key"]!r}')
    print(f'    {r["alias"]!r} ({r["kind"]}) → {r["suggested_target"]!r}  conf={r["confidence"]}')
    print(f'    reason: {r["reason"]}')
    cand_id = r['id']

# --- Step 5：清理（业务表这条放行产生的 rates + pending + 候选 + alias 若有）---
print('\nStep5 清理:')
if cand_id:
    post('/api/candidates/reject', {"id": cand_id, "reviewer": "e2e", "note": "测试清理"})
    c.execute('DELETE FROM rule_candidates WHERE id=?', (cand_id,)); c.commit()
# 删放行进业务表的行
row2 = c.execute('SELECT final_key FROM pending_records WHERE id=?', (pid,)).fetchone()
fk = row2['final_key'] if row2 else None
if fk:
    for k in str(fk).split(','):
        c.execute('DELETE FROM freight_rates WHERE content_key=?', (k.strip(),))
    c.commit()
    print(f'  业务表已删 final_key={fk}')
post('/api/pending/delete', {**{"id": pid}, 'force': True})
n_pd = c.execute('SELECT COUNT(*) FROM pending_records').fetchone()[0]
n_cd = c.execute('SELECT COUNT(*) FROM rule_candidates').fetchone()[0]
n_rt = c.execute('SELECT COUNT(*) FROM freight_rates').fetchone()[0]
c.close()
print(f'  清理验证: pending={n_pd} candidates={n_cd} rates={n_rt}（应全 0）')
