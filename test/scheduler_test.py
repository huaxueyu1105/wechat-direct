from datetime import datetime
from pathlib import Path
import sys
import tempfile
import threading
import unittest
import uuid
from unittest.mock import patch
from zoneinfo import ZoneInfoNotFoundError

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'plugin/scripts'))
from scheduler import Scheduler, ScheduleError


def at(value):
    return datetime.fromisoformat(value).timestamp()


class SchedulerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / 'schedules.sqlite3'
        self.scheduler = Scheduler(self.path, 'account-A')
        self.payload = {'action': 'send', 'recipientId': 'peer', 'text': '固定正文'}
        self.now = at('2026-11-09T09:00:00+08:00')
        self.calls = []

    def tearDown(self):
        self.scheduler.close()
        self.temp.cleanup()

    def runner(self, payload, operation_key):
        self.calls.append((payload, operation_key))
        return {'status': 'confirmed', 'serverId': '123'}

    def once(self, date='2026-11-09T08:00:00+08:00'):
        return self.scheduler.create(self.payload, {'kind': 'once', 'at': date}, now=self.now)

    def test_overdue_once_runs_once_and_preserves_payload(self):
        task = self.once()
        self.payload['text'] = '后续修改'
        result = self.scheduler.run_due(self.runner, now=self.now)
        self.assertEqual(result['runs'][0]['action'], 'attempted')
        self.assertEqual(result['runs'][0]['status'], 'confirmed')
        self.assertEqual(self.calls[0][0]['text'], '固定正文')
        self.assertEqual(self.scheduler.get(task['taskId'])['status'], 'completed')
        self.scheduler.run_due(self.runner, now=self.now + 86400)
        self.assertEqual(len(self.calls), 1)

    def test_fixed_task_id_replay_after_reopen_does_not_create_or_reactivate(self):
        task_id = str(uuid.uuid4())
        rule = {'kind': 'daily', 'time': '08:00'}
        first = self.scheduler.create(self.payload, rule, now=self.now, task_id=task_id)
        self.scheduler.pause(task_id)
        self.scheduler.close()
        self.scheduler = Scheduler(self.path, 'account-A')
        replay = self.scheduler.create(self.payload, rule, now=self.now + 86400, task_id=task_id)
        self.assertEqual(replay['status'], 'paused')
        self.assertEqual(replay['nextRunAt'], first['nextRunAt'])
        self.assertEqual(len(self.scheduler.list()), 1)

    def test_fixed_task_id_changes_or_other_account_conflict(self):
        task_id = str(uuid.uuid4())
        rule = {'kind': 'daily', 'time': '08:00'}
        self.scheduler.create(self.payload, rule, now=self.now, task_id=task_id)
        variants = [({**self.payload, 'text': 'changed'}, rule, 'Asia/Shanghai'),
                    (self.payload, {'kind': 'daily', 'time': '09:00'}, 'Asia/Shanghai'),
                    (self.payload, rule, 'UTC')]
        for payload, schedule, timezone_name in variants:
            with self.assertRaises(ScheduleError) as caught:
                self.scheduler.create(payload, schedule, timezone_name, now=self.now, task_id=task_id)
            self.assertEqual(caught.exception.code, 'SCHEDULE_CONFLICT')
        other = Scheduler(self.path, 'account-B')
        try:
            with self.assertRaises(ScheduleError) as caught:
                other.create(self.payload, rule, now=self.now, task_id=task_id)
            self.assertEqual(caught.exception.code, 'SCHEDULE_CONFLICT')
            self.assertEqual(other.list(), [])
        finally:
            other.close()
        self.assertEqual(len(self.scheduler.list()), 1)

    def test_invalid_task_id_cannot_create_task(self):
        for task_id in ('', 'not-a-uuid', 42):
            with self.assertRaises(ScheduleError) as caught:
                self.scheduler.create(self.payload, {'kind': 'daily', 'time': '08:00'}, task_id=task_id)
            self.assertEqual(caught.exception.code, 'INVALID_SCHEDULE_ID')
        self.assertEqual(self.scheduler.list(), [])

    def test_daily_missed_runs_send_only_latest_and_advance(self):
        task = self.scheduler.create(self.payload, {'kind': 'daily', 'time': '08:00'}, now=self.now)
        late = at('2026-11-15T10:00:00+08:00')
        result = self.scheduler.run_due(self.runner, now=late)
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(result['runs'][0]['scheduledAt'], '2026-11-15T00:00:00+00:00')
        self.assertEqual(self.scheduler.get(task['taskId'])['nextRunAt'], '2026-11-16T00:00:00+00:00')

    def test_weekly_uses_explicit_weekdays_and_timezone(self):
        task = self.scheduler.create(self.payload, {'kind': 'weekly', 'time': '08:00', 'weekdays': [0, 4]}, now=self.now)
        self.assertEqual(task['nextRunAt'], '2026-11-13T00:00:00+00:00')
        result = self.scheduler.run_due(self.runner, now=at('2026-11-18T10:00:00+08:00'))
        self.assertEqual(result['runs'][0]['scheduledAt'], '2026-11-16T00:00:00+00:00')
        self.assertEqual(self.scheduler.get(task['taskId'])['nextRunAt'], '2026-11-20T00:00:00+00:00')

    def test_pause_cancel_and_resume_do_not_send_on_mutation(self):
        first = self.once()
        second = self.once()
        self.scheduler.pause(first['taskId'])
        self.scheduler.cancel(second['taskId'])
        self.scheduler.run_due(self.runner, now=self.now)
        self.assertEqual(self.calls, [])
        self.scheduler.resume(first['taskId'])
        self.assertEqual(self.calls, [])
        self.scheduler.run_due(self.runner, now=self.now)
        self.assertEqual(len(self.calls), 1)
        with self.assertRaises(ScheduleError):
            self.scheduler.resume(second['taskId'])

    def test_unknown_reuses_key_only_for_status_and_blocks_later_periods(self):
        task = self.scheduler.create(self.payload, {'kind': 'daily', 'time': '10:00'}, now=self.now)
        result = self.scheduler.run_due(lambda p, k: {'status': 'unknown'}, now=self.now + 3600)
        key = result['runs'][0]['operationKey']
        queried = []
        self.scheduler.close()
        self.scheduler = Scheduler(self.path, 'account-A')
        self.scheduler.pause(task['taskId'])
        self.scheduler.resume(task['taskId'])
        def missing(operation_key):
            queried.append(operation_key)
            return {'ok': False, 'error': {'code': 'OPERATION_NOT_FOUND'}}
        result = self.scheduler.run_due(self.runner, missing, now=self.now + 3 * 86400)
        self.assertEqual(queried, [key])
        self.assertEqual(self.calls, [])
        self.assertEqual(result['runs'][0]['status'], 'unknown')

    def test_crash_after_claim_is_only_queried_after_reopen(self):
        task = self.once()
        class Crash(BaseException):
            pass
        def crash(payload, operation_key):
            raise Crash()
        with self.assertRaises(Crash):
            self.scheduler.run_due(crash, now=self.now)
        key = self.scheduler.get(task['taskId'])['lastRun']['operationKey']
        self.scheduler.close()
        self.scheduler = Scheduler(self.path, 'account-A')
        queried = []
        def confirmed(operation_key):
            queried.append(operation_key)
            return {'status': 'confirmed'}
        self.scheduler.run_due(self.runner, confirmed, now=self.now)
        self.assertEqual(self.calls, [])
        self.assertEqual(queried, [key])
        self.assertEqual(self.scheduler.get(task['taskId'])['status'], 'completed')

    def test_exception_stays_unknown_and_cancel_does_not_resubmit(self):
        task = self.once()
        def failure(*args):
            raise OSError('offline')
        self.scheduler.run_due(failure, now=self.now)
        self.scheduler.cancel(task['taskId'])
        result = self.scheduler.run_due(self.runner, failure, now=self.now + 86400)
        self.assertEqual(result['runs'][0]['status'], 'unknown')
        self.assertEqual(self.calls, [])
        self.assertEqual(self.scheduler.get(task['taskId'])['status'], 'cancelled')

    def test_not_submitted_once_fails_but_daily_continues_next_period(self):
        once = self.once()
        daily = self.scheduler.create(self.payload, {'kind': 'daily', 'time': '10:00'}, now=self.now)
        self.scheduler.run_due(lambda p, k: {'status': 'not_submitted'}, now=self.now + 3600)
        self.assertEqual(self.scheduler.get(once['taskId'])['status'], 'failed')
        self.assertEqual(self.scheduler.get(daily['taskId'])['status'], 'active')
        self.scheduler.run_due(self.runner, now=self.now + 86400 + 3600)
        self.assertEqual(len(self.calls), 1)

    def test_accounts_cannot_list_mutate_or_run_each_others_tasks(self):
        task = self.once()
        other = Scheduler(self.path, 'account-B')
        try:
            self.assertEqual(other.list(), [])
            for method in (other.get, other.pause, other.resume, other.cancel):
                with self.assertRaises(ScheduleError):
                    method(task['taskId'])
            self.assertEqual(other.run_due(self.runner, now=self.now)['runs'], [])
            self.assertEqual(self.calls, [])
        finally:
            other.close()

    def test_two_runners_claim_a_due_send_only_once(self):
        self.once()
        started, release = threading.Event(), threading.Event()
        errors = []
        def run_first():
            scheduler = Scheduler(self.path, 'account-A')
            try:
                def slow(payload, key):
                    self.calls.append((payload, key))
                    started.set()
                    if not release.wait(5):
                        raise RuntimeError('test wait timed out')
                    return {'status': 'confirmed'}
                scheduler.run_due(slow, now=self.now)
            except Exception as error:
                errors.append(error)
            finally:
                scheduler.close()
        thread = threading.Thread(target=run_first)
        thread.start()
        try:
            self.assertTrue(started.wait(5))
            result = self.scheduler.run_due(self.runner, now=self.now)
            self.assertEqual(result['runs'][0]['action'], 'unresolved')
            self.assertEqual(len(self.calls), 1)
        finally:
            release.set()
            thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])

    def test_invalid_rules_payloads_and_missing_timezone_fail_explicitly(self):
        for rule in ({'kind': 'once', 'at': '2026-11-09T10:00:00'}, {'kind': 'daily', 'time': '25:00'},
                     {'kind': 'weekly', 'time': '08:00', 'weekdays': []}):
            with self.assertRaises(ScheduleError):
                self.scheduler.create(self.payload, rule)
        for payload in ({'action': 'shell', 'recipientId': 'peer', 'text': 'x'},
                        {**self.payload, 'operationKey': 'override'}, {**self.payload, 'recipientId': ''}):
            with self.assertRaises(ScheduleError):
                self.scheduler.create(payload, {'kind': 'daily', 'time': '08:00'})
        with patch('scheduler.ZoneInfo', side_effect=ZoneInfoNotFoundError('missing')):
            with self.assertRaises(ScheduleError) as caught:
                self.scheduler.create(self.payload, {'kind': 'daily', 'time': '08:00'}, timezone='America/New_York')
            self.assertEqual(caught.exception.code, 'TIMEZONE_UNAVAILABLE')
            self.scheduler.create(self.payload, {'kind': 'daily', 'time': '08:00'})

    def test_image_and_file_payloads_are_fixed_references_only(self):
        for action in ('send-image', 'send-file'):
            task = self.scheduler.create({'action': action, 'recipientId': 'peer', 'filePath': 'C:/fictional/file',
                                          'fileSha256': 'a' * 64}, {'kind': 'once', 'at': '2026-11-09T08:00:00+08:00'}, now=self.now)
            self.assertEqual(task['payload']['action'], action)
        self.scheduler.run_due(self.runner, now=self.now)
        self.assertEqual(len(self.calls), 2)


if __name__ == '__main__':
    unittest.main()
