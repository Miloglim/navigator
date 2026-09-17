"""purge_business_tables.py — 一次性清空 4 张数据表（freight_rates / space_records / pending_records / pending_shadow）"""
import sys, io, sqlite3
sys.stdout.reconfigure(encoding='utf-8')

DB = r'C:\Users\YQN\Documents\正式版运价库\运价项目\Database\freight_manual.db'
TABLES = ['freight_rates', 'space_records', 'pending_records', 'pending_shadow']

conn = sqlite3.connect(DB)
print('=== 清空前 ===')
for t in TABLES:
    n = conn.execute(f'SELECT COUNT(*) FROM {t}').fetchone()[0]
    print(f'  {t:20s} {n:>5} 条')

# 事务里删，全成功才 commit；任何一步炸就 rollback
try:
    for t in TABLES:
        cur = conn.execute(f'DELETE FROM {t}')
        print(f'  DELETE {t}: {cur.rowcount} 行')
    # 顺带重置 sqlite_sequence 让新入库 id 从 1 开始（可选）
    conn.execute("DELETE FROM sqlite_sequence WHERE name IN ({})".format(
        ",".join("?" * len(TABLES))), TABLES)
    conn.commit()
    print('\n[OK] 事务已提交')
except Exception as e:
    conn.rollback()
    print(f'\n[FAIL] 事务回滚：{e}')
    raise

print()
print('=== 清空后 ===')
for t in TABLES:
    n = conn.execute(f'SELECT COUNT(*) FROM {t}').fetchone()[0]
    print(f'  {t:20s} {n:>5} 条')

print()
print('=== 保留（主数据/别名/配置） ===')
for t in ['ports', 'carriers', 'routes', 'senders', 'port_groups', 'chat_groups', 'operators', 'alias_scope']:
    n = conn.execute(f'SELECT COUNT(*) FROM {t}').fetchone()[0]
    print(f'  {t:20s} {n:>5} 条')

conn.close()
