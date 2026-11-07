from pathlib import Path
import sys
import tempfile
import time
import types
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'plugin/scripts'))
from operations import Operations, OperationConflict
from reply_policy import compose, decision, ReplyPolicyError
from wechat import WeChat, dispatch


class ReplyPolicyTests(unittest.TestCase):
    def test_ratio_and_spacing_hold_through_a_long_conversation(self):
        history = []
        for i in range(80):
            policy = decision('thanks', history)
            history.insert(0, {'text': '', 'kind': 'sticker' if policy['useExpression'] else 'text', 'used': policy['useExpression']})
            self.assertLessEqual(sum(x['used'] for x in history[:20]) * 5, min(len(history), 20))
            self.assertLessEqual(sum(x['used'] for x in history[:5]), 1)
        self.assertEqual(sum(x['used'] for x in history), 16)

    def test_serious_contexts_never_add_expressions_when_quota_is_available(self):
        for scene in ('formal', 'serious', 'conflict', 'condolence'):
            body, policy = compose('我知道了', scene, [{'text': 'plain'}] * 19)
            self.assertEqual(body, '我知道了')
            self.assertFalse(policy['useExpression'])

    def test_recent_verbatim_expression_also_limits_drafted_reply(self):
        for value in ('好的 [抱拳]', '好的 😊', '好的 1️⃣','好的 /::)'):
            self.assertFalse(decision('casual', [{'text': value}] + [{'text': 'plain'}] * 18)['useExpression'])

    def test_predecorated_draft_is_rejected_instead_of_doubling_expression(self):
        with self.assertRaises(ReplyPolicyError):
            compose('哈哈 [呲牙]', 'casual', [{'text': 'plain'}] * 19)

    def test_expression_cannot_push_body_over_native_byte_limit(self):
        body, policy = compose('a' * 16000, 'thanks', [{'text': 'plain'}] * 19)
        self.assertEqual(len(body.encode('utf8')), 16000)
        self.assertFalse(policy['useExpression'])

    def test_invalid_or_null_scene_fails_before_send(self):
        class NoSend:
            def send(self, *args):raise AssertionError('must not send')
        for scene in (None, 'unknown', [], {}):
            with self.assertRaises(ReplyPolicyError):
                dispatch(NoSend(), {'action': 'send-reply', 'scene': scene})


class DurableReplyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.data = Path(self.temp.name)
        self.path = self.data / 'operations.sqlite3'
        self.sent = []
        self.native_calls = 0

    def tearDown(self):
        self.temp.cleanup()

    def seed(self, count=4, recipient='peer', account='self', status='confirmed'):
        log = Operations(self.path)
        try:
            for i in range(count):
                key = f'seed-{account}-{recipient}-{i}'
                item, _ = log.reserve(key, account, recipient, 'plain', 'seed:process', [])
                item['status'] = status
                log.save(key, item)
        finally:log.close()

    def service(self):
        service = object.__new__(WeChat)
        service.data = self.data
        service.live = lambda: {'accountId': 'self', 'pid': 123, 'identity': 'fake:creation'}
        service.recipient = lambda value: value
        service.module = lambda: types.SimpleNamespace(extract_master_key_from_cfg=lambda pid: (None, None, 'self'))
        service.rows = lambda *args: list(self.sent)
        def native(recipient, body, job, action):
            self.native_calls += 1
            self.sent.append({'direction': 'outgoing', 'serverId': str(100 + self.native_calls),
                              'localId': str(self.native_calls), 'text': body, 'createdAt': int(time.time())})
            return {'stage': 'submitted'}
        service.native = native
        return service

    def test_composed_body_is_sent_and_replay_after_restart_submits_once(self):
        self.seed()
        service = self.service()
        result = service.send('peer', '谢谢啦', 'draft-operation-001', 0, 'thanks')
        self.assertEqual(result['status'], 'confirmed')
        self.assertEqual(result['text'], '谢谢啦')
        self.assertEqual(self.sent[0]['text'], result['text'])
        replay = self.service().send('peer', '谢谢啦', 'draft-operation-001', 0, 'thanks')
        self.assertEqual(replay, result)
        self.assertEqual(self.native_calls, 1)
        with self.assertRaises(OperationConflict):
            self.service().send('peer', '谢谢啦', 'draft-operation-001', 0, 'casual')
        self.assertEqual(self.native_calls, 1)

    def test_verbatim_user_body_is_preserved_even_with_multiple_expressions(self):
        body = '哈哈 [呲牙][庆祝] 😊'
        result = self.service().send('peer', body, 'verbatim-operation-001', 0)
        self.assertEqual(result['text'], body)
        self.assertNotIn('replyPolicy', result)

    def test_history_is_isolated_by_account_and_recipient(self):
        self.seed(recipient='other-peer')
        self.seed(account='other-account')
        result = self.service().send('peer', '收到', 'isolated-operation-001', 0, 'greeting')
        self.assertEqual(result['text'], '收到')
        self.assertEqual(result['replyPolicy']['recentMessageCount'], 0)

    def test_failed_submission_does_not_earn_quota(self):
        self.seed(status='not_submitted')
        result = self.service().send('peer', '谢谢', 'failed-history-operation', 0, 'thanks')
        self.assertFalse(result['replyPolicy']['useExpression'])

    def test_policy_can_be_read_without_wechat_running_or_bound(self):
        result = dispatch(object.__new__(WeChat), {'action': 'reply-policy'})
        self.assertEqual(result['maxRatio'], 0.2)
        self.assertTrue(result['imageStickersSupported'])
        self.assertFalse(result['ownCreationsOnly'])
        self.assertEqual(result['allowedExpressionSources'],['wechat_builtin','wechat_favorite'])
        self.assertFalse(result['automaticBuiltinExpressions'])
        self.assertFalse(result['automaticReplies'])


if __name__ == '__main__':unittest.main()
