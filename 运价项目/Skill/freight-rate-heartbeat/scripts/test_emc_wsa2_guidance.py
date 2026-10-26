import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import record_builder as rb


class EmcWsa2GuidanceRegression(unittest.TestCase):
    TEXT = ("长荣西岸指导卖价\n"
            "WSA2/ WA07E023 (ETA CNXHK:10/10)\n"
            "WCSA/MX: USD5100/5300(INCL ISOCC,EBS)\n"
            "GTZNJ: USD6200/6400(INCL ISOCC,EBS)\n \n"
            "WSA2/ KTEG010E (ETA CNXHK:10/11)\n"
            "WCSA/MX: USD5100/5300(INCL ISOCC,EBS)\n"
            "GTZNJ: USD6200/6400(INCL ISOCC,EBS)")

    def msg(self, **extra):
        return dict({'_slug':'huanan_latam','id':'msg/fixture','s':'Mandy李龙艳',
                     't':'2026-09-30 11:08:36','c':self.TEXT}, **extra)

    def test_two_sailings_three_destinations_two_containers(self):
        rows = rb.t_emc_wsa2_guidance(self.msg())
        self.assertEqual(len(rows), 12)
        records = [row['record'] for row in rows]
        self.assertEqual({r['船司'] for r in records}, {'EMC'})
        self.assertEqual({r['船名航次'] for r in records}, {'WA07E023','KTEG010E'})
        self.assertEqual({r['航线服务'] for r in records}, {'WSA2'})
        self.assertEqual({r['目的港'] for r in records}, {'WCSA','MEX','Puerto Quetzal'})
        self.assertEqual({r['柜型'] for r in records}, {'20GP','40HQ/HC'})
        self.assertEqual({(r['目的港'],r['柜型']):r['海运费USD'] for r in records if r['船名航次']=='WA07E023'}, {
            ('WCSA','20GP'):5100, ('WCSA','40HQ/HC'):5300,
            ('MEX','20GP'):5100, ('MEX','40HQ/HC'):5300,
            ('Puerto Quetzal','20GP'):6200, ('Puerto Quetzal','40HQ/HC'):6400})
        self.assertTrue(all(r['AI判断']=='是' for r in records))
        self.assertTrue(all('ETA CNXHK' in r['备注'] for r in records))
        self.assertTrue(all('船期ETD' not in r for r in records))
        fingerprint = rows[0]['_meta']['template_fingerprint']
        self.assertEqual(fingerprint['pods'], ['MEX','Puerto Quetzal','WCSA'])
        self.assertEqual((fingerprint['pod_count'],fingerprint['record_count']),(3,12))

    def test_empty_dws_quote_placeholder_is_not_a_real_quote(self):
        empty_quote = {'t':None,'s':None,'senderId':None,'senderOpenDingTalkId':None,'id':None,'c':None}
        self.assertEqual(len(rb.t_emc_wsa2_guidance(self.msg(q=empty_quote))), 12)

    def test_structural_signature_survives_mojibake_title(self):
        damaged = self.TEXT.replace('长荣西岸指导卖价', '��������ָ������')
        rows = rb.t_emc_wsa2_guidance(self.msg(c=damaged))
        self.assertEqual(len(rows), 12)
        self.assertEqual({row['record']['船司'] for row in rows}, {'EMC'})

    def test_extra_priced_port_routes_whole_message_to_agent(self):
        changed = self.TEXT + '\nBALBOA: USD6300/6500(INCL ISOCC,EBS)'
        with self.assertRaises(rb.TemplateFingerprintMismatch):
            rb.t_emc_wsa2_guidance(self.msg(c=changed))

    def test_duplicate_product_with_different_price_fails(self):
        first = rb.rate(self.msg(), '华南基本港', 'WCSA', '南美西', 'EMC', '20GP', 5100)
        second = rb.rate(self.msg(), '华南基本港', 'WCSA', '南美西', 'EMC', '20GP', 5200)
        with self.assertRaises(rb.TemplateFingerprintMismatch):
            rb.check_template_fingerprint('fixture', [first, second], {'WCSA'}, 2,
                                          max_port_changes=0, max_record_changes=0)

    def test_generic_fingerprint_supports_bounded_two_to_three_item_drift(self):
        rows = [rb.rate(self.msg(), '华南基本港', pod, '南美西', 'EMC', '20GP', 1)
                for pod in ('A','B','C','D')]
        checked = rb.check_template_fingerprint('fixture', rows, {'A','B','C'}, 3,
                                                max_port_changes=3, max_record_changes=3)
        self.assertEqual(checked[0]['_meta']['template_fingerprint']['record_changes'], 1)

    def test_main_routes_fingerprint_drift_to_agent_queue(self):
        changed = self.TEXT + '\nBALBOA: USD6300/6500(INCL ISOCC,EBS)'
        message = self.msg(c=changed, q={'t':None,'s':None,'id':None,'c':None})
        message.pop('_slug')
        with tempfile.TemporaryDirectory() as folder:
            work = Path(folder)
            source = work / 'msgs_0930_B'
            source.mkdir()
            (source / 'huanan_latam.json').write_text(json.dumps([message], ensure_ascii=False), encoding='utf-8')
            output = work / 'draft'
            with patch.object(rb, 'WORK', str(work)), patch.object(rb.P, 'sfx', return_value='_B'), \
                    patch.object(rb, 'publisher_snapshot', return_value={}), \
                    patch.object(rb, 'publisher_decision', return_value={'action':'parse'}), \
                    patch('sys.argv', ['record_builder.py','--date','0930','--outdir',str(output)]):
                rb.main()
            self.assertEqual(json.loads((output/'draft_rate.json').read_text(encoding='utf-8')), [])
            needs = json.loads((output/'needs_llm.json').read_text(encoding='utf-8'))
            self.assertEqual(len(needs), 1)
            self.assertIn('模板指纹校验失败', needs[0]['reason'])
            self.assertIn('模板外报价', needs[0]['reason'])

    def test_partial_or_wrong_scope_is_not_accepted(self):
        with self.assertRaises(rb.TemplateFingerprintMismatch):
            rb.t_emc_wsa2_guidance(self.msg(c=self.TEXT.replace('GTZNJ: USD6200/6400(INCL ISOCC,EBS)','',1)))
        self.assertIsNone(rb.t_emc_wsa2_guidance(self.msg(_slug='qingdao_latam')))
        self.assertIsNone(rb.t_emc_wsa2_guidance(self.msg(q={'c':'原报价'})))


if __name__ == '__main__':
    unittest.main()
