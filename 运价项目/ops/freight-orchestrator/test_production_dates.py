import unittest
from datetime import date

from freight_flow.services import production


class ProductionDateTests(unittest.TestCase):
    anchor = date(2026, 10, 12)

    def test_explicit_full_dates_preserve_year(self):
        for value in ('2026/10/17', '2026.10.17', '2026-10-17', '2026/1/7'):
            expected = '2026-01-07' if value.endswith('/1/7') else '2026-10-17'
            self.assertEqual(production._date(value, self.anchor), expected)

    def test_existing_english_day_is_not_month_year(self):
        self.assertEqual(production._date('Oct-26', self.anchor), '2026-10-26')
        self.assertEqual(production._date('OCT.25', self.anchor), '2026-10-25')
        self.assertEqual(production._date('25 Oct.', self.anchor), '2026-10-25')

    def test_common_numeric_and_datetime_spellings_are_mechanical(self):
        self.assertEqual(production._date('10-25', self.anchor), '2026-10-25')
        self.assertEqual(production._date('2026-10-22 14:30', self.anchor), '2026-10-22')

    def test_bare_day_uses_message_month_and_rolls_forward_when_past(self):
        self.assertEqual(production._date('29号', date(2026, 10, 22)), '2026-10-29')
        self.assertEqual(production._date('3号', date(2026, 10, 22)), '2026-11-03')
        self.assertEqual(production._date('3日', date(2026, 12, 22)), '2027-01-03')

    def test_explicit_chinese_dates_follow_existing_year_rules(self):
        for value, anchor, expected in (
            ('10月16日', self.anchor, '2026-10-16'),
            ('2025年10月16日', self.anchor, '2025-10-16'),
            ('1月7日', date(2026, 12, 20), '2027-01-07'),
            ('12月28日', date(2026, 1, 5), '2025-12-28'),
        ):
            with self.subTest(value=value):
                self.assertEqual(production._date(value, anchor), expected)

    def test_chinese_dates_reject_invalid_days_and_uncertain_modifiers(self):
        for value in ('2026年2月29日', '2月30日', '13月1日', '10月16日（可能开航）', '可能10月16日'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                production._date(value, self.anchor)

    def test_validity_range_fills_both_bounds_without_etd(self):
        for field in ('有效期开始', '有效期截止'):
            candidate = {field: '10.20-10.31', 'ETD': ''}
            result = production.normalize_candidate_dates(candidate, self.anchor)
            self.assertEqual(result['有效期开始'], '2026-10-20')
            self.assertEqual(result['有效期截止'], '2026-10-31')
            self.assertEqual(result['ETD'], '')
            self.assertEqual(candidate[field], '10.20-10.31')

    def test_single_week_uses_iso_bounds(self):
        for value in ('43周', 'WK43', '2026年第43周'):
            result = production.normalize_candidate_dates({'有效期截止': value}, self.anchor)
            self.assertEqual((result['有效期开始'], result['有效期截止']),
                             ('2026-10-19', '2026-10-25'))
            self.assertEqual(result['ETD'], '')

    def test_explicit_year_and_december_range(self):
        for value, expected in (
            ('2025/10/20~10/31', ('2025-10-20', '2025-10-31')),
            ('12.28-1.3', ('2026-12-28', '2027-01-03')),
            ('10.20-31', ('2026-10-20', '2026-10-31')),
            ('20 Oct to 31 Oct', ('2026-10-20', '2026-10-31')),
        ):
            result = production.normalize_candidate_dates({'有效期开始': value}, self.anchor)
            self.assertEqual((result['有效期开始'], result['有效期截止']), expected)

    def test_ambiguous_or_invalid_windows_are_rejected(self):
        for value in ('41/42周', 'WK41/WK42', '54周', '10.31-10.20', '2.30-3.1'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                production.normalize_candidate_dates({'有效期开始': value}, self.anchor)

    def test_etd_does_not_select_one_day_from_window(self):
        with self.assertRaises(ValueError):
            production.normalize_candidate_dates({'ETD': '10.20-10.31'}, self.anchor)

    def test_week_etd_is_preserved_as_unbound_note_instead_of_blocking(self):
        result = production.normalize_candidate_dates({'ETD': 'wk32开始'}, self.anchor)
        self.assertEqual(result['ETD'], '')
        self.assertIn('未绑定船期:wk32开始', result['备注'])
        validity = production.normalize_candidate_dates({'有效期开始': 'wk32开始'}, self.anchor)
        self.assertEqual((validity['有效期开始'], validity['有效期截止']), ('', ''))
        self.assertIn('未绑定有效期:wk32开始', validity['备注'])

    def test_source_explicit_etd_window_is_preserved_without_single_etd(self):
        candidate = {'ETD': '10.20-10.31', '备注': '新单运价'}
        result = production.normalize_candidate_dates(candidate, self.anchor,
            source_text='HPL 新单运价 ETD10.20-10.31')
        self.assertEqual(result['ETD'], '')
        self.assertNotIn('船期截关', result)
        self.assertIn('ETD10.20-10.31', result['备注'])
        self.assertEqual((result['有效期开始'], result['有效期截止']), ('', ''))

    def test_etd_window_does_not_modify_explicit_cutoff(self):
        result = production.normalize_candidate_dates(
            {'ETD': '10.20-10.31', '船期截关': '10/18截关'}, self.anchor,
            source_text='ETD10.20-10.31；10/18截关')
        self.assertEqual(result['ETD'], '')
        self.assertEqual(result['船期截关'], '10/18截关')
        self.assertIn('ETD10.20-10.31', result['备注'])

    def test_misplaced_validity_range_cannot_be_converted_to_etd_note(self):
        with self.assertRaises(ValueError):
            production.normalize_candidate_dates({'ETD': '10.20-10.31'}, self.anchor,
                source_text='有效期10.20-10.31')

    def test_month_end_only_in_validity_end_and_uses_source_month(self):
        result = production.normalize_candidate_dates({'有效期截止': '月底'}, date(2024, 2, 10))
        self.assertEqual(result['有效期截止'], '2024-02-29')
        self.assertEqual(result['有效期开始'], '')
        for field in ('ETD', '有效期开始'):
            with self.assertRaises(ValueError):
                production.normalize_candidate_dates({field: '月底'}, self.anchor)

    def test_source_week_derivation_requires_rule_opt_in(self):
        candidate = {'ETD': '', '有效期开始': '', '有效期截止': ''}
        text = 'MSK 43周 价格更新'
        plain = production.normalize_candidate_dates(candidate, self.anchor, source_text=text)
        self.assertEqual((plain['有效期开始'], plain['有效期截止']), ('', ''))
        derived = production.normalize_candidate_dates(candidate, self.anchor,
            source_text=text, week_as_validity=True)
        self.assertEqual((derived['有效期开始'], derived['有效期截止']),
                         ('2026-10-19', '2026-10-25'))
        for text in ('MSK 41/42周', 'WK41/WK42', '41周 42周'):
            ambiguous = production.normalize_candidate_dates(candidate, self.anchor,
                source_text=text, week_as_validity=True)
            self.assertEqual((ambiguous['有效期开始'], ambiguous['有效期截止']), ('', ''))

    def test_conflicting_bounds_do_not_silently_overwrite(self):
        with self.assertRaises(ValueError):
            production.normalize_candidate_dates(
                {'有效期开始': '43周', '有效期截止': '10/31'}, self.anchor)

    def test_ledger_preserves_explicit_window_without_ai_default(self):
        source = {'message_id': 'm', 'text': '43周运价', 'message_time': '2026-10-12 10:00:00',
                  'group': {'name': 'test', 'cid': 'g'}, 'sender': 'test'}
        row = production.records_for_ledger(source, [{'_table': 'rate', '有效期开始': '43周'}])['rates'][0]
        self.assertEqual((row['有效期起'], row['有效期止'], row['船期ETD']),
                         ('2026-10-19', '2026-10-25', ''))
        self.assertNotIn('AI判断', row)


if __name__ == '__main__':
    unittest.main()
