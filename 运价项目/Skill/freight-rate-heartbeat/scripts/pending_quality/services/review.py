"""Pure source grouping, prioritization and completion checks; no IO."""
import hashlib
import json
import re
from pending_quality.core.contracts import OUTCOMES, MIN_NOTE_LENGTH

def fingerprint(rows):
    data = [{'id':r['id'], 'kind':r['kind'], 'payload':r.get('payload') or {},
             'check_status':r.get('check_status'), 'review_status':r.get('review_status'),
             'edit_log':r.get('edit_log')} for r in sorted(rows,key=lambda r:r['id'])]
    return hashlib.sha256(json.dumps(data,ensure_ascii=False,sort_keys=True,default=str).encode()).hexdigest()

def source_key(group, message_id):
    return hashlib.sha256(json.dumps([group,message_id],ensure_ascii=False).encode()).hexdigest()

def group_rows(rows):
    grouped = {}
    for r in rows:
        if r.get('origin')!='agent' or r.get('review_status')!='awaiting' or r.get('kind') not in ('rates','space'):
            continue
        p=r.get('payload') or {}; mid=str(p.get('消息ID') or '').strip()
        group=str(p.get('来源群') or '').strip()
        # Missing provenance is a review target too, but it never grants repair authority.
        identity = mid or ('entry:'+str(r['id']))
        key=source_key(group,identity)
        grouped.setdefault(key,{'key':key,'group':group,'message_id':mid,'rows':[]})['rows'].append(r)
    result=[]
    for item in grouped.values():
        item['fingerprint']=fingerprint(item['rows'])
        item['signals']=signals(item['rows'])
        item['priority']=len(item['signals']) if all(r.get('check_status')=='passed' for r in item['rows']) else -1
        item['oldest_id']=min(r['id'] for r in item['rows'])
        result.append(item)
    return result

def signals(rows):
    p=rows[0].get('payload') or {}; text=str(p.get('消息原文') or '').split('——引用',1)[0]
    findings=[]
    if not p.get('消息ID') or not p.get('来源群') or not text.strip():findings.append('来源证据缺失')
    if any(r.get('check_status')!='passed' for r in rows):findings.append('最终校验未过：只能转人工，不再重复修正')
    quoted=bool(re.search(r'\d{3,5}\s*/\s*\d{3,5}',text))
    if quoted and not any(str((r.get('payload') or {}).get(k) or '').strip() for r in rows for k in ('海运费USD','价格USD')):
        findings.append('正文出现双档金额，结构化价格全部为空')
    if len(rows)==1 and ('mediaId=' in text or p.get('图片名')):
        findings.append('带图消息仅一条记录：核对是否遗漏原图行列')
    if any(r['kind']=='space' and not any((r.get('payload') or {}).get(k) for k in ('柜型','箱量','船名航次')) for r in rows):
        findings.append('舱位核心业务明细全空：核对是否用摘要替代展开')
    if re.search(r'(?:ATD|Week|有效|到|至).*?\d{1,2}[./]\d{1,2}',text,re.I) and any((r.get('payload') or {}).get('AI判断')=='是' for r in rows):
        findings.append('原文有日期线索且使用AI兜底：核对日期范围')
    return findings

def check_report(report, before_rows, after_rows):
    if report.get('outcome') not in OUTCOMES:raise ValueError('outcome must be reviewed/repaired/needs_human')
    if len(str(report.get('note') or '').strip())<MIN_NOTE_LENGTH:raise ValueError('review conclusion is required')
    if not isinstance(report.get('evidence'),list) or not report['evidence']:raise ValueError('read evidence paths are required')
    old={r['id']:r for r in before_rows}; new={r['id']:r for r in after_rows}
    if any(r.get('review_status')!='awaiting' or r.get('origin')!='agent' for r in after_rows):
        raise ValueError('source no longer exclusively unreviewed Agent rows; refresh and leave human changes alone')
    outcome=report['outcome']
    if not set(old)<=set(new):raise ValueError('original pending IDs disappeared; cannot complete automatic repair')
    changed=fingerprint(before_rows)!=fingerprint(after_rows)
    if outcome=='reviewed' and changed:raise ValueError('source changed during review; cannot mark unchanged review complete')
    if outcome=='repaired':
        if not changed:raise ValueError('repaired requires a real pending change')
        if any(r.get('check_status')!='passed' for r in after_rows):raise ValueError('repair still fails: use needs_human, never retry')
        if any(r.get('check_status')!='passed' for r in before_rows):raise ValueError('failed submission cannot start another automatic repair round')
        # An existing source identity may not silently shift to a different source.
        for rid,previous in old.items():
            for field in ('消息ID','来源群','消息原文','消息时间','发送人'):
                if previous['payload'].get(field)!=new[rid]['payload'].get(field):raise ValueError('source provenance changed')
    if outcome=='reviewed' and any(r.get('check_status')!='passed' for r in before_rows):
        raise ValueError('failed rows require needs_human')
    return outcome
