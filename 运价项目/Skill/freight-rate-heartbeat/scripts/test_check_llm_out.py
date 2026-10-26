import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import check_llm_out as subject


class PriceBearingRateTests(unittest.TestCase):
    def test_price_bearing_rate_requires_container_and_price(self):
        source = {'c': 'ISTANBUL/IZMIT USD3700+ENS+14FT'}
        record = {'_table': 'rate', '船司': 'HPL', '备注': '保留原文待核对'}
        self.assertEqual(subject._incomplete_priced_rate(source, record), ['柜型', '海运费USD'])

    def test_complete_inferred_rate_passes_structure_check(self):
        source = {'c': 'ISTANBUL/IZMIT USD3700+ENS+14FT'}
        record = {'_table': 'rate', '柜型': '40HQ/HC', '海运费USD': 3700,
                  '备注': '参照同船司相近港口同期高柜报价，AI判断'}
        self.assertEqual(subject._incomplete_priced_rate(source, record), [])

    def test_formula_only_clause_cannot_be_empty_rate_record(self):
        source = {'c': 'RIO GRANDE addon +USD250/500'}
        record = {'_table': 'rate', '目的港': 'Rio Grande'}
        self.assertEqual(subject._incomplete_priced_rate(source, record), ['柜型', '海运费USD'])

    def test_bare_price_without_currency_is_detected(self):
        source = {'c': '10.7 上海-ISTANBUL 3700 2高'}
        record = {'_table': 'rate', '目的港': 'Istanbul'}
        self.assertEqual(subject._incomplete_priced_rate(source, record), ['柜型', '海运费USD'])

    def test_year_is_not_treated_as_bare_price(self):
        source = {'c': '2026年10月现舱开放'}
        record = {'_table': 'rate'}
        self.assertEqual(subject._incomplete_priced_rate(source, record), [])

    def test_space_and_non_price_messages_are_not_rate_blocked(self):
        self.assertEqual(subject._incomplete_priced_rate({'c': '现舱开放'}, {'_table': 'space'}), [])
        self.assertEqual(subject._incomplete_priced_rate({'c': '现舱开放'}, {'_table': 'rate'}), [])

    def test_priced_space_cannot_be_an_empty_placeholder(self):
        source = {'c': '上海 zim\n10.7 ZIM ARIES 12E\n7500/40hq -> charleston'}
        record = {'_table': 'space', '备注': '原图/正文已保留'}
        self.assertEqual(subject._incomplete_priced_space(source, record), ['核心字段'])

    def test_priced_space_with_one_confirmed_fact_is_not_blocked(self):
        source = {'c': '上海 zim\n10.7 ZIM ARIES 12E\n7500/40hq -> charleston'}
        record = {'_table': 'space', '船司': 'ZIM'}
        self.assertEqual(subject._incomplete_priced_space(source, record), [])

    def test_priced_space_requires_rate_and_space_companions(self):
        source = {'c': 'MSK现舱 10.3 NB-GDANSK 2X40HQ RATE USD2975'}
        self.assertEqual(subject._missing_dual_tables(source, [{'_table': 'rate'}]), ['space'])
        self.assertEqual(subject._missing_dual_tables(source, [{'_table': 'space'}]), ['rate'])
        self.assertEqual(subject._missing_dual_tables(source, [{'_table': 'rate'}, {'_table': 'space'}]), [])

    def test_plain_price_card_does_not_require_space_companion(self):
        source = {'c': 'MSK FAK 10.3 GDANSK USD2975/40HQ'}
        self.assertEqual(subject._missing_dual_tables(source, [{'_table': 'rate'}]), [])

    def test_empty_space_placeholder_is_blocked(self):
        source = {'c': '这个特价有到Gdansk吗？没有哦'}
        self.assertTrue(subject._invalid_space_fact(source, {'_table': 'space', '备注': '交人工核对'}))

    def test_space_destination_scope_accepts_route_or_service(self):
        self.assertEqual(subject._invalid_space_fact({}, {'_table': 'space', '航线': '地中海'}), [])
        self.assertEqual(subject._invalid_space_fact({}, {'_table': 'space', '航线服务': 'WSA'}), [])
        issues = subject._invalid_space_fact({}, {'_table': 'space', '船名航次': 'YM WINDOW / 048W'})
        self.assertTrue(any('缺少目的范围' in issue for issue in issues))

    def test_loss_fee_is_not_a_space_price(self):
        source = {'c': 'MSK电商现舱 亏舱USD100/柜 10.3 NB-GDANSK 2975'}
        record = {'_table': 'space', '舱位类型': '现舱', '目的港': 'Gdansk', '价格USD': 100}
        self.assertTrue(any('亏舱费' in issue for issue in subject._invalid_space_fact(source, record)))
        record['价格USD'] = 2975
        self.assertEqual(subject._invalid_space_fact(source, record), [])

    def test_explicit_space_price_cannot_disappear_behind_carrier(self):
        source = {'c': '上海 zim\n10.7 ZIM ARIES 12E\n7500/40hq -> charleston'}
        records = [{'_table': 'space', '船司': 'ZIM'}]
        self.assertEqual(subject._missing_explicit_pairs(source, records), [(7500, '40HQ/HC')])

    def test_all_explicit_pairs_are_checked_across_records(self):
        source = {'c': 'Charleston 7500/40HQ; Savannah 7800/40HQ'}
        records = [{'_table': 'rate', '柜型': '40HQ/HC', '海运费USD': 7500}]
        self.assertEqual(subject._missing_explicit_pairs(source, records), [(7800, '40HQ/HC')])

    def test_combined_container_prices_and_space_prices_are_covered(self):
        source = {'c': '1000/20GP 1500/40HQ 1800/40NOR'}
        records = [{'_table': 'rate', '柜型': '20GP/40HQ/HC', '海运费USD': '1000/1500'},
                   {'_table': 'space', '柜型': '40NOR', '价格USD': 1800}]
        self.assertEqual(subject._missing_explicit_pairs(source, records), [])

    def test_unmatched_text_is_not_treated_as_coverage_proof(self):
        source = {'c': '1000/20GP 1500/40HQ'}
        records = [{'_table': 'rate', '柜型': '20GP/40HQ/HC', '海运费USD': '1000'}]
        self.assertEqual(subject._missing_explicit_pairs(source, records),
                         [(1000, '20GP'), (1500, '40HQ/HC')])

    def test_three_digit_addon_is_not_mistaken_for_base_rate(self):
        source = {'c': '海运费 3500/40HQ，另加 USD250/40HQ 附加费'}
        records = [{'_table': 'rate', '柜型': '40HQ/HC', '海运费USD': 3500}]
        self.assertEqual(subject._missing_explicit_pairs(source, records), [])

    def test_triple_slash_price_is_not_a_price_container_pair(self):
        source = {'c': 'MX 5500/5800/40NOR 4400; WCSA 5500/5800/40NOR 4200'}
        records = [{'_table': 'rate', '柜型': '40NOR', '海运费USD': 4400},
                   {'_table': 'rate', '柜型': '40NOR', '海运费USD': 4200}]
        self.assertEqual(subject._missing_explicit_pairs(source, records), [])

    def test_struck_out_old_prices_are_not_required_in_current_records(self):
        source = {'c': 'NYC--8800/40H\n~~LA-6700/40H; CHI-8700/40H~~'}
        records = [{'_table': 'space', '柜型': '40HQ/HC', '价格USD': 8800}]
        self.assertEqual(subject._missing_explicit_pairs(source, records), [])

    def test_standalone_check_reads_source_text_before_coverage_check(self):
        with tempfile.TemporaryDirectory() as folder:
            work = Path(folder)
            (work / 'msgs_1005').mkdir()
            (work / 'llm_out_1005').mkdir()
            (work / 'msgs_1005' / 'meijia_sales.json').write_text(
                json.dumps([{'id': 'msg1', 'c': '7500/40HQ -> Charleston'}]), encoding='utf-8')
            (work / 'llm_out_1005' / 'msg1.json').write_text(json.dumps({
                'msg_id': 'msg1', 'group': 'meijia_sales', 'decision': 'records',
                'rule_basis': 'extraction-rules.md#17',
                'records': [{'_table': 'space', '船司': 'ZIM', '舱位类型': '现舱', '目的港': 'Charleston'}],
            }), encoding='utf-8')
            with patch.object(subject.P, 'sfx', return_value=''), \
                 patch.object(subject.P, 'chat_groups', return_value=[{'slug': 'meijia_sales'}]), \
                 patch.object(subject.spec_index, 'resolve_basis', return_value=(True, '')), \
                 patch.object(subject.spec_index, 'spec_for', return_value={'abbrev': []}):
                report = subject.check('1005', work=str(work), message_ids=['msg1'])
            self.assertEqual(report['n_err'], 1)
            self.assertIn('7500/40HQ/HC', report['errors'][0])

    def test_template_record_counts_unless_agent_replaces_template(self):
        with tempfile.TemporaryDirectory() as folder:
            work = Path(folder)
            (work / 'msgs_1005').mkdir()
            (work / 'llm_out_1005').mkdir()
            (work / 'draft_1005').mkdir()
            (work / 'msgs_1005' / 'meijia_sales.json').write_text(
                json.dumps([{'id': 'msg1', 'c': '7500/40HQ -> Charleston'}]), encoding='utf-8')
            decision = {'msg_id': 'msg1', 'group': 'meijia_sales', 'decision': 'records',
                        'rule_basis': 'extraction-rules.md#17',
                        'records': [{'_table': 'space', '船司': 'ZIM', '舱位类型': '现舱', '目的港': 'Charleston'}]}
            output = work / 'llm_out_1005' / 'msg1.json'
            output.write_text(json.dumps(decision), encoding='utf-8')
            (work / 'draft_1005' / 'draft_space.json').write_text(json.dumps([{
                '_meta': {'msg_id': 'msg1'},
                'record': {'柜型': '40HQ/HC', '价格USD': 7500},
            }]), encoding='utf-8')
            with patch.object(subject.P, 'sfx', return_value=''), \
                 patch.object(subject.P, 'chat_groups', return_value=[{'slug': 'meijia_sales'}]), \
                 patch.object(subject.spec_index, 'resolve_basis', return_value=(True, '')), \
                 patch.object(subject.spec_index, 'spec_for', return_value={'abbrev': []}):
                self.assertEqual(subject.check('1005', work=str(work), message_ids=['msg1'])['n_err'], 0)
                decision['replace_template'] = True
                output.write_text(json.dumps(decision), encoding='utf-8')
                self.assertEqual(subject.check('1005', work=str(work), message_ids=['msg1'])['n_err'], 1)


if __name__ == '__main__':
    unittest.main()
