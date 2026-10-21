"""Multi-LOCODE columns must validate and stage every member."""
import json,sqlite3,unittest
import manualdb

def verify_locode_lists(test,conn):
    for name,code in (('Locode Origin','CNORI'),('Locode Port One','BRAAA'),('Locode Port Two','BRBBB'),('Locode Port Three','BRCCC')):
        conn.execute('INSERT INTO ports(name,aliases,active) VALUES(?,?,1)',(name,code))
    conn.execute('INSERT INTO carriers(code,active) VALUES(?,1)',('LOCODETEST',));conn.commit()
    master=manualdb.Master(conn)
    quote={'起运港':'CNORI','目的港':'BRAAA, BRBBB,BRCCC','船司':'LOCODETEST','柜型':'20GP','海运费USD':100,
           '有效期起':'2026-10-01','有效期止':'2026-10-14','消息ID':'locode-list-fixture','来源群':'fixture','消息原文':'isolated locode quote'}
    validated=manualdb.validate_record(conn,master,'rates',quote)
    test.assertTrue(validated['ok'],validated)
    test.assertEqual(validated['resolved']['目的港'],'Locode Port One/Locode Port Two/Locode Port Three')
    alternate=dict(quote,航线服务='COCTG',海运费USD=90)
    ids=manualdb.stage_pending(conn,master,'rates',[quote,alternate],auto_suggest=False,preserve_other_sources=True)
    test.assertEqual(len(ids),6)
    rows=manualdb.list_pending(conn,message_id=quote['消息ID'])['rows'];test.assertEqual(len(rows),6)
    test.assertTrue(all(r['check_status']=='passed' for r in rows))
    test.assertEqual(len({(r['payload']['目的港'],r['payload'].get('航线服务','')) for r in rows}),6)
    unknown=dict(quote,目的港='BRAAA,BRZZZ')
    validation=manualdb.validate_record(conn,master,'rates',unknown)
    test.assertFalse(validation['ok']);test.assertTrue(any(e['input']=='BRZZZ' for e in validation['errors']))

class LocodeListsTests(unittest.TestCase):
    def test_only_locode_lists_split_on_comma(self):
        self.assertEqual(manualdb._split_port('BRPEC,BRSUA,BRSSA'),['BRPEC','BRSUA','BRSSA'])
        self.assertEqual(manualdb._split_port('BRPEC，BRSUA/BRSSA'),['BRPEC','BRSUA','BRSSA'])
        self.assertEqual(manualdb._split_port('Manzanillo,MX'),['Manzanillo,MX'])
        self.assertEqual(manualdb._split_port('Santos,BR'),['Santos,BR'])
    def test_sqlite_multi_port_and_services(self):
        conn=sqlite3.connect(':memory:');conn.row_factory=sqlite3.Row
        try:manualdb.init_schema(conn);verify_locode_lists(self,conn)
        finally:conn.close();manualdb.invalidate_master()

if __name__=='__main__':unittest.main()
