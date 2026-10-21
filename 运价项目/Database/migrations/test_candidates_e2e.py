"""test_candidates_e2e.py — 端到端测候选闭环：
   造 failed pending → difflib 扫生成候选 → 批准 → 落 alias_scope → 清理
"""
import sys, io, json, sqlite3, subprocess, urllib.request
from pathlib import Path
from e2e_guard import target
sys.stdout.reconfigure(encoding='utf-8')

DB, BASE, TOKEN = target()


def post(path, body):
    req = urllib.request.Request(
        BASE + path,
        data=json.dumps(body, ensure_ascii=False).encode('utf-8'),
        headers={'Content-Type': 'application/json', 'x-push-token': TOKEN},
        method='POST')
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read().decode('utf-8'))


def get(path):
    req = urllib.request.Request(BASE + path, headers={'x-push-token': TOKEN})
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read().decode('utf-8'))


# --- Step 1：提交一条含拼写错目的港的 pending，stage_pending 应判 failed ---
bad = {
    "table": "rates",
    "records": [{
        "来源群": "土耳其价格更新", "发送人": "候选闭环测试", "消息时间": "2026-09-17 11:00",
        "消息原文": "候选闭环测试：MSC 宁波→Ambarlii 40HQ/HC 3400",
        "起运港": "Ningbo", "目的港": "Ambarlii", "船司": "MSC", "柜型": "40HQ/HC",
        "海运费USD": 3400, "有效期船期": "9.17-9.25", "备注": "",
    }],
    "origin": "agent",
}
res = post('/api/pending', bad)
print('Step1 pending 提交:', res)
pending_id = (res.get('ids') or [None])[0]

c = sqlite3.connect(str(DB)); c.row_factory = sqlite3.Row
row = c.execute('SELECT id, check_status, errors FROM pending_records WHERE id=?', (pending_id,)).fetchone()
print(f'  pending id={row["id"]} check_status={row["check_status"]}')
print(f'  errors: {row["errors"][:200]}')
c.close()

if row['check_status'] != 'failed':
    print('!! 用例前提不成立（应 failed），终止')
    sys.exit(1)

# --- Step 2：跑 difflib 扫描生成候选 ---
print()
print('Step2 跑 difflib 扫描:')
gen = subprocess.run(
    [sys.executable, str(Path(__file__).resolve().parent / 'generate_candidates.py'), '--db', str(DB)],
    capture_output=True, text=True, encoding='utf-8', errors='replace')
print(gen.stdout)
if gen.stderr.strip():
    print('STDERR:', gen.stderr[:400])

# --- Step 3：看候选 ---
cd = get('/api/candidates?status=pending')
print()
print(f'Step3 候选列表 ({cd["total"]} 条):')
for r in cd['rows']:
    print(f'  id={r["id"]} alias={r["alias"]!r} kind={r["kind"]} → {r["suggested_target"]!r} conf={r["confidence"]:.2f} src={r["source"]}')
    print(f'    reason: {r["reason"][:100]}')

# --- Step 4：批准第一条 ---
target_cand = next((r for r in cd['rows'] if r['alias'] == 'Ambarlii'), None)
if not target_cand:
    print('!! 候选里没找到 Ambarlii，终止')
    sys.exit(1)

ap_res = post('/api/candidates/approve', {'id': target_cand['id'], 'reviewer': 'e2e-test'})
print()
print('Step4 批准候选:', ap_res)

# --- Step 5：alias_scope 应多一条 Ambarlii → Ambarli ---
al = get('/api/alias?q=Ambarlii')
print()
print(f'Step5 alias_scope 命中 "Ambarlii": {al["total"]} 条')
for r in al['rows']:
    print(f'  {r["alias"]}·{r["kind"]}·{r["scope_group"]} → {r["target"]}  src={r["source"]}')
    print(f'  note: {r["note"]}')

# --- Step 6：清理测试痕迹 ---
print()
post('/api/alias/delete', {'alias': 'Ambarlii', 'kind': 'pod', 'scope_group': '土耳其价格更新'})
post('/api/pending/delete', {**{'id': pending_id}, 'force': True})
c = sqlite3.connect(str(DB))
c.execute('DELETE FROM rule_candidates WHERE id=?', (target_cand['id'],))
c.commit()
c.close()
print('Step6 已清理测试痕迹（Ambarlii 别名 + 测试 pending + 测试候选）')
al2 = get('/api/alias?q=Ambarlii')
p2 = get(f'/api/pending?limit=1&pending_id={pending_id}')
c2 = sqlite3.connect(str(DB)); n = c2.execute('SELECT COUNT(*) FROM rule_candidates').fetchone()[0]; c2.close()
print(f'  清理验证：Ambarlii 别名={al2["total"]}（应 0）、rule_candidates={n}（应 0）')
