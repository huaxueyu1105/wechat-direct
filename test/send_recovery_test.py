"""Crash recovery must unblock safely without resubmitting or inventing success."""
from pathlib import Path
import sys
import tempfile
import unittest

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'plugin/scripts'))
from operations import Operations,OperationConflict
from scheduler import Scheduler
from wechat import WeChat,PluginError


class SendRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(dir=ROOT.parent)
        self.folder=Path(self.temp.name)
        self.client=object.__new__(WeChat);self.client.data=self.folder
        self.client.config={'accountId':'self'}
        self.client.live=lambda:(_ for _ in ()).throw(AssertionError('No live access expected'))
        self.log=Operations(self.folder/'operations.sqlite3')
    def tearDown(self):self.log.close();self.temp.cleanup()
    def reserve(self,key='original-key',status='reserved'):
        item,_=self.log.reserve(key,'self','peer','test body','process',[])
        item['status']=status;self.log.save(key,item)
        return item
    def test_interrupted_reservation_is_proven_not_submitted_and_never_retried(self):
        self.reserve()
        result=self.client.send_status('original-key')
        self.assertEqual(result['status'],'not_submitted')
        self.assertEqual(result['error'],'INTERRUPTED_BEFORE_SUBMISSION')
        old,fresh=self.log.reserve('original-key','self','peer','test body','new-process',[])
        self.assertFalse(fresh);self.assertEqual(old['status'],'not_submitted')
        _,fresh=self.log.reserve('new-message','self','peer','different body','new-process',[])
        self.assertTrue(fresh)
    def test_unknown_requires_explicit_review_and_closed_does_not_mean_success(self):
        self.reserve(status='unknown')
        with self.assertRaises(PluginError):self.client.close_send('original-key')
        with self.assertRaises(OperationConflict):self.log.reserve('new-message','self','peer','other','process',[])
        result=self.client.close_send('original-key',reviewed=True)
        self.assertEqual(result['status'],'closed');self.assertNotIn('serverId',result)
        self.assertEqual(result['deliveredToRecipient'],'unknown')
        old,fresh=self.log.reserve('original-key','self','peer','test body','process',[])
        self.assertFalse(fresh);self.assertEqual(old['status'],'closed')
        _,fresh=self.log.reserve('new-message','self','peer','other','process',[])
        self.assertTrue(fresh)
    def test_other_account_cannot_close_operation(self):
        self.reserve(status='unknown');self.client.config={'accountId':'other'}
        with self.assertRaises(PluginError):self.client.close_send('original-key',reviewed=True)
        self.assertEqual(self.log.get('original-key')['status'],'unknown')
    def test_closing_completed_operation_keeps_receipt(self):
        item=self.reserve(status='confirmed');item['record']={'serverId':'123','localId':'1'}
        self.log.save('original-key',item)
        result=self.client.close_send('original-key',reviewed=True)
        self.assertEqual((result['status'],result['serverId']),('confirmed','123'))
    def test_manual_close_finishes_schedule_without_another_send(self):
        jobs=Scheduler(self.folder/'schedules.sqlite3','self')
        calls=[]
        try:
            task=jobs.create({'action':'send','recipientId':'peer','text':'test'},
                             {'kind':'once','at':'2026-01-01T00:00:00Z'},now=0)
            jobs.run_due(lambda p,k:calls.append(k) or {'status':'unknown'},now=1800000000)
            jobs.run_due(lambda p,k:calls.append(k),lambda key:{'status':'closed'},now=1800000001)
            self.assertEqual(len(calls),1)
            self.assertEqual(jobs.get(task['taskId'])['status'],'cancelled')
            self.assertEqual(jobs.get(task['taskId'])['lastRun']['status'],'closed')
        finally:jobs.close()

    def test_claim_without_operation_record_can_be_manually_closed(self):
        jobs=Scheduler(self.folder/'schedules.sqlite3','self')
        try:
            task=jobs.create({'action':'send','recipientId':'peer','text':'test'},
                             {'kind':'once','at':'2026-01-01T00:00:00Z'},now=0)
            jobs._claim(1800000000)  # Simulate process exit before invoking the sender.
            with self.assertRaises(PluginError):
                self.client.schedule('schedule-update',{'taskId':task['taskId'],'operation':'close-run'})
            result=self.client.schedule('schedule-update',{'taskId':task['taskId'],'operation':'close-run','reviewed':True})
            self.assertEqual(result['task']['status'],'cancelled')
            self.assertEqual(result['task']['lastRun']['status'],'closed')
            self.assertEqual(jobs.run_due(lambda *args:self.fail('Unexpected send'),now=1800000001)['runs'],[])
        finally:jobs.close()

    def test_close_run_preserves_a_known_outcome_from_send_journal(self):
        jobs=Scheduler(self.folder/'schedules.sqlite3','self')
        try:
            for operation_state,run_state,task_state in [('confirmed','confirmed','completed'),
                                                        ('reserved','not_submitted','failed')]:
                with self.subTest(operation_state=operation_state):
                    task=jobs.create({'action':'send','recipientId':'peer','text':'test body'},
                                     {'kind':'once','at':'2026-01-01T00:00:00Z'},now=0)
                    jobs._claim(1800000000)
                    key=jobs.get(task['taskId'])['lastRun']['operationKey']
                    item=self.reserve(key,status=operation_state)
                    if operation_state=='confirmed':
                        item['record']={'serverId':'123','localId':'1'};self.log.save(key,item)
                    result=self.client.schedule('schedule-update',{'taskId':task['taskId'],
                        'operation':'close-run','reviewed':True})['task']
                    self.assertEqual(result['lastRun']['status'],run_state)
                    self.assertEqual(result['status'],task_state)
                    if operation_state=='confirmed':self.assertEqual(result['lastRun']['result']['serverId'],'123')
        finally:jobs.close()


if __name__=='__main__':unittest.main()
