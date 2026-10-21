"""Pure semantic manifest comparison; counts alone cannot prove completeness."""
import hashlib
import json
from core.contracts import SUBMISSION_COVERAGE_VERSION, SUBMISSION_IDENTITY_FIELDS


def projection(kind, record):
    out = {key: str(record.get(key) if record.get(key) is not None else '').strip()
           for key in SUBMISSION_IDENTITY_FIELDS}
    price = record.get('海运费USD')
    if price in (None, ''):
        price = record.get('价格USD')
    out['price'] = str(price if price is not None else '').strip()
    out['kind'] = kind
    return out


def fingerprint(kind, record):
    return hashlib.sha256(json.dumps(projection(kind, record), ensure_ascii=False, sort_keys=True).encode('utf-8')).hexdigest()


def compare_submission(kind, expected, actual):
    wanted = {fingerprint(kind, rec): projection(kind, rec) for rec in expected}
    entries = [{'entry_id':row['entry_id'], 'fingerprint':fingerprint(kind,row['payload']),
                'message_id':str(row['payload'].get('消息ID') or '')} for row in actual]
    found = {fingerprint(kind, row.get('comparison_payload', row['payload'])) for row in actual}
    missing = [record for key,record in wanted.items() if key not in found]
    return {'version':SUBMISSION_COVERAGE_VERSION,'ok':not missing,
            'expected_unique':len(wanted),'actual_unique':len(found),
            'exact_duplicates_merged':len(expected)-len(wanted),'missing':missing,'entries':entries}


def assess_receipt(receipt, entries, source_counts):
    coverage = receipt.get('coverage')
    issues = []
    if not coverage:
        absent = [mid for mid in receipt.get('message_ids',[]) if not source_counts.get(mid)]
        if absent:
            issues.append({'reason':'source_missing','message_ids':absent})
        if receipt.get('staged',0) < receipt.get('requested',0):
            issues.append({'reason':'legacy_partial_unverified','requested':receipt['requested'],'staged':receipt.get('staged',0)})
        return issues
    if coverage.get('missing'):
        issues.append({'reason':'missing_at_submission','records':coverage['missing']})
    for expected in coverage.get('entries',[]):
        row = entries.get(expected['entry_id'])
        if row is None:
            issues.append({'reason':'entry_missing','entry_id':expected['entry_id'],'message_id':expected['message_id']})
        elif row['message_id'] != expected['message_id']:
            issues.append({'reason':'source_replaced','entry_id':expected['entry_id']})
        elif row['fingerprint'] != expected['fingerprint'] and not row.get('edited'):
            issues.append({'reason':'unexplained_change','entry_id':expected['entry_id']})
    return issues
