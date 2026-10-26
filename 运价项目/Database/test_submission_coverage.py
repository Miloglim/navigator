import unittest
from services.submission_coverage import compare_submission, assess_receipt, duplicate_match_fingerprint, fingerprint

class SubmissionCoverageTests(unittest.TestCase):
    def records(self):
        return [{'消息ID':'same','来源群':'group','目的港':'port-'+str(i),'海运费USD':100+i,'柜型':'20GP'} for i in range(12)]
    def test_one_missing_is_not_hidden_by_message_presence(self):
        expected=self.records()
        actual=[{'entry_id':str(i),'payload':r} for i,r in enumerate(expected[:-1])]
        report=compare_submission('rates',expected,actual)
        self.assertFalse(report['ok']);self.assertEqual(len(report['missing']),1)
        self.assertEqual(report['missing'][0]['目的港'],'port-11')
    def test_exact_duplicate_is_explained_but_price_difference_is_not(self):
        rec=self.records()[0];actual=[{'entry_id':'one','payload':rec}]
        report=compare_submission('rates',[rec,dict(rec)],actual)
        self.assertTrue(report['ok']);self.assertEqual(report['exact_duplicates_merged'],1)
        self.assertFalse(compare_submission('rates',[rec,dict(rec,海运费USD=200)],actual)['ok'])
    def test_later_loss_and_legitimate_edit(self):
        rec=self.records()[0];coverage=compare_submission('rates',[rec],[{'entry_id':'one','payload':rec}])
        receipt={'coverage':coverage}
        self.assertEqual(assess_receipt(receipt,{}, {'same':10})[0]['reason'],'entry_missing')
        entry={'one':dict(coverage['entries'][0],fingerprint='changed',edited=True)}
        self.assertEqual(assess_receipt(receipt,entry,{'same':1}),[])
    def test_legacy_partial_cannot_be_called_complete(self):
        self.assertEqual(assess_receipt({'requested':12,'staged':1,'message_ids':['same']},{},{'same':1})[0]['reason'],'legacy_partial_unverified')

    def test_partial_retry_reason_does_not_change_product_identity(self):
        first = dict(self.records()[0], partial_review=False, message_partial_review=True,
                     message_partial_reasons=['第一次覆盖失败'],
                     备注='[部分解析待核] 第一次覆盖失败；来源行=text:7')
        retry = dict(first, message_partial_reasons=['第二次覆盖失败，候选数量变化'],
                     备注='[部分解析待核] 第二次覆盖失败，候选数量变化；来源行=text:7')
        self.assertEqual(fingerprint('rates', first), fingerprint('rates', retry))
        changed_row = dict(retry, 备注='[部分解析待核] 第二次覆盖失败；来源行=text:8')
        self.assertNotEqual(fingerprint('rates', first), fingerprint('rates', changed_row))

    def test_internal_source_row_does_not_require_technical_copy_in_remark(self):
        legacy = dict(self.records()[0], partial_review=True,
                      备注='[部分解析待核] 覆盖检查失败；来源行=text:7')
        current = dict(self.records()[0], partial_review=True, _source_row='text:7', 备注='业务备注')
        self.assertEqual(fingerprint('rates', legacy), fingerprint('rates', current))
        self.assertEqual(duplicate_match_fingerprint('rates', current),
                         duplicate_match_fingerprint('rates', dict(current, _source_row='text:8')))

    def test_partial_multi_pol_identity_matches_ledger_first_explicit_segment(self):
        expected = dict(self.records()[0], partial_review=True, 起运港='越南/Jakarta, Java', 备注='来源行=text:24')
        actual = dict(expected, 起运港='越南')
        self.assertEqual(fingerprint('rates', expected), fingerprint('rates', actual))
        self.assertNotEqual(fingerprint('rates', expected),
                            fingerprint('rates', dict(actual, 起运港='Nansha')))

    def test_additive_recovery_ignores_source_row_but_not_price(self):
        first = dict(self.records()[0], partial_review=True,
                     备注='[部分解析待核] 覆盖检查失败；来源行=image:1:row:4')
        duplicate = dict(first, 备注='[部分解析待核] 覆盖检查失败；来源行=image:1:row:5')
        changed_price = dict(duplicate, 海运费USD=999)
        self.assertEqual(duplicate_match_fingerprint('rates', first),
                         duplicate_match_fingerprint('rates', duplicate))
        self.assertNotEqual(duplicate_match_fingerprint('rates', first),
                            duplicate_match_fingerprint('rates', changed_price))

if __name__=='__main__':unittest.main()
