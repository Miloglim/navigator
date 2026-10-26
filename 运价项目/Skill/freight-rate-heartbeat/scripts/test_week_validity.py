import unittest

from validity_parser import derive, parse


class WeekValidityTests(unittest.TestCase):
    def test_single_week_is_a_validity_window_not_etd(self):
        for text, start, end in (
            ('41周', '2026-10-05', '2026-10-11'),
            ('WK42', '2026-10-12', '2026-10-18'),
            ('2026年第41周', '2026-10-05', '2026-10-11'),
        ):
            with self.subTest(text=text):
                self.assertEqual(parse(text, '2026-09-28', week_as_validity=True),
                                 {'valid_from': start, 'valid_to': end, 'etd': None})
                self.assertEqual(derive(text, '2026-09-28', week_as_validity=True), (start, end, None, False))
                self.assertEqual(parse(text, '2026-09-28'),
                                 {'valid_from': None, 'valid_to': None, 'etd': None})

    def test_iso_etd_keeps_date_and_independent_validity(self):
        self.assertEqual(derive('ETD 2026-10-06', '2026-09-29'),
                         ('2026-09-29', '2026-10-06', '2026-10-06', False))
        self.assertEqual(derive('2026-09-28 至 2026-09-28；ETD 2026-09-28', '2026-09-29'),
                         ('2026-09-28', '2026-09-28', '2026-09-28', False))
        self.assertEqual(derive('2026-09-29 至 2026-10-06；ETD 2026-10-06', '2026-09-29'),
                         ('2026-09-29', '2026-10-06', '2026-10-06', False))

    def test_two_weeks_are_not_one_window(self):
        self.assertEqual(parse('41/42周', '2026-09-28', week_as_validity=True),
                         {'valid_from': None, 'valid_to': None, 'etd': None})

    def test_late_sailing_uses_actual_delayed_etd(self):
        for text, expected in (
            ('9/6晚开9/9', '2026-09-09'),
            ('8.25 晚开 9.4', '2026-09-04'),
            ('10.22晚开到10.28', '2026-10-28'),
            ('10-22晚开至10-28', '2026-10-28'),
        ):
            with self.subTest(text=text):
                self.assertEqual(parse(text, '2026-10-23')['etd'], expected)
        self.assertEqual(
            derive('HMM 10.22晚开到10.28 南美东 价格 $3950/4250', '2026-10-23'),
            ('2026-10-23', '2026-10-28', '2026-10-28', False),
        )


if __name__ == '__main__':
    unittest.main()
