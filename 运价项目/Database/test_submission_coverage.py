import unittest
from services.submission_coverage import compare_submission, assess_receipt

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

if __name__=='__main__':unittest.main()
