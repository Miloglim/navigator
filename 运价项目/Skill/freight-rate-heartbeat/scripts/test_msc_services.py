import unittest
import record_builder as rb

TEXT = '''MSC 南美东 9.30-10.14更新
Base Port：USD5700/6000/NOR5300 Via Ipanema incl，GFS 周二船
USD5500/5800/NOR5200 Via Carioca for base Port incl GFS
USD4700/5000/NOR4600 Via Santana for base Port incl GFS 周六欧转慢船
NE Port：USD6700/6900/NOR5600 Incl GFS
PY Port：USD6800/7000 Incl GFS
Manaus/Villoa Do Conde：USD6500/7000 Incl GFS
Rosario/Zarate：USD5500/5900 Incl GFS
Base Port:NAVEGANTES/SANTOS
NE Port:SUAPE
PY Port:CAACUPEMI ASUNCION/CAACUPEMI PILAR/PUERTO SEGURO FLUVIAL (VILLETA)/TERPORT VILLETA'''


class MscServicesTests(unittest.TestCase):
    def message(self, text=TEXT):
        return {'_slug': 'dalian_latam', 'c': text, 'id': 'test-services', 't': '2026-09-28 15:47', 's': 'test'}

    def test_all_services_and_terminals(self):
        rows = [item['record'] for item in rb.t_msc_dalian_services(self.message())]
        self.assertEqual(len(rows), 37)
        for service, expected in [('Ipanema', [5700, 6000, 5300]), ('Carioca', [5500, 5800, 5200]), ('Santana', [4700, 5000, 4600])]:
            self.assertEqual([r['海运费USD'] for r in rows if r.get('航线服务') == service and r['目的港'] == 'SANTOS'], expected)
        terminals = [r for r in rows if r.get('目的码头')]
        self.assertEqual(len(terminals), 8)
        self.assertEqual({r['目的码头'] for r in terminals}, {'CAACUPEMI ASUNCION', 'CAACUPEMI PILAR', 'PUERTO SEGURO FLUVIAL (VILLETA)', 'TERPORT VILLETA'})
        self.assertEqual({r['目的港'] for r in terminals}, {'ASUNCION', 'PILAR', 'VILLETA'})
        self.assertTrue(all(r['消息原文'] == TEXT and r['消息ID'] == 'test-services' for r in rows))
        self.assertTrue(all((r['有效期起'], r['有效期止']) == ('2026-09-30', '2026-10-14') for r in rows))

    def test_no_partial_or_quoted_extraction(self):
        self.assertIsNone(rb.t_msc_dalian_services(self.message(TEXT.replace('USD5500/5800/NOR5200', 'USD待确认'))))
        self.assertIsNone(rb.t_msc_dalian_services(dict(self.message(), q={'c': 'quoted price'})))
        self.assertIsNone(rb.t_msc_dalian_services(self.message(TEXT.replace('Via Ipanema', ''))))
