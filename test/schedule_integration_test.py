"""Scheduling integration with a fake account and transport; never opens WeChat."""
from pathlib import Path
import hashlib
import sys
import tempfile
import unittest
import uuid

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'plugin/scripts'))
from wechat import WeChat, PluginError, dispatch


class ScheduleIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.service=object.__new__(WeChat)
        self.service.data=Path(self.temp.name)
        self.service.config={'accountId':'self'}
        self.service.support={'mediaCapabilities':{'image':True,'file':True}}
        self.service.live=lambda:{'accountId':'self'}
        self.service.recipient=lambda value:value
        self.service.rows=lambda recipient,limit:[]
        self.sent=[]
        self.service.send=lambda recipient,body,key,wait=0: self.sent.append(key) or {
            'status':'confirmed','operationKey':key,'text':body}
        self.request={'action':'schedule-create','taskId':str(uuid.uuid4()),
            'payload':{'action':'send','recipientId':'peer','text':'hello'},
            'schedule':{'kind':'once','at':'2000-01-01T09:00:00+08:00'}}

    def tearDown(self):
        self.temp.cleanup()

    def test_create_is_durable_idempotent_and_does_not_send(self):
        first=dispatch(self.service,self.request)
        second=dispatch(self.service,self.request)
        self.assertEqual(first,second)
        self.assertTrue(first['workerRequired'])
        self.assertEqual(self.sent,[])
        self.assertEqual(len(dispatch(self.service,{'action':'schedule-list'})['tasks']),1)
        dispatch(self.service,{'action':'schedule-run'})
        dispatch(self.service,{'action':'schedule-run'})
        self.assertEqual(len(self.sent),1)

    def test_known_offline_failure_before_reservation_is_not_submitted(self):
        dispatch(self.service,self.request)
        def offline():raise PluginError('ACCOUNT_NOT_READY','offline')
        self.service.live=offline
        result=dispatch(self.service,{'action':'schedule-run'})
        self.assertEqual(result['runs'][0]['status'],'not_submitted')
        self.assertEqual(self.sent,[])

    def test_terminal_send_status_checks_bound_account_without_live_access(self):
        item={'accountId':'other','status':'confirmed'}
        with self.assertRaises(PluginError) as caught:
            self.service.reconcile('unused',item,None)
        self.assertEqual(caught.exception.code,'ACCOUNT_MISMATCH')

    def test_cancelled_schedule_is_never_dispatched(self):
        dispatch(self.service,self.request)
        dispatch(self.service,{'action':'schedule-update','taskId':self.request['taskId'],'operation':'cancel'})
        dispatch(self.service,{'action':'schedule-run'})
        self.assertEqual(self.sent,[])

    def test_scheduled_file_pins_bytes_and_refuses_changed_content(self):
        source=self.service.data/'example.txt';source.write_text('original')
        request={**self.request,'payload':{'action':'send-file','recipientId':'peer','filePath':str(source)}}
        created=dispatch(self.service,request)
        self.assertEqual(len(created['task']['payload']['fileSha256']),64)
        source.write_text('changed')
        result=dispatch(self.service,{'action':'schedule-run'})
        self.assertEqual(result['runs'][0]['status'],'not_submitted')
        self.assertEqual(result['runs'][0]['result']['error'],'MEDIA_CHANGED')

    def test_lost_creation_response_can_be_recovered_offline_after_file_deletion(self):
        source=self.service.data/'retry.txt';source.write_text('original')
        request={**self.request,'payload':{'action':'send-file','recipientId':'peer','filePath':str(source)}}
        first=dispatch(self.service,request)
        source.unlink()
        def forbidden(*args):raise AssertionError('retry must not read account or contact')
        self.service.live=forbidden;self.service.recipient=forbidden
        self.assertEqual(dispatch(self.service,request),first)
        self.assertEqual(len(dispatch(self.service,{'action':'schedule-list'})['tasks']),1)

    def test_explicit_uppercase_digest_is_idempotent(self):
        source=self.service.data/'digest.txt';source.write_bytes(b'original')
        request={**self.request,'payload':{'action':'send-file','recipientId':'peer','filePath':str(source),
            'fileSha256':hashlib.sha256(source.read_bytes()).hexdigest().upper()}}
        self.assertEqual(dispatch(self.service,request),dispatch(self.service,request))


if __name__=='__main__':unittest.main()
