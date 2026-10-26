import unittest
import record_builder as rb
from validity_parser import parse, derive
MSG = {'_slug':'dixi_hei_price','id':'fixture-image','s':'测试','t':'2026-09-28 15:11:00','c':'MSC 全国-地西 10.01-14有效'}
class ImageRateRegression(unittest.TestCase):
    def test_stable_sender_metadata_comes_from_message(self):
        msg=dict(MSG,senderId='stable-sender',senderOpenDingTalkId='stable-open')
        rec=rb.base_cells('rate',msg,{'发送人ID':'forged'},'high')['record']
        self.assertEqual(rec['发送人ID'],'stable-sender')
        self.assertEqual(rec['发送人OpenID'],'stable-open')
        self.assertEqual(rec['来源群ID'],rb.GMAP[MSG['_slug']]['cid'])

    def test_priced_space_table_emits_rate_and_space_with_split_fields(self):
        out=rb.t_space_table_cache(MSG,{
            'type':'space_table','carrier':'MSC','route':'地西','rows':[
                {'etd':'10.6','pol':'Shanghai','pod':'Istanbul','vessel':'A.P. MOLLER/638W',
                 'vol':'1X40HC','rate':'3750','ft':'14','remark':'直达'},
                {'etd':'10.7','pol':'Ningbo','pod':'Istanbul','vessel':'TEST/001W',
                 'vol':'2X20GP','rate':'','remark':'无价舱位'}
            ]}) or []
        self.assertEqual(len(out),3)
        priced_rate=next(row for row in out if row['_meta']['table']=='rate')['record']
        priced_space=next(row for row in out if row['_meta']['table']=='space' and row['record']['起运港']=='Shanghai')['record']
        unpriced=next(row for row in out if row['_meta']['table']=='space' and row['record']['起运港']=='Ningbo')['record']
        self.assertEqual(priced_rate['海运费USD'],3750)
        self.assertEqual(priced_rate['船名航次'],'A.P. MOLLER/638W')
        self.assertNotIn('价格USD',priced_rate)
        self.assertNotIn('舱位类型',priced_rate)
        self.assertNotIn('箱量',priced_rate)
        self.assertNotIn('箱型箱量',priced_rate)
        self.assertEqual(priced_space['舱位类型'],'现舱')
        self.assertEqual(priced_space['箱量'],1)
        self.assertNotIn('价格USD',priced_space)
        self.assertIn('价格见同消息运价记录',priced_space['备注'])
        self.assertEqual(unpriced['舱位类型'],'现舱')
        self.assertNotIn('价格USD',unpriced)
    def card(self, rows, **extra):
        return rb.t_price_card_cache(MSG, dict(type='price_card', carrier='MSC', route='地西', pol='全国基本港', rows=rows, **extra)) or []
    def test_columns_dates_and_clauses(self):
        out=self.card([{'pod':'Sines','20GP':2775,'40GP':3400,'40HQ':3400,'45HQ':4025,'validity':'10.01-10.14'}],free_days=12,notes='9.28更新',footer_notes='自2026-04.01起附加费；Sines不接45HQ')
        self.assertEqual([(x['record']['柜型'],x['record']['海运费USD']) for x in out],[('20GP',2775),('40HQ/HC',3400),('45HQ/HC',4025)])
        for x in out:
            r=x['record']
            self.assertEqual((r['有效期起'],r['有效期止']),('2026-10-01','2026-10-14'))
            self.assertNotIn('AI判断',r)
            self.assertEqual(r['目免'],12)
            self.assertIn('不接45HQ',r['备注'])
        self.assertEqual(next(x['record']['海运费USD'] for x in out if x['record']['柜型']=='45HQ/HC'), 4025)
    def test_unequal_gp_is_preserved_for_review(self):
        out=self.card([{'pod':'Genoa','40GP':3300,'40HQ':3400}])
        self.assertEqual({x['record']['柜型']:x['record']['海运费USD'] for x in out},{'40HQ/HC':3400,'40GP':3300})
        x=next(x for x in out if x['record']['柜型']=='40GP')
        self.assertEqual(x['_meta']['conf'],'low')
        self.assertIn('待确认',x['record']['备注'])
    def test_conflicting_hq_aliases_do_not_choose_one(self):
        out=self.card([{'pod':'Genoa','40HQ':3400,'40HC':3500}])
        self.assertEqual(len(out),1)
        self.assertNotIn('柜型',out[0]['record'])
        self.assertEqual(out[0]['_meta']['conf'],'low')
        self.assertIn('不自动选价',out[0]['record']['备注'])
    def test_zero_nor_and_quoted_headers(self):
        out=self.card([{'pod':'Genoa',"20' GP":0,'40HC':3400,'40NOR':2900}])
        self.assertEqual([(x['record']['柜型'],x['record']['海运费USD']) for x in out],[('20GP',0),('40HQ/HC',3400),('40NOR',2900)])
    def test_shared_dv_hc_price_column_is_not_dropped(self):
        out=self.card([{'pod':'Iquique','20GP':6600,'40DV/HC':6850}])
        self.assertEqual([(x['record']['柜型'],x['record']['海运费USD']) for x in out], [('20GP',6600),('40HQ/HC',6850)])
    def test_paused_and_unsupported_only(self):
        out=self.card([{'pod':'Genoa','20GP':2775},{'pod':'Setubal','20GP':'/','tag':'暂停服务'}])
        self.assertEqual(len(out),1)
        self.assertIn('1行无报价/暂停服务',out[0]['record']['备注'])
        out=self.card([{'pod':'Genoa','45HQ':4025}])
        self.assertEqual(len(out),1)
        self.assertEqual(out[0]['_meta']['conf'],'med')
        self.assertEqual(out[0]['record']['柜型'],'45HQ/HC')
        self.assertEqual(out[0]['record']['海运费USD'],4025)
    def test_nested_matrix_date_priority(self):
        out=rb.t_rate_matrix_cache(MSG,{'carrier':'MSC','route':'地西','validity':'10.01-10.14','rows':[{'pol':'Qingdao','validity':'10.02-10.13','cells':[{'pod':'Genoa','20GP':2775,'validity':'10.03-10.12'},{'pod':'Sines','40HQ/HC':3400}]}]})
        self.assertEqual([(x['record']['有效期起'],x['record']['有效期止']) for x in out],[('2026-10-03','2026-10-12'),('2026-10-02','2026-10-13')])
        self.assertTrue(all(x['record']['起运港']=='Qingdao' for x in out))
    def test_flat_matrix_iso_dates(self):
        out=rb.t_rate_matrix_cache(MSG,{'carrier':'MSC','route':'地西','valid_from':'2026-10-01','valid_to':'2026-10-14','rows':[{'pod':'Genoa','20GP':2775,'40HQ/HC':3400}]})
        self.assertEqual(len(out),2)
        self.assertEqual(out[0]['record']['有效期止'],'2026-10-14')
    def test_invalid_image_date_is_low_confidence(self):
        out=self.card([{'pod':'Genoa','20GP':2775,'validity':'需另确认'}])
        self.assertEqual(out[0]['_meta']['conf'],'low')
        self.assertIn('图示有效期无法解析',out[0]['record']['备注'])
    def test_quoted_image_requires_semantic_review(self):
        quote=dict(MSG, id='reply', q={'c':'原报价'})
        self.assertTrue(rb._quoted_image_needs_review(quote, {'source_message':MSG['id']}))
        self.assertTrue(rb._quoted_image_needs_review(quote, {}))
        self.assertFalse(rb._quoted_image_needs_review(MSG, {'source_message':MSG['id']}))
        self.assertFalse(rb._quoted_image_needs_review(quote, {'source_message':'reply'}))
    def test_quoted_image_revision_is_reparsed(self):
        quote = dict(MSG, id='reply', c='这个是week43', q={'c':'原报价'})
        self.assertFalse(rb._quoted_image_needs_review(quote, {'source_message':MSG['id']}))
        quote['c'] = '有效期更正为week43'
        self.assertFalse(rb._quoted_image_needs_review(quote, {'source_message':MSG['id']}))
    def test_formula_card_preserves_reference_not_calculated_cost(self):
        msg = dict(MSG, c='实际成本：表价-600+EBS 400+50')
        cache = {'type':'formula_card','carrier':'MSK','pol':'青岛','cost_formula':'表价-600+EBS 400+50',
                 'validity':'9.22-9.28','rows':[{'pod':'Santos','40NOR':6400}]}
        rows = rb.t_formula_card_cache(msg, cache)
        self.assertEqual(len(rows), 1)
        self.assertNotIn('海运费USD', rows[0]['record'])
        self.assertIn('参考FAK USD6400', rows[0]['record']['备注'])
        self.assertEqual(rows[0]['record']['柜型'], '40NOR')
        self.assertIsNone(rb.t_formula_card_cache(MSG, cache))
    def test_explicit_fak_basis_preserves_source_price_without_applying_adjustment(self):
        msg = dict(MSG, c='原图FAK价格，BASKET成本减150/300')
        cache = {'type':'formula_card','carrier':'MSC','pol':'青岛','price_basis':'FAK',
                 'cost_formula':'BASKET成本减150/300','validity':'9.22-9.28',
                 'rows':[{'pod':'Santos','20GP':6300,'40HQ':6800}]}
        rows = rb.t_formula_card_cache(msg, cache)
        self.assertEqual([r['record']['海运费USD'] for r in rows], [6300, 6800])
        self.assertTrue(all('非折后合约成本' in r['record']['备注'] for r in rows))
        cache.pop('price_basis')
        rows = rb.t_formula_card_cache(msg, cache)
        self.assertEqual([r['record']['海运费USD'] for r in rows], [6300, 6800])
        self.assertTrue(all('非折后合约成本' in r['record']['备注'] for r in rows))

    def test_fak_prices_survive_without_cache_price_basis(self):
        msg = dict(MSG, c='原图FAK价格，BASKET/T0成本减150/300')
        cache = {'type':'formula_card','carrier':'MSC','pol':'青岛',
                 'cost_formula':'BASKET/T0成本减150/300','validity':'9.22-9.28',
                 'rows':[{'pod':'Santos','20GP':6300,'40HQ':6800}]}
        rows = rb.t_formula_card_cache(msg, cache)
        self.assertEqual([r['record']['海运费USD'] for r in rows], [6300, 6800])
    def test_row_origins_and_unresolved_group_scope(self):
        msg = dict(MSG, c='FAK价格；成本减150/300')
        cache = {'type':'formula_card','carrier':'MSC','pol':'Qingdao',
                 'price_basis':'FAK','cost_formula':'成本减150/300',
                 'rows':[{'pol':'Shekou','pod':'Turbo','20GP':9400},
                         {'pol':'Nansha','pod':'Turbo','20GP':9530},
                         {'pol':'Shekou','pod':'Turbo','40NOR':8600,
                          'price_scope_unresolved':'Grp1=8600/Grp2=8700，起运港成员未定义'}]}
        rows = rb.t_formula_card_cache(msg, cache)
        self.assertEqual([r['record']['起运港'] for r in rows[:2]], ['Shekou','Nansha'])
        self.assertEqual([r['record']['海运费USD'] for r in rows[:2]], [9400,9530])
        self.assertNotIn('起运港', rows[2]['record'])
        self.assertNotIn('海运费USD', rows[2]['record'])
        self.assertEqual(rows[2]['_meta']['conf'], 'low')
        self.assertIn('Grp1=8600/Grp2=8700', rows[2]['record']['备注'])
class CmaFakRegression(unittest.TestCase):
    TEXT = ('达飞南美东\n以下价格为长荣FAK提单成本，需要另加USD30+ISPS+local，利润不得低于USD100/BOX\n'
            '29 Sep to 14 Oct\nECSA BP - USD 5600/5900/5200NOR\n'
            'RIO GRANDE addon +USD250/500）\nASUNCION  addon +1000/+1200\n'
            'NEB - USD 6700/6900/5600NOR\n(PECEM: +400/TEU)\n'
            'BRVIX/BRVCO - USD 6900/7100/5800NOR\n9')

    def msg(self, **fields):
        return dict({'_slug':'cma_emc_pil', 'id':'fixture-fak', 's':'测试',
                     't':'2026-09-28 16:10', 'c':self.TEXT}, **fields)

    def test_full_message_prices_dates_and_separate_addons(self):
        rows = rb.t_cma_ecsa_fak(self.msg())
        self.assertEqual(len(rows), 12)
        priced = [r['record'] for r in rows if '海运费USD' in r['record']]
        expected = {'ECSA':(5600,5900,5200), 'NEB':(6700,6900,5600),
                    'BRVIX':(6900,7100,5800), 'BRVCO':(6900,7100,5800)}
        self.assertEqual({(r['目的港'],r['柜型']):r['海运费USD'] for r in priced},
                         {(pod,ct):p for pod,prices in expected.items()
                          for ct,p in zip(('20GP','40HQ/HC','40NOR'),prices)})
        addons = [r for r in rows if '海运费USD' not in r['record']]
        self.assertEqual(addons, [], 'surcharge clauses must not create empty quote rows')
        self.assertIn('+USD250/500',priced[0]['备注'])
        self.assertIn('NEB 基础上另加',priced[0]['备注'])
        for row in rows:
            r = row['record']
            self.assertEqual((r['有效期起'],r['有效期止']),('2026-09-29','2026-10-14'))
            self.assertNotIn('船期ETD',r)
            self.assertNotIn('AI判断',r)
            self.assertEqual(r['船司'],'CMA')
            self.assertEqual(r['消息原文'],self.TEXT)
            self.assertIn('USD30+ISPS+local',r['备注'])

    def test_empty_dws_quote_placeholder_does_not_block_template(self):
        empty_quote = {'t':None,'s':None,'senderId':None,'senderOpenDingTalkId':None,'id':None,'c':None}
        self.assertEqual(len(rb.t_cma_ecsa_fak(self.msg(q=empty_quote))), 12)

    def test_unknown_price_line_and_other_context_not_partially_accepted(self):
        self.assertIsNone(rb.t_cma_ecsa_fak(self.msg(c=self.TEXT+'\nOTHER - USD 1000/2000')))
        self.assertIsNone(rb.t_cma_ecsa_fak(self.msg(_slug='ningbo_space')))
        self.assertIsNone(rb.t_cma_ecsa_fak(self.msg(q={'c':'原报价'})))
        self.assertIsNone(rb.t_cma_ecsa_fak(self.msg(c=self.TEXT.replace('29 Sep to 14 Oct',''))))

    def test_english_to_ranges_never_become_etd(self):
        cases = [('29 Sep to 14 Oct','2026-09-29','2026-10-14'),
                 ('29 September TO 14 October','2026-09-29','2026-10-14'),
                 ('29-Sep to 14-Oct','2026-09-29','2026-10-14'),
                 ('29th Sep 2027 to 14th Oct 2027','2027-09-29','2027-10-14'),
                 ('29 Dec to 14 Jan','2026-12-29','2027-01-14'),
                 ('1 Sep - 7 Sep','2026-09-01','2026-09-07')]
        for text,start,end in cases:
            with self.subTest(text=text):
                self.assertEqual(parse(text,'2026-09-28'),dict(valid_from=start,valid_to=end,etd=None))
                self.assertEqual(derive(text,'2026-09-28'),(start,end,None,False))
        self.assertEqual(parse('ETD: 4 SEP','2026-09-28')['etd'],'2026-09-04')

if __name__=='__main__': unittest.main()
