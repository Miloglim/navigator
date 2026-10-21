"""Derived 45-foot search stays consistent across counts, pagination and views."""
import sqlite3
import unittest

import manualdb
from services.container_features import has_45_high_cube


def verify_container_features(test, conn):
    fixtures = [
        ('hq', 'rates', '40HQ/HC', '45HQ+USD800', '', ''),
        ('hc', 'rates', '20GP', '', '45HC报价', ''),
        ('box', 'rates', '40NOR', '', '', '45英尺高箱'),
        ('false', 'rates', '20GP', '145HQ 450HQ 45GP USD45', '', ''),
        ('space', 'space', '40HQ/HC', "45'HQ", '', ''),
    ]
    for key,kind,ctype,remark,message,box in fixtures:
        conn.execute('INSERT INTO freight_items(entry_id,content_key,record_type,container_type,remark,message_text,box_desc,carrier,pol,pod,status,source_group) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
                     ('frt_feature_'+key,key,kind,ctype,remark,message,box,'MSK','Yantian',key,'当前生效','45测试群'))
    conn.commit()
    master = manualdb.Master(conn)
    result = manualdb.query(conn,master,'rates',{'ctype':'45HQ/HC','limit':'2'})
    test.assertEqual(result['total'],3)
    test.assertEqual(len(result['rows']),2)
    rest = manualdb.query(conn,master,'rates',{'ctype':'45HQ/HC','limit':'2','offset':'2'})
    test.assertEqual(rest['total'],3)
    test.assertEqual(len(rest['rows']),1)
    test.assertEqual({r['content_key'] for r in result['rows']+rest['rows']},{'hq','hc','box'})
    union = manualdb.query(conn,master,'rates',{'ctype':'20GP,45HQ/HC'})
    test.assertEqual(union['total'],4)
    test.assertEqual(manualdb.query(conn,master,'rates',{'ctype':'45HC'})['total'],3)
    test.assertEqual(manualdb.query(conn,master,'space',{'ctype':'45HQ/HC'})['total'],1)
    test.assertEqual(manualdb.query_grouped(conn,master,'rates',{'ctype':'45HQ/HC'})['total'],3)


class ContainerFeatureTests(unittest.TestCase):
    def test_40ft_standard_aliases_use_the_existing_40hq_price_bucket(self):
        for alias in ('40GP', '40DC', '40DV'):
            with self.subTest(alias=alias):
                self.assertTrue(manualdb.ctype_ok(alias))
                self.assertEqual(manualdb.norm_ctype(alias), '40HQ/HC')

    def test_45hq_is_an_independent_container_type(self):
        self.assertTrue(manualdb.ctype_ok('45HQ'))
        self.assertTrue(manualdb.ctype_ok('45HC'))
        self.assertEqual(manualdb.norm_ctype('45HQ'), '45HQ/HC')
        self.assertNotEqual(manualdb.norm_ctype('45HQ'), '40HQ/HC')

    def test_sqlite_derived_search(self):
        conn = sqlite3.connect(':memory:')
        conn.row_factory = sqlite3.Row
        try:
            manualdb.init_schema(conn)
            verify_container_features(self,conn)
        finally:
            conn.close()

    def test_high_cube_detection_boundaries(self):
        for value in ('45HQ','45 HC',"45'HQ",'45尺高柜','不接45HQ'):
            self.assertTrue(has_45_high_cube(value),value)
        for value in ('145HQ','450HQ','45HQfoo','45GP','USD45',None):
            self.assertFalse(has_45_high_cube(value),value)
