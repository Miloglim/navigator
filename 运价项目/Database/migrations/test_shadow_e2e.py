"""test_shadow_e2e.py — P2 影子重放端到端测：拿最近 pending id → baseline+no_alias 重放 → 看 diff → 清理"""
import sys, io, json, urllib.request
from e2e_guard import target
sys.stdout.reconfigure(encoding='utf-8')

_DB, BASE, TOKEN = target()


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


# 拿最近 3 条 pending id
d = get('/api/pending?limit=3')
ids = [int(r['id']) for r in d.get('rows', [])]
print('最近 pending ids:', ids)
if not ids:
    raise SystemExit('E2E fixture missing: create pending records in the isolated database first')

# 重放两种 mode
res = post('/api/shadow/replay', {'ids': ids, 'modes': ['baseline', 'no_alias']})
print('shadow_replay 结果:', res)

# 拉影子列表看 diff
sh = get('/api/shadow?limit=10')
print()
print(f'影子列表 ({len(sh.get("rows", []))} 条):')
for r in sh.get('rows', []):
    print(f"  id={r['id']} src={r['source_pending_id']} mode={r['mode']:8s} kind={r['kind']}")
    print(f"    diff: {r['diff_summary'][:100]}")

# 清理
c = post('/api/shadow/clear', {})
print()
print('清理影子:', c)
