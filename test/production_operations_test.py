import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'plugin/scripts'))
sys.path.insert(0,str(ROOT/'plugin/runtime/python-deps'))
from operations import Operations,OperationConflict

class OperationTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.path=Path(self.temp.name)/'journal.sqlite3';self.log=Operations(self.path)
    def tearDown(self):self.log.close();self.temp.cleanup()
    def reserve(self,key='operation-001',text='hello',recipient='peer',maximum=1024):
        return self.log.reserve(key,'self',recipient,text,'pid:start',[],maximum)
    def test_same_operation_after_restart_does_not_become_fresh(self):
        item,fresh=self.reserve();self.assertTrue(fresh)
        item.update(status='confirmed',record={'serverId':'9007199254740993'});self.log.save('operation-001',item)
        second=Operations(self.path)
        try:
            repeated,again=second.reserve('operation-001','self','peer','hello','pid:start',[])
            self.assertFalse(again);self.assertEqual(repeated['record']['serverId'],'9007199254740993')
        finally:second.close()
    def test_changed_body_under_same_key_is_rejected(self):
        self.reserve()
        with self.assertRaises(OperationConflict):self.reserve(text='different')
        self.assertEqual(self.log.get('operation-001')['text'],'hello')
    def test_unknown_outcome_blocks_new_key_for_same_conversation(self):
        item,_=self.reserve();item['status']='unknown';self.log.save('operation-001',item)
        with self.assertRaises(OperationConflict):self.reserve(key='operation-002')
    def test_other_conversation_is_independent(self):
        self.reserve();_,fresh=self.reserve(key='operation-002',recipient='other');self.assertTrue(fresh)
    def test_capacity_is_bound_to_process_creation_not_pid_alone(self):
        self.reserve(maximum=1)
        with self.assertRaises(OperationConflict):self.reserve(key='operation-002',recipient='other',maximum=1)
        _,fresh=self.log.reserve('operation-003','self','other','hello','pid:new-start',[],1)
        self.assertTrue(fresh)
    def test_recent_history_preserves_keys_and_filters_account(self):
        self.reserve()
        self.assertEqual(self.log.recent('self')[0]['operationKey'],'operation-001')
        self.assertEqual(self.log.recent('unrelated'),[])

class ReceiptTests(unittest.TestCase):
    def setUp(self):
        from wechat import WeChat
        self.service=object.__new__(WeChat);self.service.live=lambda:{'accountId':'self'}
        self.saved=[]
        self.log=type('Log',(),{'save':lambda _,key,item:self.saved.append(item['status'])})()
        self.item={'operationKey':'operation-001','operationHash':'h','status':'submitted','accountId':'self',
            'recipientId':'peer','text':'hello','startedAt':1000,'baselineServerIds':['10']}
    def row(self,**changes):
        value={'direction':'outgoing','serverId':'11','text':'hello','createdAt':1000,'localId':'2'};value.update(changes);return value
    def test_old_server_record_cannot_confirm(self):
        self.service.rows=lambda *args:[self.row(serverId='10')]
        result=self.service.reconcile('operation-001',self.item,self.log)
        self.assertEqual(result['status'],'unknown')
    def test_incoming_or_different_text_cannot_confirm(self):
        self.service.rows=lambda *args:[self.row(direction='incoming'),self.row(text='unrelated')]
        self.assertEqual(self.service.reconcile('operation-001',self.item,self.log)['status'],'unknown')
    def test_unique_new_outgoing_record_confirms_without_claiming_read(self):
        self.service.rows=lambda *args:[self.row(serverId='9007199254740993')]
        result=self.service.reconcile('operation-001',self.item,self.log)
        self.assertEqual(result['status'],'confirmed');self.assertEqual(result['serverId'],'9007199254740993')
        self.assertEqual(result['readByRecipient'],'unknown')
    def test_multiple_new_records_are_ambiguous(self):
        from wechat import PluginError
        self.service.rows=lambda *args:[self.row(),self.row(serverId='12')]
        with self.assertRaises(PluginError):self.service.reconcile('operation-001',self.item,self.log)

if __name__=='__main__':unittest.main()
