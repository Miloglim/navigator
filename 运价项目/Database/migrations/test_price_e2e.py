"""test_price_e2e.py — 端到端测价格纠错：走 /api/pending 提交一批含异常价，看"备注"前缀"""
import sys, io, json, urllib.request
sys.stdout.reconfigure(encoding='utf-8')
sys.path.insert(0, r'C:\Users\YQN\Documents\正式版运价库\运价项目\Database')

with open(r'C:\Users\YQN\Documents\正式版运价库\运价项目\Database\token_manual.txt', encoding='utf-8') as f:
    TOKEN = f.read().strip()


def post(path, body):
    req = urllib.request.Request(
        'http://127.0.0.1:8795' + path,
        data=json.dumps(body, ensure_ascii=False).encode('utf-8'),
        headers={'Content-Type': 'application/json', 'x-push-token': TOKEN},
        method='POST')
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read().decode('utf-8'))


def get(path):
    req = urllib.request.Request('http://127.0.0.1:8795' + path, headers={'x-push-token': TOKEN})
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read().decode('utf-8'))


records = [
    {'来源群': 'P3 手测群', '发送人': 'P3 手测', '消息时间': '2026-09-16 09:00',
     '消息原文': 'P3 手测：ONE 宁波→墨西哥 20GP 13400',
     '起运港': 'Ningbo', '目的港': 'MEX', '船司': 'ONE', '柜型': '20GP',
     '海运费USD': 13400, '有效期船期': '9.16-9.22', '备注': ''},
    {'来源群': 'P3 手测群', '发送人': 'P3 手测', '消息时间': '2026-09-16 09:00',
     '消息原文': 'P3 手测：ONE 宁波→墨西哥 40HQ/HC 13600',
     '起运港': 'Ningbo', '目的港': 'MEX', '船司': 'ONE', '柜型': '40HQ/HC',
     '海运费USD': 13600, '有效期船期': '9.16-9.22', '备注': ''},
    {'来源群': 'P3 手测群', '发送人': 'P3 手测', '消息时间': '2026-09-16 09:00',
     '消息原文': 'P3 手测：ONE 宁波→墨西哥 40NOR 136000 (异常价)',
     '起运港': 'Ningbo', '目的港': 'MEX', '船司': 'ONE', '柜型': '40NOR',
     '海运费USD': 136000, '有效期船期': '9.16-9.22', '备注': ''},
]

res = post('/api/pending', {'table': 'rates', 'records': records, 'origin': 'agent'})
print('pending 提交:', res)
ids = res.get('ids') or []

# 直连 SQLite 按 ids 精确定位（避免 /api/pending 分页丢最新）
import sqlite3
conn = sqlite3.connect(r'C:\Users\YQN\Documents\正式版运价库\运价项目\Database\freight_manual.db')
conn.row_factory = sqlite3.Row
placeholders = ",".join("?" * len(ids))
rows = list(conn.execute(f"SELECT id, payload FROM pending_records WHERE id IN ({placeholders})", ids))
print()
print(f'拉回 {len(rows)} 条 payload：')
found = None
for r in rows:
    try:
        payload = json.loads(r['payload'])
    except Exception:
        continue
    p = payload.get('海运费USD')
    print(f'  id={r["id"]} 海运费USD={p!r} 柜型={payload.get("柜型")!r} 备注={payload.get("备注")!r}')
    if str(p) == '136000':
        found = payload

conn.close()

if found:
    print()
    print('异常价 136000 那条:')
    print('  价格建议:', repr(found.get('价格建议')))
    print('  备注(应干净):', repr(found.get('备注')))
    print('  含"13600":', '13600' in (found.get('价格建议') or ''))
    print('  备注未被污染:', not (found.get('备注') or ''))
else:
    print('未命中 136000 那条')

# 清理
for _id in ids:
    try:
        post('/api/pending/delete', {**{'id': _id}, 'force': True})
        print(f'清理 pending id={_id}')
    except Exception as e:
        print(f'清理失败 {_id}: {e}')
