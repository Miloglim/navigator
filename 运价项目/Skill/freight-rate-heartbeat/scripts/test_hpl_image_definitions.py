"""HPL image definitions, independent services and complete quote conditions."""
import unittest
import record_builder as rb

class HplImageDefinitionsTests(unittest.TestCase):
    def test_image_definition_is_not_global_ecsa_alias(self):
        pod,note=rb.hpl_pod_fields({'pod':'ECSA Base Port 1'}, {'ECSA Base Port 1':['BRSSZ','BRIOA']})
        self.assertEqual(pod,'BRSSZ/BRIOA');self.assertIn('本图底部定义',note)
        self.assertEqual(rb.hpl_pod_fields({'pod':'Argentina 1'})[0],'')
        self.assertEqual(rb.hpl_pod_fields({'pod':'US2 Port group'}, {'Argentina 1':['ARPMY']})[0],'')
    def test_services_survive_and_footer_is_not_truncated(self):
        msg={'_slug':'one_zim_hpl_csp','id':'fixture','s':'fixture','t':'2026-09-29 09:38','c':'HPL quote'}
        footer='x'*250+' BRITJ case by case; TW add USD15; ARPMY ARBHI subject to TAD'
        price={'20GP':100,'40HQ/HC':200,'40NOR':150}
        rows=[{'pod':'BRMAO','via':via,'sea':price,'mfr':{},'allin':{}} for via in ('','COCTG')]
        rows.append({'pod':'US2 Port group','via':'COCTG','sea':price,'mfr':{},'allin':{}})
        records=rb.t_hpl_cache(msg,{'valid_from':'2026-10-01','valid_to':'2026-10-14','rows':rows,'footer_notes':[footer],'pod_definitions':{'US2 Port group':['BRSSZ','BRPNG']}})
        self.assertEqual(len(records),9)
        self.assertEqual({r['record']['航线服务'] for r in records},{'原表未列经由','COCTG'})
        self.assertTrue(all(footer in r['record']['备注'] for r in records))
        self.assertEqual(records[-1]['record']['目的港'],'BRSSZ/BRPNG')
        self.assertEqual(records[-1]['record']['海运费USD'],150)
    def test_unresolved_classifications_cannot_collapse_into_one_product(self):
        msg={'_slug':'one_zim_hpl_csp','id':'fixture','s':'fixture','t':'2026-09-29 09:38','c':'HPL quote'}
        rows=[{'pod':name,'sea':{'20GP':100}} for name in ('ECSA Base Port 1','Argentina 1')]
        out=rb.t_hpl_cache(msg,{'rows':rows,'valid_from':'2026-10-01','valid_to':'2026-10-14'})
        self.assertEqual(len({r['record']['航线服务'] for r in out}),2)
        self.assertTrue(all(not r['record'].get('目的港') for r in out))

if __name__=='__main__':unittest.main()
