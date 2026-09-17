"""audit_ctype_combo.py — 深挖"40HQ/HC"复合柜型的入库路径"""
import sys, io, sqlite3
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')

DB = r'C:\Users\YQN\Documents\正式版运价库\运价项目\Database\freight_manual.db'
c = sqlite3.connect(DB); c.row_factory = sqlite3.Row

print('=== 1. 按 origin/ai_flag 分：40HQ/HC 是不是 agent 生成？ ===')
# freight_rates 有 ai_flag
rows = list(c.execute(
    "SELECT container_type, origin, ai_flag, COUNT(*) n FROM freight_rates "
    "WHERE container_type IS NOT NULL AND container_type!='' "
    "GROUP BY container_type, origin, ai_flag ORDER BY n DESC LIMIT 20"
))
print('-- freight_rates --')
for r in rows:
    print(f"  {r['container_type']:12s} origin={r['origin']:8s} ai_flag={(r['ai_flag'] or 'NULL'):12s} n={r['n']}")
# space_records 无 ai_flag
rows = list(c.execute(
    "SELECT container_type, origin, COUNT(*) n FROM space_records "
    "WHERE container_type IS NOT NULL AND container_type!='' "
    "GROUP BY container_type, origin ORDER BY n DESC LIMIT 20"
))
print('-- space_records (无 ai_flag 列) --')
for r in rows:
    print(f"  {r['container_type']:12s} origin={r['origin']:8s} n={r['n']}")
print()

print('=== 2. 按 source_group + 时间：40HQ/HC 集中在哪些群/时段？ ===')
for tbl in ('freight_rates', 'space_records'):
    rows = list(c.execute(
        f"SELECT source_group, substr(validity_raw or message_time, 1, 10) timebucket, COUNT(*) n "
        f"FROM {tbl} WHERE container_type='40HQ/HC' "
        f"GROUP BY source_group, timebucket ORDER BY n DESC LIMIT 12"
    ))
    print(f'-- {tbl} --')
    for r in rows:
        print(f"  {(r['source_group'] or 'NULL'):24s} t={r['timebucket'] or '?':10s} n={r['n']}")
    print()

print('=== 3. 抽样 5 条 40HQ/HC 消息，看 message_text 里是不是原文写了"40HQ/HC" ===')
for r in c.execute("SELECT source_group, container_type, message_text, ai_flag FROM freight_rates WHERE container_type='40HQ/HC' LIMIT 5"):
    mt = (r['message_text'] or '').replace('\n', ' ')[:200]
    print(f"  [{r['source_group']}] ai_flag={r['ai_flag'] or 'None'} ctype={r['container_type']}")
    print(f"    msg: {mt}")
    print()

print('=== 4. 抽样 pending 里 ctype=40HQ/HC 的分布（未审的中间态） ===')
rows = list(c.execute(
    "SELECT json_extract(payload, '$.柜型') ctype, review_status, COUNT(*) n "
    "FROM pending_records "
    "WHERE json_extract(payload, '$.柜型') IS NOT NULL "
    "GROUP BY ctype, review_status ORDER BY n DESC LIMIT 15"
))
for r in rows:
    print(f"  ctype={str(r['ctype']):20s} status={r['review_status']:10s} n={r['n']}")

print()
print('=== 5. 关键：这些 40HQ/HC 走的是哪条 push 路径？（memory 说 expand_cn 会把 40HQ/HC 拆两条）===')
# expand_cn 里 ctype_list 处理 '40HQ/HC'：会拆成 ['40HQ','40HC'] 各生成一条 rates
# 若业务表里存了 '40HQ/HC' 未拆，说明走了非 expand_cn 路径（比如 /api/push 人工批量导入？）
# 或 expand_cn 逻辑改动前后
# 看 origin 分布：manual=push 或 record_builder；agent=AI 判定
r = list(c.execute("SELECT origin, COUNT(*) n FROM freight_rates WHERE container_type='40HQ/HC' GROUP BY origin"))
print(f"  freight_rates 40HQ/HC 按 origin: {[(x['origin'], x['n']) for x in r]}")
