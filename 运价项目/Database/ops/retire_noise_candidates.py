"""Retire reviewed candidate IDs only, preserving evidence and review reasons."""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import manualdb
import postgresdb

TARGETS = {4: ('Giaia tauro', 'Gioia Tauro'), 6: ('Gioia Tauro', 'Barcelona'),
           7: ('Spainsh base ports', 'Valencia'), 8: ('Bubadela', 'Bobadela'),
           19: ('CAACUPEMI ASUNCION', 'Asunción'), 20: ('CAACUPEMI PILAR', 'Pilar'),
           21: ('PUERTO SEGURO FLUVIAL', 'Villeta'), 22: ('TERPORT VILLETA', 'Villeta'),
           24: ('USD', 'Istanbul'), 25: ('华南基本港', 'Qingdao'),
           26: ('Base port via Ipanema', 'ECSA'), 27: ('Base port via Carioca', 'ECSA'),
           28: ('Base port via Santana', 'ECSA'), 29: ('Villa Do Conde', 'Vila do Conde'),
           30: ('RTM', 'Rotterdam')}


def run(apply=False):
    c = postgresdb.connect()
    try:
        c.execute('SET TRANSACTION READ WRITE' if apply else 'SET TRANSACTION READ ONLY')
        rows = c.execute('SELECT * FROM rule_candidates WHERE id IN (' + ','.join('?' for _ in TARGETS)
                         + ')' + (' FOR UPDATE' if apply else ''), list(TARGETS)).fetchall()
        result = []
        for row in rows:
            if (row['alias'], row['suggested_target']) != TARGETS[row['id']]:
                raise RuntimeError('Candidate identity changed: ' + str(row['id']))
            if row['status'] != 'pending':
                continue
            reason = manualdb.candidate_noise_reason(c, dict(row))
            if not reason:
                raise RuntimeError('Candidate needs fresh review: ' + str(row['id']))
            result.append({'id': row['id'], 'reason': reason})
        if apply:
            for item in result:
                c.execute("UPDATE rule_candidates SET status='rejected', reviewed_by=?, reviewed_at=?, reviewer_note=? WHERE id=? AND status='pending'",
                          ('agent:user-authorized-cleanup', manualdb.now_cn(), item['reason'], item['id']))
            c.commit()
        return {'ok': True, 'applied': apply, 'count': len(result), 'items': result}
    finally:
        c.close()


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    parser = argparse.ArgumentParser()
    parser.add_argument('--apply', action='store_true')
    print(json.dumps(run(parser.parse_args().apply), ensure_ascii=False))
