"""Re-stage the two verified MSK price cards from the 2026-09-28 Turkey message.

Read-only source audit plus the normal validate -> pending path. Never approves
or changes an existing formal quote; --submit only creates review candidates.
"""
import argparse
import json
import sys
from collections import Counter
from datetime import date, timedelta
from pathlib import Path

import psycopg

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT.parent / 'Skill' / 'freight-rate-heartbeat' / 'scripts'
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(SCRIPTS))

from ops.postgres_sync import DEFAULT_ENV_FILE, load_dsn  # noqa: E402
import ledger_client  # noqa: E402


MESSAGE_ID = 'msgWsu8uRhQiNzMr2HT2zsfjA=='
GROUP = '土耳其价格更新'
WEEK_MEDIA = {
    41: '@lQLPKGotYCrws_nNAfXNBSGwB3lKNEN1fGIKi1Yfij24AA',
    42: '@lQLPJxRXnUX7MvnNAfPNBTKwvqE189S3m4UKi1Yfij24AQ',
}
# 42-week card, transcribed by destination and 20GP/40HQ column from its source image.
WEEK42_PRICES = {
    'Ashdod': (2730, 3700), 'Haifa': (2730, 3700),
    'Piraeus': (2880, 3800), 'Thessaloníki': (2960, 3900),
    'Limassol': (3040, 4000), 'ISTanbul': (2640, 3500),
    'Iskenderun': (2800, 3700), 'Izmit': (2640, 3500),
    'Port Said East': (2760, 3650), 'Gemlik': (2720, 3600),
    'Burgas': (3040, 4000), 'Alexandria': (3040, 4000),
    'İzmir': (2720, 3600), 'Mersin': (2720, 3600),
    'Chornomorsk': (3760, 4900), 'Damietta': (2760, 3650),
    'Port Said': (2880, 3800), 'Constanta': (3200, 4200),
    'Varna': (3040, 4000), 'Poti': (3120, 4100),
}
BOX_INDEX = {'20GP': 0, '40HQ/HC': 1}
EXPECTED_ROWS = len(WEEK42_PRICES) * len(BOX_INDEX)


def week_dates(number):
    start = date.fromisocalendar(2026, number, 1)
    return start.isoformat(), (start + timedelta(days=6)).isoformat()


def read_source():
    with psycopg.connect(load_dsn(DEFAULT_ENV_FILE)) as db:
        db.execute('SET TRANSACTION READ ONLY')
        rows = db.execute(
            "SELECT payload,check_status,review_status FROM pending_records "
            "WHERE payload::jsonb->>'消息ID'=%s ORDER BY id", (MESSAGE_ID,)).fetchall()
        source = [json.loads(payload) for payload, check, review in rows
                  if check == 'passed' and review == 'approved'
                  and json.loads(payload).get('来源群') == GROUP]
        awaiting = [json.loads(payload) for payload, _, review in rows if review == 'awaiting']
        formal = db.execute('SELECT count(*) FROM freight_rates WHERE mid=%s',
                            (MESSAGE_ID,)).fetchone()[0]
    if len(source) != EXPECTED_ROWS or formal != EXPECTED_ROWS:
        raise RuntimeError(f'Expected {EXPECTED_ROWS} approved and formal source rows; got {len(source)}, {formal}')
    keys = [(row.get('目的港'), row.get('柜型')) for row in source]
    if len(set(keys)) != EXPECTED_ROWS or set(pod for pod, _ in keys) != set(WEEK42_PRICES):
        raise RuntimeError('Approved destination/equipment matrix differs from the verified cards')
    if any(row.get('消息原文', '').count(WEEK_MEDIA[week]) != 1 for row in source for week in WEEK_MEDIA):
        raise RuntimeError('Source raw text does not contain both original media IDs exactly once')
    return source, awaiting


def build(source):
    records = []
    for week in (41, 42):
        start, end = week_dates(week)
        for original in source:
            record = dict(original)
            if week == 42:
                record['海运费USD'] = WEEK42_PRICES[record['目的港']][BOX_INDEX[record['柜型']]]
            record['有效期船期'] = f'{week}周'
            record['有效期起'], record['有效期止'] = start, end
            record.pop('AI判断', None)
            record['图片名'] = WEEK_MEDIA[week]
            record['备注'] = (record.get('备注', '')
                            .replace('第二张价卡', f'{week}周价卡')
                            .replace('价卡行与第一张逐行并列保留。', '两周价卡分别留痕。'))
            records.append(record)
    return records


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--submit', action='store_true', help='Stage via normal reviewer queue')
    args = parser.parse_args()
    source, awaiting = read_source()
    records = build(source)
    existing = {(str(r.get('目的港') or '').casefold(), r.get('柜型'), r.get('有效期船期'),
                 str(r.get('海运费USD')), r.get('有效期起'), r.get('有效期止'))
                for r in awaiting}
    expected = {(r['目的港'].casefold(), r['柜型'], r['有效期船期'],
                 str(r['海运费USD']), r['有效期起'], r['有效期止']) for r in records}
    if expected <= existing:
        print(json.dumps({'ok': True, 'already_staged': len(expected)}, ensure_ascii=False))
        return
    checked = ledger_client.validate('rates', records)
    if not checked.get('ok') or checked.get('bad') or checked.get('good') != len(records):
        print(json.dumps({'ok': False, 'good': checked.get('good'), 'bad': checked.get('bad'),
                          'errors': [r.get('errors') for r in checked.get('results', []) if not r.get('ok')][:3]},
                         ensure_ascii=False))
        raise SystemExit(2)
    print(json.dumps({'ok': True, 'validated': len(records), 'by_week': dict(Counter(r['有效期船期'] for r in records)),
                      'sample': [(r['目的港'], r['柜型'], r['海运费USD'], r['有效期起'], r['有效期止'])
                                 for r in records if r['目的港'] == 'Ashdod']}, ensure_ascii=False))
    if args.submit:
        result = ledger_client.submit('rates', records)
        if not result.get('ok') or result.get('staged') != len(records) or result.get('check_failed'):
            raise RuntimeError('Pending submission was incomplete: ' + json.dumps(
                {key: result.get(key) for key in ('ok', 'staged', 'check_failed', 'error')}, ensure_ascii=False))
        print(json.dumps({'ok': True, 'staged': result['staged'], 'check_passed': result['check_passed'],
                          'first_id': result['ids'][0], 'last_id': result['ids'][-1]}, ensure_ascii=False))


if __name__ == '__main__':
    main()
