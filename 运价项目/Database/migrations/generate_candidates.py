"""generate_candidates.py — CLI 薄壳，调 manualdb.scan_failed_for_candidates

用法：
  python generate_candidates.py            # 默认扫 2000 条 failed、cutoff 0.75
  python generate_candidates.py --limit 500 --cutoff 0.8
"""
import sys, io, argparse, sqlite3
from pathlib import Path
sys.stdout.reconfigure(encoding='utf-8')
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import manualdb

DB_PATH = Path(__file__).resolve().parent.parent / "freight_manual.db"

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=2000)
    ap.add_argument("--cutoff", type=float, default=0.75)
    ap.add_argument("--db", type=Path, default=DB_PATH)
    args = ap.parse_args()
    conn = sqlite3.connect(str(args.db))
    conn.row_factory = sqlite3.Row
    try:
        stats = manualdb.scan_failed_for_candidates(conn, limit=args.limit, cutoff=args.cutoff)
    finally:
        conn.close()
    print('=== 生成候选统计 ===')
    for k, v in stats.items():
        if k == 'ok':
            continue
        print(f'  {k:24s} {v}')

if __name__ == '__main__':
    main()
