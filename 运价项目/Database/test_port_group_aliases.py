"""Canonical group aliases keep old quotes searchable without rewriting rows."""
import json
import sqlite3
import unittest

import manualdb
from ops.merge_port_groups import merge, business_digest


def verify_group_aliases(test, connection):
    connection.execute("DELETE FROM port_groups WHERE name IN ('NEB','NE ports','Brazil North Ports')")
    ports = ('Itaqui','Pecém')
    for port in ports:
        connection.execute("INSERT INTO ports(name,active) VALUES(?,1) ON CONFLICT(name) DO NOTHING",(port,))
    for name in ('NEB','NE ports'):
        connection.execute("INSERT INTO port_groups(name,label,members,kind,active) VALUES(?,?,?,?,1)",
                           (name,'北巴（巴西东北）',','.join(ports),'pod'))
    for kind in ('rates','space'):
        for index,pod in enumerate(('NEB','NE ports','Itaqui')):
            key = f'group-alias-{kind}-{index}'
            connection.execute("INSERT INTO freight_items(entry_id,content_key,record_type,pol,pod,carrier,container_type,price_usd,status) VALUES(?,?,?,?,?,?,?,?,?)",
                               ('frt_'+key,key,kind,'Qingdao',pod,'MSK','20GP',2000+index,'当前生效'))
    connection.execute("INSERT INTO pending_records(entry_id,kind,payload,resolved,check_status,review_status) VALUES(?,?,?,?,?,?)",
                       ('frt_group_pending','rates',json.dumps({'目的港':'NEB'}),json.dumps({'目的港':'NEB'}),'passed','awaiting'))
    connection.execute("INSERT INTO alias_scope(alias,kind,scope_group,target) VALUES('local north','pod','test-group','NEB')")
    connection.commit()
    before = business_digest(connection)
    result = merge(connection,'Brazil North Ports',['NEB','NE ports'],apply=True)
    connection.commit()
    manualdb.invalidate_master()
    test.assertTrue(result['data']['applied'])
    test.assertEqual(business_digest(connection),before)
    test.assertEqual(connection.execute("SELECT target FROM alias_scope WHERE alias='local north'").fetchone()[0],'Brazil North Ports')
    master = manualdb.Master(connection)
    for value in ('Brazil North Ports','NEB','NE ports','北巴'):
        test.assertEqual(master.group_for(value,'pod'),'Brazil North Ports')
        test.assertEqual(master.name_resolver.resolve('pod',value)['canonical'],'Brazil North Ports')
        test.assertEqual(master.name_resolver.group_index()['pod'][manualdb.fold(value)],'Brazil North Ports')
        test.assertEqual(master.group_for(value,'pol'),'')
        for kind in ('rates','space'):
            test.assertEqual(manualdb.query(connection,master,kind,{'pod':value})['total'],3)
            test.assertEqual(manualdb.query(connection,master,kind,{'pod':value,'nogroup':'1'})['total'],2)
            test.assertEqual(manualdb.query_grouped(connection,master,kind,{'pod':value})['total'],3)
    test.assertEqual(master.group_for('local north','pod','test-group'),'Brazil North Ports')
    expanded = master.expand_cn({'目的港':'NEB','起运港':'Qingdao','船司':'MSK','柜型':'20GP','海运费USD':2000})
    test.assertEqual(len(expanded),1)
    test.assertEqual(expanded[0]['目的港'],'Brazil North Ports')
    for kind in ('rates','space'):
        test.assertEqual(manualdb.query(connection,master,kind,{'pod':'Itaqui'})['total'],3)
        test.assertEqual(manualdb.query(connection,master,kind,{'pod':'Itaqui','nogroup':'1'})['total'],1)
    resolved, errors = manualdb.resolve_core(master,'rates',{'目的港':'NEB','起运港':'Qingdao','船司':'MSK','柜型':'20GP'})
    test.assertEqual(resolved['目的港'],'Brazil North Ports')
    test.assertFalse(any(row['field']=='目的港' for row in errors))
    with test.assertRaises(ValueError):
        manualdb.master_save(connection,'port_groups',{'name':'Other North','aliases':'北巴','members':'Itaqui','kind':'pod'})
    connection.rollback()


class PortGroupAliasTests(unittest.TestCase):
    def setUp(self):
        self.connection = sqlite3.connect(':memory:')
        self.connection.row_factory = sqlite3.Row
        manualdb.init_schema(self.connection)

    def tearDown(self):
        self.connection.close()
        manualdb.invalidate_master()
        manualdb._GROUPED_CACHE.clear()

    def test_history_queries_and_validation(self):
        verify_group_aliases(self,self.connection)

    def test_different_members_cannot_merge(self):
        self.connection.execute("INSERT INTO port_groups(name,members,kind) VALUES('A','Itaqui','pod'),('B','Pecém','pod')")
        with self.assertRaises(ValueError):
            merge(self.connection,'C',['A','B'],True)
        self.assertIsNone(self.connection.execute("SELECT name FROM port_groups WHERE name='C'").fetchone())

    def test_same_alias_different_groups_is_rejected(self):
        self.connection.execute("INSERT INTO ports(name) VALUES('Itaqui')")
        self.connection.execute("INSERT INTO port_groups(name,label,members) VALUES('A','same','Itaqui'),('B','same','Itaqui')")
        master = manualdb.Master(self.connection)
        self.assertEqual(master.name_resolver.resolve('pod','same')['status'],'ambiguous')
        resolved, errors = manualdb.resolve_core(master,'rates',{'目的港':'same'})
        self.assertTrue(any(row['field']=='目的港' for row in errors))
        self.assertEqual(master.name_resolver.group_index()['pod'].get('same'),None)
