import copy,json,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
import manualdb
from core.contracts import AGENT_MASTER_ENSURE_ROUTE,AGENT_PENDING_REPAIR_ROUTE
from services.agent_repair import payload_signature
from transport.access_rules import authorize
from test_port_routes import seed,quote,POL,PORT_A,LANE_A,CARRIER

GROUP='Agent Repair Fixture'
EVIDENCE=[{'kind':'group_spec','reference':'fixture/规范.md#缩写表','reason':'isolated verified abbreviation'}]
OFFICIAL=[{'kind':'official_source','reference':'https://official.example/ports/fixture','reason':'isolated official-source contract fixture'}]

class SpaceEvidenceGateTests(unittest.TestCase):
    def test_message_terms_do_not_cross_carrier_destination_or_sailing(self):
        original = {'消息ID':'terms', '船司':'CMA', '目的港':'Piraeus', '柜型':'20GP', '目免':14}
        same = dict(original, 柜型='40HQ/HC', 目免='')
        others = [dict(same, 船司='MSK'), dict(same, 目的港='Koper'),
                  dict(same, 船期ETD='2026-10-14')]
        manualdb.inherit_message_fields([original, same, *others])
        self.assertEqual(str(same['目免']), '14')
        self.assertTrue(all(row['目免'] == '' for row in others))

    def test_empty_current_space_is_not_validated_for_release(self):
        class Master:
            def route_candidates(self, pod):
                return []
        with patch.object(manualdb, 'resolve_core', side_effect=lambda *_: ({}, [])), patch.object(
                manualdb, 'name_resolution_shadow',
                return_value={'stage':'shadow','equal':True,'fields':[]}):
            empty = {'舱位类型': '现舱', '船司': 'HPL', '价格USD': 3850}
            result = manualdb.validate_record(None, Master(), 'space', empty)
            self.assertFalse(result['ok'])
            self.assertTrue(any(e['field'] == '现舱明细' for e in result['errors']))
            self.assertTrue(manualdb.validate_record(None, Master(), 'space',
                            dict(empty, 目的港='Istanbul'))['ok'])
            self.assertTrue(manualdb.validate_record(None, Master(), 'space',
                            dict(empty, 航线='地中海'))['ok'])
            self.assertTrue(manualdb.validate_record(None, Master(), 'space',
                            dict(empty, 航线服务='WSA'))['ok'])
            self.assertFalse(manualdb.validate_record(None, Master(), 'space',
                             {'舱位类型': '售罄'})['ok'])
            self.assertTrue(manualdb.validate_record(None, Master(), 'space',
                            {'舱位类型': '售罄', '航线': '地中海'})['ok'])

    def test_space_vessel_without_destination_scope_is_blocked(self):
        class Master:
            def route_candidates(self, pod):
                return []
        with patch.object(manualdb, 'resolve_core', side_effect=lambda *_: ({}, [])), patch.object(
                manualdb, 'name_resolution_shadow',
                return_value={'stage':'shadow','equal':True,'fields':[]}):
            result = manualdb.validate_record(None, Master(), 'space',
                                              {'船名航次': 'YM WINDOW / 048W'})
        self.assertFalse(result['ok'])
        self.assertTrue(any(e['field'] == '目的范围' for e in result['errors']))

    def test_loss_fee_cannot_be_a_space_price(self):
        class Master:
            def route_candidates(self, pod):
                return []
        record = {'舱位类型': '现舱', '目的港': 'Gdansk',
                  '消息原文': 'MSK现舱 亏舱USD100/柜 NB-GDANSK 2975', '价格USD': 100}
        with patch.object(manualdb, 'resolve_core', side_effect=lambda *_: ({}, [])), patch.object(
                manualdb, 'name_resolution_shadow',
                return_value={'stage':'shadow','equal':True,'fields':[]}):
            result = manualdb.validate_record(None, Master(), 'space', record)
            self.assertFalse(result['ok'])
            self.assertTrue(any(e['field'] == '价格USD' for e in result['errors']))
            self.assertTrue(manualdb.validate_record(None, Master(), 'space',
                            dict(record, 价格USD=2975))['ok'])

    def test_stale_passed_placeholder_cannot_be_approved(self):
        with tempfile.TemporaryDirectory() as folder:
            conn = manualdb.connect(Path(folder) / 'isolated.db')
            try:
                seed(conn)
                master = manualdb.get_master(conn)
                record = {'起运港': POL, '船司': CARRIER, '舱位类型': '现舱',
                          '有效期起': '2026-10-07', '消息原文': 'isolated empty placeholder',
                          '消息ID': 'placeholder-fixture', '来源群': GROUP}
                pid = manualdb.stage_pending(conn, master, 'space', [record], auto_suggest=False)[0]
                conn.execute("UPDATE pending_records SET check_status='passed', errors='[]' WHERE id=?", (pid,))
                conn.commit()
                result = manualdb.approve_pending(conn, master, pid)
                self.assertFalse(result['ok'])
                self.assertTrue(any(e['field'] == '现舱明细' for e in result['errors']))
                row = conn.execute('SELECT review_status,check_status FROM pending_records WHERE id=?', (pid,)).fetchone()
                self.assertEqual((row['review_status'], row['check_status']), ('awaiting', 'failed'))
                self.assertEqual(conn.execute('SELECT count(*) FROM freight_items').fetchone()[0], 0)
            finally:
                conn.close()
                manualdb.invalidate_master()

def verify_agent_repair(case,conn):
    manualdb.invalidate_master();seed(conn)
    conn.execute('INSERT INTO chat_groups(name) VALUES(?) ON CONFLICT(name) DO NOTHING',(GROUP,));conn.commit()
    body={'entity':'alias','source_group':GROUP,'message_id':'fixture','row':{'alias':'ORIGIN_SHORT','kind':'pol','target':POL},'evidence':EVIDENCE}
    result=manualdb.agent_master_ensure(conn,body);case.assertTrue(result['ok'])
    master=manualdb.get_master(conn);case.assertEqual(master.lookup_port('ORIGIN_SHORT','pol',GROUP),(POL,True));case.assertFalse(master.lookup_port('ORIGIN_SHORT','pol','Another Group')[1])
    case.assertEqual(manualdb.agent_master_ensure(conn,body)['data']['action'],'existing')
    bad=copy.deepcopy(body);bad['row']['target']=PORT_A
    with case.assertRaises(ValueError):manualdb.agent_master_ensure(conn,bad)
    conn.rollback()
    unknown=copy.deepcopy(body);unknown['row']={'alias':'UNKNOWN_ROUTE','kind':'route','target':'Not a real route'}
    with case.assertRaises(ValueError):manualdb.agent_master_ensure(conn,unknown)
    conn.rollback()
    port={'entity':'port','source_group':GROUP,'message_id':'fixture','row':{'name':'Agent Verified Fixture Port','country':'GE','kind':'pod','routes_list':[LANE_A]},'evidence':OFFICIAL}
    case.assertTrue(manualdb.agent_master_ensure(conn,port)['ok'])
    count=conn.execute('SELECT count(*) FROM ports WHERE name=?',(port['row']['name'],)).fetchone()[0]
    case.assertEqual(count,1);case.assertEqual(manualdb.agent_master_ensure(conn,port)['data']['action'],'existing')
    unsafe=copy.deepcopy(port);unsafe['row']['country']='TR'
    with case.assertRaises(ValueError):manualdb.agent_master_ensure(conn,unsafe)
    conn.rollback()
    no_evidence=copy.deepcopy(port);no_evidence['evidence']=EVIDENCE
    with case.assertRaises(ValueError):manualdb.agent_master_ensure(conn,no_evidence)
    conn.rollback()
    rec=dict(quote(),**{'起运港':'MISSING_ORIGIN','消息ID':'fixture','来源群':GROUP,'船期ETD':'2026-09-29'})
    pid=manualdb.stage_pending(conn,manualdb.get_master(conn),'rates',[rec],auto_suggest=False)[0]
    before=dict(conn.execute('SELECT * FROM pending_records WHERE id=?',(pid,)).fetchone())
    payload=json.loads(before['payload'])
    request={'id':pid,'expected_signature':payload_signature(payload),'fields':{'起运港':POL,'船期ETD':''},'evidence':EVIDENCE,'_actor':'agent'}
    case.assertTrue(manualdb.agent_pending_repair(conn,request)['ok'])
    after=dict(conn.execute('SELECT * FROM pending_records WHERE id=?',(pid,)).fetchone())
    case.assertEqual(after['entry_id'],before['entry_id']);case.assertEqual(after['orig_payload'],before['orig_payload']);case.assertEqual(after['review_status'],'awaiting');case.assertEqual(after['check_status'],'passed');case.assertTrue(after['edit_log'])
    case.assertEqual(json.loads(after['payload'])['船期ETD'],'')
    case.assertTrue(any(e['field']=='船期ETD' and e['old']=='2026-09-29' and e['new']=='' for e in json.loads(after['edit_log'])))
    with case.assertRaises(ValueError):manualdb.agent_pending_repair(conn,request)
    conn.rollback()
    current=json.loads(after['payload']);unsafe=dict(request,expected_signature=payload_signature(current),fields={'消息原文':'alter provenance'})
    with case.assertRaises(ValueError):manualdb.agent_pending_repair(conn,unsafe)
    conn.rollback()
    conn.execute("UPDATE pending_records SET review_status='rejected' WHERE id=?",(pid,));conn.commit()
    unsafe=dict(request,expected_signature=payload_signature(current))
    with case.assertRaises(ValueError):manualdb.agent_pending_repair(conn,unsafe)
    conn.rollback()
    case.assertEqual(conn.execute('SELECT count(*) FROM freight_items').fetchone()[0],0)

def verify_source_scoped_pending(case, conn):
    manualdb.invalidate_master(); seed(conn)
    master=manualdb.get_master(conn)
    other=dict(quote(), **{'来源群':GROUP,'消息ID':'other-source','消息原文':'other original'})
    old_id=manualdb.stage_pending(conn,master,'rates',[other],auto_suggest=False)[0]
    before=dict(conn.execute('SELECT * FROM pending_records WHERE id=?',(old_id,)).fetchone())
    target=dict(other, **{'消息ID':'target-source','消息原文':'target original','海运费USD':5600})
    pid=manualdb.stage_pending(conn,master,'rates',[target],auto_suggest=False,preserve_other_sources=True)[0]
    case.assertEqual(dict(conn.execute('SELECT * FROM pending_records WHERE id=?',(old_id,)).fetchone()),before)
    changed=dict(target, **{'海运费USD':5700})
    new_id=manualdb.stage_pending(conn,master,'rates',[changed],auto_suggest=False,preserve_other_sources=True)[0]
    case.assertNotEqual(pid,new_id)
    case.assertIsNone(conn.execute('SELECT id FROM pending_records WHERE id=?',(pid,)).fetchone())
    case.assertEqual(dict(conn.execute('SELECT * FROM pending_records WHERE id=?',(old_id,)).fetchone()),before)
    conn.execute('UPDATE pending_records SET warnings=? WHERE id=?',
                 (json.dumps([{'field':'港口覆盖率','msg':'stale'},{'field':'other','msg':'keep'}]),new_id))
    conn.commit()
    extra=dict(target, **{'柜型':'40NOR'})
    manualdb.stage_pending(conn,master,'rates',[extra],auto_suggest=False,preserve_other_sources=True)
    warnings=json.loads(conn.execute('SELECT warnings FROM pending_records WHERE id=?',(new_id,)).fetchone()[0])
    case.assertEqual(warnings,[{'field':'other','msg':'keep'}])
    snapshot=[dict(r) for r in conn.execute('SELECT * FROM pending_records ORDER BY id')]
    for records,flag in (([target,dict(other,来源群='')],True),([dict(target,消息ID='')],True),([target],'true')):
        with case.assertRaises(ValueError):
            manualdb.stage_pending(conn,master,'rates',records,auto_suggest=False,preserve_other_sources=flag)
        conn.rollback()
        case.assertEqual([dict(r) for r in conn.execute('SELECT * FROM pending_records ORDER BY id')],snapshot)
    batch=[dict(target,消息ID='batch-a'),dict(target,消息ID='batch-b')]
    batch_ids=manualdb.stage_pending(conn,master,'rates',batch,auto_suggest=False,preserve_other_sources=True)
    case.assertEqual(len(batch_ids),2)
    case.assertEqual(dict(conn.execute('SELECT * FROM pending_records WHERE id=?',(old_id,)).fetchone()),before)

    for index, reverse in enumerate((False, True)):
        interval = dict(target, 消息ID='interval-fixture-'+str(index),
                        有效期起='2026-10-08', 有效期止='2026-10-31', 船期ETD='', 海运费USD=3000)
        sailing = dict(interval, 船期ETD='2026-10-14', 海运费USD=2900)
        ordered = [sailing, interval] if reverse else [interval, sailing]
        retained = manualdb.stage_pending(conn,master,'rates',ordered,auto_suggest=False,
                                         preserve_other_sources=True)
        case.assertEqual(len(retained),2)
        rows = [json.loads(conn.execute('SELECT payload FROM pending_records WHERE id=?',(pid,)).fetchone()[0])
                for pid in retained]
        case.assertEqual({str(row['海运费USD']) for row in rows},{'2900','3000'})
        conflict = dict(interval, 消息ID='conflicting-fixture-'+str(index), 海运费USD=999)
        retained = manualdb.stage_pending(conn,master,'rates',
                                         [dict(conflict, 海运费USD=3000),conflict],
                                         auto_suggest=False,preserve_other_sources=True)
        case.assertEqual(len(retained),1)

    # Same-source additive repair must preserve the audited row byte-for-byte and
    # insert only the missing product.  This is the contract used after a row repair.
    additive=dict(other, **{'消息ID':'additive-source','消息原文':'additive original','海运费USD':5800})
    repaired_id=manualdb.stage_pending(conn,master,'rates',[additive],auto_suggest=False,
                                      preserve_other_sources=True)[0]
    conn.execute('UPDATE pending_records SET edit_log=? WHERE id=?',
                 (json.dumps([{'action':'update','field':'目的港','old':'bad','new':PORT_A}]),repaired_id))
    conn.commit()
    repaired_before=dict(conn.execute('SELECT * FROM pending_records WHERE id=?',(repaired_id,)).fetchone())
    missing=dict(additive, **{'柜型':'40NOR','海运费USD':5900})
    preserved=[]
    added=manualdb.stage_pending(conn,master,'rates',[additive,missing],auto_suggest=False,
                                 preserve_other_sources=True,preserve_existing_source_rows=True,
                                 preserved_ids=preserved)
    case.assertEqual(preserved,[repaired_id])
    case.assertEqual(len(added),1)
    case.assertEqual(dict(conn.execute('SELECT * FROM pending_records WHERE id=?',(repaired_id,)).fetchone()),repaired_before)
    additive_rows=list(conn.execute("SELECT id,payload FROM pending_records WHERE json_extract(payload,'$.消息ID')='additive-source' ORDER BY id"))
    case.assertEqual(len(additive_rows),2)
    case.assertEqual({json.loads(row['payload'])['柜型'] for row in additive_rows},{additive['柜型'],'40NOR'})
    with case.assertRaises(ValueError):
        manualdb.stage_pending(conn,master,'rates',[missing],auto_suggest=False,
                               preserve_existing_source_rows=True)
    conn.rollback()
    with case.assertRaises(ValueError):
        manualdb.stage_pending(conn,master,'rates',[missing],auto_suggest=False,
                               preserve_other_sources=True,preserve_existing_source_rows='true')
    conn.rollback()


class AgentRepairTests(unittest.TestCase):
    def test_canonical_port_is_not_redirected_by_scoped_alias(self):
        with tempfile.TemporaryDirectory() as folder:
            conn = manualdb.connect(Path(folder) / 'isolated.db')
            try:
                seed(conn)
                conn.execute('INSERT INTO alias_scope(alias,kind,scope_group,target) VALUES(?,?,?,?)',
                             (PORT_A,'pod',GROUP,POL))
                conn.commit()
                manualdb.invalidate_master()
                master = manualdb.get_master(conn)
                self.assertEqual(master.lookup_port(PORT_A,'pod',GROUP), (PORT_A,True))
                self.assertEqual(manualdb.resolve_core(master,'rates',dict(quote(),来源群=GROUP))[0]['目的港'],PORT_A)
            finally:
                conn.close()
                manualdb.invalidate_master()

    def test_canonical_port_with_comma_is_one_destination(self):
        with tempfile.TemporaryDirectory() as folder:
            conn = manualdb.connect(Path(folder) / 'isolated.db')
            try:
                seed(conn)
                canonical = 'Fixture Freeport, Island'
                conn.execute('INSERT INTO ports(name,kind,active) VALUES(?,?,1)', (canonical,'both'))
                conn.execute('INSERT INTO alias_scope(alias,kind,scope_group,target) VALUES(?,?,?,?)', ('COMMA_ALIAS','pod',GROUP,canonical))
                conn.commit()
                manualdb.invalidate_master()
                master = manualdb.get_master(conn)
                rows = master.expand_cn(dict(quote(), **{'来源群':GROUP,'目的港':'COMMA_ALIAS'}))
                self.assertEqual(len(rows), 1)
                self.assertEqual(rows[0]['目的港'], canonical)
                ids = manualdb.stage_pending(conn, master, 'rates', [dict(quote(), **{'来源群':GROUP,'目的港':'COMMA_ALIAS','消息ID':'comma-fixture'})], auto_suggest=False)
                self.assertEqual(len(ids), 1)
            finally:
                conn.close()
                manualdb.invalidate_master()

    def test_expansion_preserves_inland_marker_and_does_not_pass_as_seaport(self):
        with tempfile.TemporaryDirectory() as folder:
            conn = manualdb.connect(Path(folder) / 'isolated.db')
            try:
                seed(conn)
                master = manualdb.get_master(conn)
                record = dict(quote(), **{'目的港': PORT_A + ' CY', '消息ID':'inland-marker'})
                expanded = master.expand_cn(record)
                self.assertEqual(expanded[0]['目的港'], PORT_A + ' CY')
                ids = manualdb.stage_pending(conn, master, 'rates', [record], auto_suggest=False)
                row = conn.execute('SELECT payload,check_status,errors FROM pending_records WHERE id=?', (ids[0],)).fetchone()
                self.assertEqual(json.loads(row['payload'])['目的港'], PORT_A + ' CY')
                self.assertEqual(row['check_status'], 'failed')
                self.assertTrue(any('内陆' in e['msg'] for e in json.loads(row['errors'])))
            finally:
                conn.close()
                manualdb.invalidate_master()

    def test_new_alias_does_not_duplicate_or_break_retained_source_receipt(self):
        import manual_server
        from services.submission_coverage import fingerprint
        with tempfile.TemporaryDirectory() as folder:
            db = Path(folder)/'isolated.db'
            conn = manualdb.connect(db)
            seed(conn)
            conn.execute('INSERT INTO chat_groups(name) VALUES(?) ON CONFLICT(name) DO NOTHING',(GROUP,))
            conn.commit()
            conn.close()
            manualdb.invalidate_master()
            original = dict(manual_server.CFG)
            manual_server.CFG.update({'db':str(db),'backend':'sqlite','postgres_env_file':''})
            try:
                record = dict(quote(), **{'来源群':GROUP,'消息ID':'alias-source','消息原文':'original','目的港':'IMAGE_ALIAS'})
                first = manual_server.act_pending({'table':'rates','records':[record]})
                conn = manualdb.connect(db)
                manualdb.agent_master_ensure(conn, {'entity':'alias','source_group':GROUP,'message_id':'alias-source',
                    'row':{'alias':'IMAGE_ALIAS','kind':'pod','target':PORT_A},'evidence':EVIDENCE})
                conn.close()
                result = manual_server.act_pending({'table':'rates','records':[record],
                    'preserve_other_sources':True,'preserve_existing_source_rows':True})
                self.assertTrue(result['coverage']['ok'], result['coverage'])
                self.assertEqual(result['covered_ids'],first['ids'])
                self.assertEqual(result['staged'],0)
                conn = manualdb.connect(db)
                payload = json.loads(conn.execute('SELECT payload FROM pending_records WHERE id=?',(first['ids'][0],)).fetchone()[0])
                self.assertEqual(payload['目的港'],'IMAGE_ALIAS')
                self.assertEqual(result['coverage']['entries'][0]['fingerprint'],fingerprint('rates',payload))
                conn.close()
            finally:
                manual_server.CFG.clear()
                manual_server.CFG.update(original)
                manualdb.invalidate_master()
    def test_image_source_correction_can_remain_failed_without_faking_a_pass(self):
        with tempfile.TemporaryDirectory() as folder:
            conn = manualdb.connect(Path(folder)/'isolated.db')
            try:
                seed(conn)
                manualdb.invalidate_master()
                record = dict(quote(), **{'目的港':'DOCA6','图片名':'@original'})
                pid = manualdb.stage_pending(conn,manualdb.get_master(conn),'rates',[record],auto_suggest=False)[0]
                row = conn.execute('SELECT payload FROM pending_records WHERE id=?',(pid,)).fetchone()
                body = {'id':pid,'expected_signature':payload_signature(json.loads(row['payload'])),
                        'fields':{'目的港':'DOZA6'},'retain_failed':True,
                        'evidence':[{'kind':'original_image','reference':'@original','reason':'actual code on source image'}]}
                result = manualdb.agent_pending_repair(conn,body)
                self.assertTrue(result['ok'])
                self.assertEqual(result['data']['check_status'],'failed')
                changed = conn.execute('SELECT payload,edit_log,review_status FROM pending_records WHERE id=?',(pid,)).fetchone()
                self.assertEqual(json.loads(changed['payload'])['目的港'],'DOZA6')
                self.assertTrue(changed['edit_log'])
                self.assertEqual(changed['review_status'],'awaiting')
                self.assertFalse(manualdb.approve_pending(conn,manualdb.get_master(conn),pid)['ok'])
                body['expected_signature'] = payload_signature(json.loads(changed['payload']))
                with self.assertRaises(ValueError):
                    manualdb.agent_pending_repair(conn,body)
            finally:
                conn.close()
                manualdb.invalidate_master()

    def test_text_source_correction_can_use_group_rule_without_image(self):
        with tempfile.TemporaryDirectory() as folder:
            conn = manualdb.connect(Path(folder)/'isolated.db')
            try:
                seed(conn)
                manualdb.invalidate_master()
                record = dict(quote(), **{'目的港':'', '消息ID':'text-source', '消息原文':'基本点报价'})
                pid = manualdb.stage_pending(conn, manualdb.get_master(conn), 'rates', [record], auto_suggest=False)[0]
                row = conn.execute('SELECT payload FROM pending_records WHERE id=?', (pid,)).fetchone()
                body = {
                    'id': pid,
                    'expected_signature': payload_signature(json.loads(row['payload'])),
                    'fields': {'目的港': 'Santos'},
                    'retain_failed': True,
                    'evidence': [{'kind': 'group_spec', 'reference': 'rules#basic-points', 'reason': 'text source rule'}],
                }
                result = manualdb.agent_pending_repair(conn, body)
                self.assertTrue(result['ok'])
                self.assertEqual(result['data']['check_status'], 'failed')
            finally:
                conn.close()
                manualdb.invalidate_master()
    def test_additive_recovery_keeps_distinct_source_prices_and_cutoffs(self):
        with tempfile.TemporaryDirectory() as folder:
            conn = manualdb.connect(Path(folder)/'isolated.db')
            try:
                seed(conn)
                manualdb.invalidate_master()
                master = manualdb.get_master(conn)
                base = dict(quote(), **{'来源群':GROUP,'消息ID':'distinct-source','消息原文':'table original'})
                records = [dict(base, **{'海运费USD':price,'备注':note}) for price,note in
                           ((6100,'SI 9.20'),(6200,'SI 9.22'),(6200,'SI 9.23'))]
                ids = manualdb.stage_pending(conn,master,'rates',records,auto_suggest=False,
                    preserve_other_sources=True,preserve_existing_source_rows=True)
                self.assertEqual(len(ids),3)
                preserved = []
                self.assertEqual(manualdb.stage_pending(conn,master,'rates',records,auto_suggest=False,
                    preserve_other_sources=True,preserve_existing_source_rows=True,preserved_ids=preserved),[])
                self.assertEqual(set(preserved),set(ids))
            finally:
                conn.close()
                manualdb.invalidate_master()
    def test_pending_api_reports_inserted_and_preserved_rows_separately(self):
        import manual_server
        with tempfile.TemporaryDirectory() as folder:
            db=Path(folder)/'isolated.db'
            conn=manualdb.connect(db)
            try:seed(conn)
            finally:conn.close();manualdb.invalidate_master()
            original=dict(manual_server.CFG)
            manual_server.CFG.update({'db':str(db),'backend':'sqlite','postgres_env_file':''})
            try:
                base=dict(quote(), **{'来源群':GROUP,'消息ID':'api-additive','消息原文':'api additive','海运费USD':6100})
                first=manual_server.act_pending({'table':'rates','origin':'agent','records':[base]})
                conn=manualdb.connect(db)
                try:
                    conn.execute('UPDATE pending_records SET edit_log=? WHERE id=?',
                                 (json.dumps([{'action':'update','field':'目的港','old':'bad','new':PORT_A}]),first['ids'][0]))
                    conn.commit()
                finally:conn.close()
                missing=dict(base, **{'柜型':'40NOR','海运费USD':6200})
                result=manual_server.act_pending({'table':'rates','origin':'agent','records':[base,missing],
                    'preserve_other_sources':True,'preserve_existing_source_rows':True})
                self.assertEqual(result['staged'],1)
                self.assertEqual(result['preserved'],1)
                self.assertEqual(result['preserved_ids'],first['ids'])
                self.assertEqual(len(result['ids']),1)
                self.assertEqual(set(result['covered_ids']),set(first['ids']+result['ids']))
                self.assertTrue(result['coverage']['ok'],result['coverage'])
                conn=manualdb.connect(db)
                try:
                    old=conn.execute('SELECT edit_log FROM pending_records WHERE id=?',(first['ids'][0],)).fetchone()
                    self.assertTrue(old['edit_log'])
                    self.assertEqual(conn.execute("SELECT count(*) FROM pending_records WHERE json_extract(payload,'$.消息ID')='api-additive'").fetchone()[0],2)
                finally:conn.close()
            finally:
                manual_server.CFG.clear();manual_server.CFG.update(original);manualdb.invalidate_master()

    def test_source_scoped_pending_preserves_other_messages(self):
        with tempfile.TemporaryDirectory() as folder:
            conn=manualdb.connect(Path(folder)/'isolated.db')
            try:verify_source_scoped_pending(self,conn)
            finally:conn.close();manualdb.invalidate_master()
    def test_additive_master_and_audited_pending_repair(self):
        with tempfile.TemporaryDirectory() as folder:
            conn=manualdb.connect(Path(folder)/'isolated.db')
            try:verify_agent_repair(self,conn)
            finally:conn.close();manualdb.invalidate_master()
    def test_agent_receives_only_the_narrow_new_capabilities(self):
        for path in (AGENT_MASTER_ENSURE_ROUTE,AGENT_PENDING_REPAIR_ROUTE):
            self.assertTrue(authorize('POST',path,agent=True)['ok'])
            self.assertFalse(authorize('POST',path,role='sales')['ok'])
            self.assertFalse(authorize('POST',path,role='operator')['ok'])
        for path in ('/api/master/save','/api/alias/save','/api/pending/approve','/api/record/update'):
            self.assertFalse(authorize('POST',path,agent=True)['ok'])
    def test_knowledge_sync_is_idempotent_and_concurrent_appends_survive(self):
        from concurrent.futures import ThreadPoolExecutor
        from unittest.mock import patch
        import manual_server
        with tempfile.TemporaryDirectory() as folder:
            here=Path(folder)/"Database"
            spec=here.parent/"Skill/freight-rate-heartbeat/references/群聊规范"/GROUP/"规范.md"
            spec.parent.mkdir(parents=True);spec.write_text("# Fixture\n",encoding="utf-8")
            bodies=[{'entity':'alias','source_group':GROUP,'message_id':'fixture','row':{'alias':a,'target':POL,'kind':'pol'},'evidence':EVIDENCE} for a in ('ONE','TWO')]
            with patch.object(manual_server,'HERE',here),patch.object(manual_server,'_sync_spec_to_runtime',return_value=True):
                with ThreadPoolExecutor(max_workers=2) as executor:
                    result=list(executor.map(lambda b:manual_server._agent_master_knowledge({'data':{'action':'created'}},b),bodies))
                self.assertEqual(result,[True,True])
                self.assertTrue(manual_server._agent_master_knowledge({'data':{'action':'existing'}},bodies[0]))
            text=spec.read_text(encoding="utf-8")
            self.assertEqual(text.count('[Agent预检修复]'),2)
            self.assertIn('`ONE`',text);self.assertIn('`TWO`',text)

    def test_knowledge_uses_configured_group_directory(self):
        import manual_server
        with tempfile.TemporaryDirectory() as folder:
            here = Path(folder)/'Database'
            package = here.parent/'Skill/freight-rate-heartbeat'
            config = package/'scripts/config/chat_sources.json'
            config.parent.mkdir(parents=True)
            config.write_text(json.dumps({'groups':[{'name':GROUP+' / spaced','spec_dir':'references/群聊规范/configured-folder'}]}),encoding='utf-8')
            spec = package/'references/群聊规范/configured-folder/规范.md'
            spec.parent.mkdir(parents=True)
            spec.write_text('# Fixture\n',encoding='utf-8')
            body = {'entity':'alias','source_group':GROUP+' / spaced','message_id':'fixture',
                    'row':{'alias':'IMAGE_ALIAS','target':POL,'kind':'pol'},'evidence':EVIDENCE}
            with patch.object(manual_server,'HERE',here),patch.object(manual_server,'_sync_spec_to_runtime',return_value=True):
                self.assertTrue(manual_server._agent_master_knowledge({},body))
                self.assertEqual(manual_server._group_spec_path(body['source_group']),spec)
                self.assertIsNone(manual_server._group_spec_path('..\\..\\outside'))
            self.assertIn('`IMAGE_ALIAS`',spec.read_text(encoding='utf-8'))

if __name__=='__main__':unittest.main()
