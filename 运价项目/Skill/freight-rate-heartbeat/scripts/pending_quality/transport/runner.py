"""Read pending data and evidence; write only local review state, never freight rows."""
import hashlib
import json
import logging
import re
from pathlib import Path
from urllib.parse import urlencode
import ledger_client
import paths
import spec_index
from pending_quality.core.contracts import PAGE_SIZE, MAX_PAGES
from pending_quality.services.review import group_rows, check_report
from pending_quality.transport.store import Store
LOG=logging.getLogger('pending_quality')
PROJECT=Path(paths.PROJECT_ROOT)
RUNTIME=Path(paths.WORK)/'pending-quality'

def fetch_rows(message_id=None):
    rows=[];offset=0;expected_total=None;seen=set()
    for _ in range(MAX_PAGES):
        query={'status':'awaiting','view':'entries','limit':PAGE_SIZE,'offset':offset}
        if message_id:query['message_id']=message_id
        data=ledger_client._request('GET','/api/pending?'+urlencode(query))
        if not data.get('ok'):raise RuntimeError('pending read failed: '+str(data.get('error')))
        total=data.get('total')
        if not isinstance(total,int):raise RuntimeError('pending total is missing')
        if expected_total is None:expected_total=total
        elif total!=expected_total:raise RuntimeError('pending changed during pagination; retry next run')
        batch=data.get('rows') or []
        ids=[r['id'] for r in batch]
        if len(set(ids))!=len(ids) or seen.intersection(ids):raise RuntimeError('pending page overlap; refusing incomplete source')
        seen.update(ids);rows.extend(batch);offset+=len(batch)
        if offset==expected_total:return rows
        if offset>expected_total:raise RuntimeError('pending page exceeded total')
        if not batch:raise RuntimeError('pending pagination ended before total; refusing incomplete source')
    raise RuntimeError('pending pagination exceeded bound; refusing incomplete source')

def monitor(activity,group='',message_id=''):
    # Monitoring is optional operational IO; failures are visible and do not write the ledger.
    try:
        import sys
        sys.path.insert(0,str(PROJECT/'ops/agent-monitor'))
        from agent_watch.transport.client import report, ensure_server
        if not ensure_server():
            LOG.warning("progress monitor could not wake")
            return
        if not report(stage='BUSINESS_REVIEW',activity=activity,group=group,message_id=message_id):
            LOG.warning('progress report unavailable')
    except Exception as exc:LOG.warning('progress report failed: %s',exc)

def evidence(item):
    config=next((g for g in paths.chat_groups() if g.get('name')==item['group']),None)
    knowledge=[PROJECT/'Database/docs/agent-input-contract.md',PROJECT/'Database/docs/standardization-review-spec.md',Path(paths.PKG_ROOT)/'references/extraction-rules.md',Path(paths.PKG_ROOT)/'references/learnings.md']
    if config:
        info=spec_index.brief(config['slug'])
        for key in ('spec_path','spec_base_path','spec_knowledge_path'):
            if info.get(key):knowledge.append(Path(info[key]))
    records=item['rows'];source=str((records[0].get('payload') or {}).get('消息原文') or '')
    primary=source.split('——引用',1)[0]
    media=list(dict.fromkeys(re.findall(r'mediaId=(@|\$)?([A-Za-z0-9_-]+)',primary)))
    media=[prefix+value if prefix=='$' else value for prefix,value in media]
    if not media:
        media=[m.lstrip('@') for m in str((records[0].get('payload') or {}).get('图片名') or '').split(',') if re.fullmatch(r'[@$]?[A-Za-z0-9_-]+',m)]
    images=[];caches=[];missing=[]
    image_dir=PROJECT/'Database/images'
    for mid in media:
        found=sorted(image_dir.glob(mid+'*'))
        found=[p for p in found if p.is_file() and p.suffix.lower() in ('.png','.jpg','.jpeg','.webp')]
        if found:images.extend(found)
        else:missing.append('original image: '+mid)
        cache=Path(paths.WORK)/'ocr_cache'/(mid+'.json')
        if cache.is_file():caches.append(cache)
    hashes={}
    for p in knowledge+images+caches:
        if not p.is_file():missing.append(str(p));continue
        hashes[str(p.resolve())]=hashlib.sha256(p.read_bytes()).hexdigest()
    if not config:missing.append('group knowledge route: '+item['group'])
    if not item['message_id'] or not primary.strip():missing.append('source message identity/body')
    failed=any(r.get('check_status')!='passed' for r in records)
    repaired=any('agent' in str(r.get('edit_log') or '').lower() for r in records)
    protected_edits=any(r.get('edit_log') for r in records)
    return {'source':item,'source_text':source,'knowledge_files':[str(p.resolve()) for p in knowledge],
            'original_images':[str(p.resolve()) for p in images],'ocr_files':[str(p.resolve()) for p in caches],
            'file_hashes':hashes,'missing_evidence':missing,
            'automatic_repair_allowed':not missing and not failed and not protected_edits,
            'existing_edits_protected':protected_edits,
            'repair_boundary':'only unreviewed Agent pending rows; no failed-row retry, approval, deletion, formal record writes or ungrounded aliases',
            'one_round_already_used':repaired or failed}

def next_batch(limit):
    store=Store(RUNTIME/'reviews.sqlite3')
    try:
        store.sync(group_rows(fetch_rows()))
        batch=store.claim(limit);out=[]
        for item in batch:
            packet=evidence(item)
            packet_path=RUNTIME/'evidence'/((item['key']+'-'+item['fingerprint'])+'.json')
            packet_path.parent.mkdir(parents=True,exist_ok=True)
            packet_path.write_text(json.dumps(packet,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
            monitor('领取消息组，核对原文、原图与群知识',item['group'],item['message_id'])
            out.append({'claim':item['claim'],'message_id':item['message_id'],'group':item['group'],
                        'rows':len(item['rows']),'signals':item['signals'],'evidence_file':str(packet_path.resolve()),
                        'automatic_repair_allowed':packet['automatic_repair_allowed']})
        return {'items':out,'queue':store.status()}
    finally:store.close()

def refresh(token):
    store=Store(RUNTIME/'reviews.sqlite3')
    try:
        item=store.get_claim(token)
        groups=group_rows(fetch_rows(item['message_id'] or None))
        current=next((g for g in groups if g['key']==item['key']),None)
        if not current or current['fingerprint']!=item['fingerprint']:
            raise ValueError('source changed; do not refresh evidence over concurrent business changes')
        packet=evidence(item)
        packet_path=RUNTIME/'evidence'/((item['key']+'-'+item['fingerprint'])+'.json')
        packet_path.write_text(json.dumps(packet,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
        return {'evidence_file':str(packet_path.resolve()),'automatic_repair_allowed':packet['automatic_repair_allowed'],'missing_evidence':packet['missing_evidence']}
    finally:store.close()

def finish(token,report_path):
    store=Store(RUNTIME/'reviews.sqlite3')
    try:
        before=store.get_claim(token)
        report_path=Path(report_path).resolve()
        if not report_path.is_relative_to(PROJECT.resolve()):raise ValueError('review report must be inside this project')
        report=json.loads(report_path.read_text(encoding='utf-8'))
        packet_path=RUNTIME/'evidence'/((before['key']+'-'+before['fingerprint'])+'.json')
        packet=json.loads(packet_path.read_text(encoding='utf-8'))
        if report.get('outcome') in ('reviewed','repaired'):
            if packet['missing_evidence']:raise ValueError('missing source/knowledge evidence; use needs_human')
            if report['outcome']=='repaired' and not packet['automatic_repair_allowed']:
                raise ValueError('another automatic repair is not permitted; use needs_human')
            required=set(packet['knowledge_files']+packet['original_images'])
            supplied={str(Path(p).resolve()) for p in report.get('evidence',[])}
            if not required<=supplied:raise ValueError('completion requires all routed knowledge and original-image evidence')
            for name,digest in packet['file_hashes'].items():
                if hashlib.sha256(Path(name).read_bytes()).hexdigest()!=digest:
                    raise ValueError('evidence changed during review; do not claim completion from old evidence')
        if before['message_id']:
            candidates=group_rows(fetch_rows(before['message_id']))
        else:candidates=group_rows(fetch_rows())
        after=next((i for i in candidates if i['key']==before['key']),None)
        if not after:raise ValueError('source no longer awaiting; preserve human changes and refresh')
        check_report(report,before['rows'],after['rows'])
        store.finish(token,after,report)
        monitor('业务复核结束：'+report['outcome'],after['group'],after['message_id'])
        return {'message_id':after['message_id'],'outcome':report['outcome'],'rows':len(after['rows']),'queue':store.status()}
    finally:store.close()

def status():
    store=Store(RUNTIME/'reviews.sqlite3')
    try:return {'queue':store.status()}
    finally:store.close()
