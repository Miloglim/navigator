import unittest
from pathlib import Path
from unittest.mock import patch

import recover_backfill_images as recovery


class ImageRecoveryTests(unittest.TestCase):
    def source(self):
        return {'_slug':'qingdao_latam','_shard':'A','id':'fixture','s':'sender','t':'2026-09-16 10:00',
                'c':'YML 9.20-10.10','img':['@fixture']}

    def test_complete_price_card_expands_origins_destinations_and_equipment(self):
        cache = {'type':'price_card','carrier':'YML','pol':'Qingdao/Ningbo','valid_from':'2026-09-20',
                 'valid_to':'2026-10-10','source_rows':1,'source_priced_cells':3,
                 'rows':[{'pod':'Santos/Rio de Janeiro','20GP':7450,'40GP':7750,'40HQ':7750}]}
        with patch.object(recovery,'load_cache',return_value=(Path('fixture.json'),cache)), \
                patch.object(recovery,'decision_for',return_value=None):
            rows, _ = recovery.candidate_records(self.source())
        self.assertEqual(len(rows), 8)
        self.assertTrue(all(row['record']['消息ID']=='fixture' for row in rows))
        self.assertTrue(all(row['record']['图片名']=='@fixture' for row in rows))

    def test_grid_keeps_equal_original_columns_without_fake_duplicate_records(self):
        cache = {'type':'formula_card','source_rows':2,'source_priced_cells':4,
                 'raw_price_grid':[[4700,4700],[5200,5200]],'rows':[{'40NOR':4700},{'40NOR':5200}]}
        self.assertEqual(recovery.cache_inventory_errors(cache), [])
        cache['rows'][1]['40NOR'] = 5100
        self.assertTrue(recovery.cache_inventory_errors(cache))

    def test_missed_image_cells_cannot_be_submitted(self):
        cache = {'type':'price_card','source_rows':2,'source_priced_cells':4,
                 'rows':[{'pod':'Santos','20GP':7450,'40HQ':7750}]}
        with patch.object(recovery,'load_cache',return_value=(Path('fixture.json'),cache)):
            with self.assertRaises(ValueError):
                recovery.candidate_records(self.source())

    def test_expanded_rows_require_original_price_inventory(self):
        cache = {'type':'price_card','source_rows':1,'source_priced_cells':2,
                 'raw_source_rows':[{'pod':'Santos/Rio','prices':[7000,7500]}],
                 'rows':[{'pod':pod,'20GP':7000,'40HQ':7500} for pod in ('Santos','Rio')]}
        self.assertEqual(recovery.cache_inventory_errors(cache), [])
        cache['rows'][0]['20GP'] = 7100
        self.assertTrue(recovery.cache_inventory_errors(cache))

    def test_verified_formula_can_be_retained_without_fabricating_price(self):
        source = dict(self.source(), c='WHL table less USD200/200')
        formula = 'table less USD200/200'
        cache = {'type':'formula_card', 'cost_formula':formula, 'source_rows':1,
                 'source_priced_cells':2, 'rows':[{'pod':'Santos', '20GP':4000,'40HQ':5000}]}
        decision = {'decision':'records','replace_template':True,'records':[
            {'_table':'rate','起运港':'Qingdao','目的港':'Santos','柜型':'20GP',
             '船司':'WHL','备注':'参考FAK 4000; '+formula}]}
        with patch.object(recovery,'load_cache',return_value=(Path('fixture.json'),cache)), \
                patch.object(recovery,'decision_for',return_value=decision):
            rows, _ = recovery.candidate_records(source)
            self.assertFalse(rows[0]['record'].get('海运费USD'))
            cache['cost_formula'] = 'not in original source'
            with self.assertRaises(ValueError):
                recovery.candidate_records(source)


if __name__ == '__main__':
    unittest.main()
