import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from core.company_rates_contracts import COMPANY_RATE_DDL, COMPANY_MAPPING_ROUTE, COMPANY_MAPPING_SAVE_ROUTE, COMPANY_MAPPING_PROBE_ROUTE
from ops import company_mappings as store
from ops.company_rates import CompanyRateRuntime
from services.company_rates import local_mapping_profile
from transport.access_rules import authorize
from transport.company_rate_routes import build_company_rate_routes

MASTER = {'ports':[{'name':'Pecém','country':'BR','locode_ref':'BRPEC'}],
          'carriers':[{'code':'MSK','name_en':'MAERSK'}]}

class Inline:
    def submit(self, fn, *args):
        fn(*args)

class MappingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name)/'mapping.db'
        with closing(self.connect()) as c:
            for ddl in COMPANY_RATE_DDL.values():
                c.execute(ddl)
            c.commit()
        self.runtime = self.new_runtime()

    def tearDown(self):
        self.runtime.executor = None
        self.temp.cleanup()

    def connect(self):
        c = sqlite3.connect(self.path)
        c.row_factory = sqlite3.Row
        return c

    def new_runtime(self):
        return CompanyRateRuntime(self.connect,{'YQN_DINGTALK_TOKEN':'fixture','YQN_USER_ID':'99'},
                                  executor=Inline(), master_provider=lambda c: MASTER)

    def form(self, **changes):
        return dict(kind='carrier',local_name='MSK',company_id=4550,company_code='MAERSK',
                    company_name='Maersk',status='confirmed',_actor_id='admin',**changes)

    def test_confirmed_mapping_persists_across_runtime_and_is_shared(self):
        self.runtime.mapping_save(self.form())
        other = self.new_runtime()
        other._post = lambda req: self.fail('confirmed mapping must not call external matching')
        self.assertEqual(other._carrier(99,'MSK')['id'],4550)
        self.assertEqual(other.mapping_list()['rows'][0]['updated_by'],'admin')

    def test_auto_candidate_does_not_overwrite_confirmed_or_disabled(self):
        saved = self.runtime.mapping_save(self.form())
        for status in ('confirmed','disabled'):
            if status=='disabled':
                saved=self.runtime.mapping_save({**saved,'status':status,'_actor_id':'admin'})
            c=self.connect()
            store.candidate(c,99,'carrier','MSK',{'id':1,'code':'wrong'})
            row=store.read(c,99,'carrier','MSK'); c.close()
            self.assertEqual(row['company_id'],4550)
            self.assertEqual(row['status'],status)

    def test_disabled_blocks_even_warm_cache_and_can_be_reconfirmed(self):
        saved=self.runtime.mapping_save(self.form())
        saved=self.runtime.mapping_save({**saved,'status':'disabled','_actor_id':'admin'})
        self.runtime._post=lambda req:self.fail('disabled mapping must not fall back')
        with self.assertRaisesRegex(ValueError,'停用'):
            self.runtime._carrier(99,'MSK')
        self.runtime.mapping_save({**saved,'status':'confirmed','_actor_id':'admin'})
        self.assertEqual(self.runtime._carrier(99,'MSK')['id'],4550)

    def test_stale_edit_rejected_and_business_identity_isolated(self):
        saved=self.runtime.mapping_save(self.form())
        edited=self.runtime.mapping_save({**saved,'company_id':4551,'_actor_id':'admin'})
        with self.assertRaisesRegex(ValueError,'刷新'):
            self.runtime.mapping_save({**saved,'_actor_id':'admin'})
        self.assertEqual(edited['revision'],2)
        self.assertIsNone(self.runtime._confirmed_mapping(100,'carrier',local_mapping_profile('MSK','carrier',MASTER['carriers'])))

    def test_large_business_identity(self):
        self.runtime.environ['YQN_USER_ID']='9876543210123'
        self.runtime.mapping_save(self.form())
        self.assertEqual(self.runtime._carrier(9876543210123,'MSK')['id'],4550)

    def test_invalid_local_id_and_country_rejected(self):
        for patch in ({'local_name':'UNKNOWN'},{'company_id':True},{'company_id':0},
                      {'kind':'port','local_name':'Pecém','company_code':'USPEC'}):
            with self.assertRaises(ValueError):
                self.runtime.mapping_save({**self.form(),**patch})

    def test_probe_records_pending_without_confirmation(self):
        self.runtime._post=lambda req:{'result':{'id':4550,'code':'MAERSK','match_text':'MAERSK'}}
        result=self.runtime.mapping_probe({'kind':'carrier','local_name':'MSK'})
        self.assertEqual(result['company_id'],4550)
        self.assertEqual(self.runtime.mapping_list()['rows'][0]['status'],'pending')

    def test_permissions_for_every_mapping_endpoint(self):
        get,post=build_company_rate_routes(self.runtime)
        for method,path,handler in [('GET',COMPANY_MAPPING_ROUTE,get[COMPANY_MAPPING_ROUTE]),
                                   ('POST',COMPANY_MAPPING_SAVE_ROUTE,post[COMPANY_MAPPING_SAVE_ROUTE]),
                                   ('POST',COMPANY_MAPPING_PROBE_ROUTE,post[COMPANY_MAPPING_PROBE_ROUTE])]:
            self.assertTrue(authorize(method,path,'admin')['ok'])
            for role in (None,'sales','operator','agent'):
                self.assertFalse(authorize(method,path,role,agent=role=='agent')['ok'])
                with self.assertRaises(PermissionError):
                    handler({'_role':role,'_actor_id':'fixture'})

    def test_mapping_change_invalidates_cached_prices(self):
        query={'pol':['Pecém'],'pod':['Pecém'],'carriers':['MSK']}
        self.runtime._port=lambda *args:{'id':20467,'code':'BRPEC'}
        self.runtime._company_pair=lambda *args:[{'product_id':7}]
        saved=self.runtime.mapping_save(self.form())
        self.runtime.create('sales','company',query)
        self.assertIsNotNone(self.runtime.create('sales','company',query)['cached'])
        self.runtime.mapping_save({**saved,'company_id':4551,'_actor_id':'admin'})
        self.assertIsNone(self.runtime.create('sales','company',query)['cached'])

if __name__=='__main__':
    unittest.main()
