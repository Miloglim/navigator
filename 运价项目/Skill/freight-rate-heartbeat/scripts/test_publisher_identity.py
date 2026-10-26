import json
import unittest
import tempfile
import hashlib
from pathlib import Path
from unittest.mock import patch
from datetime import date, timedelta
from freight_identity.services.publisher import assess_group, direct_mention_all, message_day, covered_days_from_intervals
from freight_identity import runtime as identity_runtime
import pipeline

AS_OF = '2026-09-28'
DAYS = [(date(2026, 9, 14) + timedelta(days=i)).isoformat() for i in range(14)
        if (date(2026, 9, 14) + timedelta(days=i)).weekday() < 5]

def messages(sender, per_day, body='价格更新', dates=DAYS):
    return [{'message_id': f'{sender}-{day}-{i}', 'sender': sender,
             'sender_open_id': sender, 'time': day + ' 12:00:00', 'content': body}
            for day in dates for i in range(per_day)]

def assess(data, coverage=DAYS):
    result = assess_group(data, as_of=AS_OF, covered_workdays=coverage)
    assert result['ok'], result
    return {m['identity_key']: m for m in result['data']['members']}

class PublisherRecognitionTests(unittest.TestCase):
    def test_unchanged_evidence_is_decoded_once_and_file_changes_invalidate_cache(self):
        with tempfile.TemporaryDirectory() as folder:
            work = Path(folder)
            source = work / 'msgs_fixture' / 'fixture.json'
            source.parent.mkdir()
            first = {'id':'first','senderOpenDingTalkId':'open-1','t':AS_OF+'T10:00:00+08:00','s':'航管','c':'报价'}
            second = {'id':'second','senderOpenDingTalkId':'open-1','t':AS_OF+'T11:00:00+08:00','s':'航管','c':'更新'}
            source.write_text(json.dumps([first]), encoding='utf-8')
            groups = [{'cid':'fixture-group','slug':'fixture','name':'测试群'}]
            with patch.object(identity_runtime, 'decode_messages', wraps=identity_runtime.decode_messages) as decode:
                messages = identity_runtime.read_evidence(work, groups, AS_OF)[0]
                self.assertEqual(len(messages), 1)
                self.assertEqual(identity_runtime.read_evidence(work, groups, AS_OF)[0], messages)
                self.assertEqual(decode.call_count, 1)
                source.write_text(json.dumps([first, second]), encoding='utf-8')
                self.assertEqual(len(identity_runtime.read_evidence(work, groups, AS_OF)[0]), 2)
                self.assertEqual(decode.call_count, 2)

    def test_direct_group_all_and_multi_publishers(self):
        result = assess(messages('a', 1, '运价更新@所有人', [AS_OF]) + messages('b', 1, '@所有人\n更新', [AS_OF]), [])
        self.assertTrue(all(r['status'] == 'publisher' and not r['frequency_ready'] for r in result.values()))
    def test_reply_never_inherits_quote(self):
        for body in ['询价\n——引用 原报价——\n@所有人 更新', '> @所有人 更新', '请 @所有人 通知']:
            self.assertFalse(direct_mention_all(body))
        reply = messages('sales', 1, '请问还有舱吗', [AS_OF])[0]
        reply['quotedMessage'] = {'content': '@所有人 更新'}
        self.assertNotEqual(assess([reply], [])['open:sales']['status'], 'publisher')
    def test_multiple_stable_publishers(self):
        result = assess(messages('first', 5) + messages('second', 4) + messages('sales', 1))
        for person in ['first', 'second']:
            self.assertEqual(result['open:' + person]['basis'], 'volume_and_frequency')
        self.assertNotEqual(result['open:sales']['status'], 'publisher')
    def test_weekend_and_unfinished_today_do_not_change_frequency(self):
        data = messages('first', 5) + messages('second', 4) + messages('sales', 1)
        data += messages('first', 100, dates=['2026-09-19', AS_OF])
        result = assess(data, DAYS + [AS_OF])['open:first']
        self.assertEqual(result['daily_cv'], 0)
        self.assertEqual(result['weekday_messages'], 50)
        self.assertEqual(result['covered_workdays'], 10)
        self.assertTrue(all(message_day(d['date']).weekday() < 5 for d in result['daily_counts']))
    def test_missing_coverage_is_unknown_not_zero(self):
        result = assess(messages('frequent', 10), [])['open:frequent']
        self.assertIsNone(result['activity_ratio'])
        self.assertIsNone(result['daily_cv'])
        self.assertFalse(result['stable_frequency'])
        self.assertTrue(all(d['count'] is None for d in result['daily_counts']))
        self.assertNotEqual(result['status'], 'publisher')
    def test_volume_gap_without_coverage_is_candidate_only(self):
        result = assess(messages('frequent', 10) + messages('sales_a', 1) + messages('sales_b', 1), [])['open:frequent']
        self.assertTrue(result['observed_volume_gap'])
        self.assertEqual(result['observed_volume_ratio'], 10)
        self.assertEqual(result['status'], 'candidate')
        self.assertFalse(result['stable_frequency'])
    def test_complete_zero_days_count_against_stability(self):
        data = messages('first', 20, dates=DAYS[:1]) + messages('second', 1) + messages('sales', 1)
        result = assess(data)['open:first']
        self.assertEqual(result['activity_ratio'], 0.1)
        self.assertFalse(result['stable_frequency'])
        self.assertNotEqual(result['status'], 'publisher')
    def test_rename_and_duplicate_not_double_counted(self):
        data = messages('a', 1, '@所有人 更新', [AS_OF])
        result = assess(data + [dict(data[0], sender='new name')], [])
        self.assertEqual(len(result), 1)
        self.assertEqual(result['open:a']['mention_all_count'], 1)
    def test_name_evidence_is_not_account_authority(self):
        message = messages('name', 1, '@所有人 更新', [AS_OF])[0]
        message.pop('sender_open_id')
        result = assess([message], [])['name:name']
        self.assertEqual(result['status'], 'publisher')
        self.assertFalse(result['identity_ready'])
    def test_invalid_date_and_business_timezone(self):
        self.assertFalse(assess_group([], as_of='invalid')['ok'])
        self.assertEqual(assess([{'message_id': 'x', 'time': 'bad'}], []), {})
        self.assertEqual(message_day('2026-09-27T20:00:00Z'), date(2026, 9, 28))
    def test_complete_day_stitches_successful_pulls(self):
        result = covered_days_from_intervals([
            ('2026-09-24 23:00:00', '2026-09-25 10:00:00'),
            ('2026-09-25 09:30:00', '2026-09-25 18:00:00'),
            ('2026-09-25 17:59:00', '2026-09-28 09:00:00')], as_of=AS_OF)
        self.assertTrue(result['ok'])
        self.assertEqual(result['data']['days'], ['2026-09-25'])
    def test_gap_cannot_be_called_full_coverage(self):
        result = covered_days_from_intervals([
            ('2026-09-25 00:00:00', '2026-09-25 10:00:00'),
            ('2026-09-25 10:00:01', '2026-09-26 00:00:00')], as_of=AS_OF)
        self.assertEqual(result['data']['days'], [])
        self.assertFalse(covered_days_from_intervals([('invalid', AS_OF)], as_of=AS_OF)['ok'])
    def test_response_archive_survives_raw_overwrite_and_deduplicates(self):
        body = json.dumps({'messages': [], 'complete': True})
        digest = hashlib.sha256(body.encode()).hexdigest()
        with tempfile.TemporaryDirectory() as folder, patch.object(pipeline, 'WORK', folder):
            run = Path(pipeline.rdir('fixture'))
            (run / 'raw' / 'pull_group.json').write_text(body, encoding='utf-8')
            receipt = {'group': {'status': 'ok', 'raw_sha256': digest}}
            filename = pipeline.write_pull_receipt('fixture', {'stage': 'PARSED'}, receipt)
            fact = json.loads(Path(filename).read_text(encoding='utf-8'))['groups']['group']
            archived = run / fact['raw_file']
            (run / 'raw' / 'pull_group.json').write_text('overwritten', encoding='utf-8')
            self.assertEqual(archived.read_text(encoding='utf-8'), body)
            self.assertEqual(pipeline.archive_pull_response('fixture', 'group', body, digest), fact['raw_file'])
            self.assertEqual(len(list((run / 'raw_archive').glob('*.json'))), 1)
    def test_changed_response_cannot_claim_original_receipt_hash(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(pipeline, 'WORK', folder):
            with self.assertRaisesRegex(RuntimeError, 'changed before receipt'):
                pipeline.archive_pull_response('fixture', 'group', 'changed', hashlib.sha256(b'original').hexdigest())
    def test_user_confirmation_is_group_and_account_scoped(self):
        data = messages('zhao', 1, dates=[AS_OF])
        confirmation = [{'group_cid': 'europe', 'name': 'zhao', 'sender_open_id': 'zhao', 'source': 'user'}]
        matching = assess_group(data, as_of=AS_OF, group_id='europe', confirmations=confirmation)['data']['members'][0]
        self.assertEqual(matching['basis'], 'user_confirmed_account')
        self.assertEqual(matching['status'], 'publisher')
        other = assess_group(data, as_of=AS_OF, group_id='another', confirmations=confirmation)['data']['members'][0]
        self.assertNotEqual(other['status'], 'publisher')
    def test_same_name_history_combines_with_account_evidence(self):
        account = {'message_id':'account','sender':'zhao','sender_open_id':'oid',
                   'time':AS_OF+'T10:00:00+08:00','content':'报价'}
        history = {'message_id':'history','sender':'zhao',
                   'time':AS_OF+'T09:00:00+08:00','content':'@所有人 更新'}
        result = assess_group([account, history], as_of=AS_OF, group_id='europe')['data']
        self.assertEqual(len(result['members']), 1)
        member = result['members'][0]
        self.assertEqual(member['identity_key'], 'open:oid')
        self.assertEqual(member['message_count'], 2)
        self.assertEqual(member['historical_message_count'], 1)
        self.assertEqual(member['mention_all_count'], 1)
        self.assertEqual(member['status'], 'publisher')
        self.assertEqual(result['aliases'], {'name:zhao':'open:oid'})
        other = dict(account, message_id='other', sender_open_id='second')
        ambiguous = assess_group([account, other, history], as_of=AS_OF, group_id='europe')['data']
        self.assertEqual(len(ambiguous['members']), 3)
        self.assertFalse(ambiguous['aliases'])

    def test_confirmed_account_links_same_name_history_not_wrong_account(self):
        confirmation = [{'group_cid': 'europe', 'name': 'zhao', 'sender_open_id': 'correct', 'source': 'user'}]
        message = messages('wrong-account', 1, dates=[AS_OF])[0]
        message['sender'] = 'zhao'
        result = assess_group([message], as_of=AS_OF, group_id='europe', confirmations=confirmation)['data']['members'][0]
        self.assertNotEqual(result['status'], 'publisher')
        message.pop('sender_open_id')
        result = assess_group([message], as_of=AS_OF, group_id='europe', confirmations=confirmation)['data']['members'][0]
        self.assertEqual(result['basis'], 'user_confirmed_account')
        self.assertTrue(result['identity_ready'])
        self.assertEqual(result['historical_message_count'], 1)
        self.assertEqual(result['identity_key'], 'open:correct')
    def test_sender_id_fallback_matches_confirmed_account_even_with_new_name(self):
        confirmation = [{'group_cid': 'europe', 'name': 'old name', 'sender_open_id': 'open', 'sender_id': 'sid', 'source': 'user'}]
        message = messages('new name', 1, dates=[AS_OF])[0]
        message.pop('sender_open_id'); message['sender_id'] = 'sid'
        result = assess_group([message], as_of=AS_OF, group_id='europe', confirmations=confirmation)['data']['members'][0]
        self.assertEqual(result['basis'], 'user_confirmed_account')
    def test_full_dws_envelope_preserves_separate_actor_identity(self):
        m = {'createTime': AS_OF + ' 15:00:00', 'sender': 'sales', 'senderId': 'sales-id',
             'senderOpenDingTalkId': 'sales-open', 'openMessageId': 'reply', 'content': '请问还有舱吗',
             'quotedMessage': {'content': '@所有人 更新', 'sender': 'publisher', 'senderId': 'publisher-id',
                               'senderOpenDingTalkId': 'publisher-open'}}
        envelope = {'messages': [m], 'complete': True, 'paginationKnown': True, 'hasMore': False, 'count': 1}
        output, error = pipeline.parse_jsonish(json.dumps(envelope))
        self.assertIsNone(error)
        self.assertEqual(output[0]['senderId'], 'sales-id')
        self.assertEqual(output[0]['senderOpenDingTalkId'], 'sales-open')
        self.assertEqual(output[0]['q']['senderId'], 'publisher-id')
        self.assertEqual(output[0]['c'], m['content'])
        self.assertFalse(direct_mention_all(output[0]['c']))
        envelope['complete'] = False
        self.assertTrue(pipeline.parse_jsonish(json.dumps(envelope))[1])

    def test_chat_messages_envelope_accepts_message_id_and_text(self):
        envelope = {
            'messages': [{'createTime': AS_OF + ' 16:00:00', 'sender': 'sales',
                          'senderId': 'sales-id', 'messageId': 'message-id',
                          'text': '现舱更新'}],
            'complete': True, 'paginationKnown': True, 'hasMore': False, 'count': 1
        }
        output, error = pipeline.parse_jsonish(json.dumps(envelope))
        self.assertIsNone(error)
        self.assertEqual(output[0]['id'], 'message-id')
        self.assertEqual(output[0]['c'], '现舱更新')

    def test_file_id_does_not_enter_image_queue(self):
        envelope = {
            'messages': [{'openMessageId': 'file-message', 'createTime': AS_OF + ' 16:00:00',
                          'sender': 'sales', 'content': '[文件] notice.pdf',
                          'resourceRefs': [
                              {'resourceId': 'file-id', 'resourceIdType': 'fileId', 'type': 'fileId'},
                              {'resourceId': '@image-id', 'resourceIdType': 'mediaId', 'type': 'mediaId'},
                              {'resourceId': '$image-id', 'resourceIdType': 'mediaId', 'type': 'mediaId'}]}],
            'complete': True, 'paginationKnown': True, 'hasMore': False, 'count': 1
        }
        output, error = pipeline.parse_jsonish(json.dumps(envelope))
        self.assertIsNone(error)
        self.assertEqual(output[0]['img'], ['@image-id', '$image-id'])

if __name__ == '__main__': unittest.main()
