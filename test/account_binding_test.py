"""Account selection is explicit and cannot silently change database ownership."""
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'plugin/scripts'))
from wechat import WeChat,PluginError


class AccountBindingTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(dir=ROOT.parent);self.folder=Path(self.temp.name)
        self.identities={10:'id_a',20:'id_b'};self.aliases={'id_a':'alias-a','id_b':'alias-b'}
        self.directories=[str(self.folder/'id_a_1234'),str(self.folder/'id_b_5678')]
        owner=self
        class Reader:
            def __init__(self,db_dir,account,workdir):
                self.account=account;self.workdir=workdir
                self._db_files=self._collect_db_files()
            def _collect_db_files(self):return [('contact.db','unused',1),('message/message_0.db','unused',1)]
            @staticmethod
            def _find_weixin_pids(_):return [10,20]
            def _open(self,rel):
                conn=sqlite3.connect(':memory:')
                conn.execute('CREATE TABLE contact(username,alias)')
                identity=self.account.rsplit('_',1)[0]
                conn.execute('INSERT INTO contact VALUES(?,?)',(identity,owner.aliases[identity]))
                return conn
        self.db=types.SimpleNamespace(WeChatDB=Reader,auto_detect_db_dir=lambda:str(self.folder),
            _find_account_dirs=lambda base:self.directories,
            extract_master_key_from_cfg=lambda pid:('synthetic',0,self.identities[pid]))
        self.client=object.__new__(WeChat);self.client.data=self.folder/'state';self.client.data.mkdir()
        self.client.config_path=self.client.data/'account.json'
        self.client.config={'accountId':'id_a','pid':10};self.original=json.dumps(self.client.config)
        self.client.config_path.write_text(self.original,encoding='utf8')
        self.client._live=None;self.client._reader=None
        self.client.module=lambda:self.db;self.client.support={'coreSha256':'supported'}
        self.client.status=lambda:{'accountId':self.client.config['accountId'],'pid':self.client.config['pid']}
        self.patches=[patch('wechat.modules',return_value=[{'name':'Weixin.dll','path':'synthetic.dll'}]),
                      patch('wechat.digest',return_value='supported'),patch('wechat.process_identity',side_effect=lambda pid:str(pid))]
        for item in self.patches:item.start()
    def tearDown(self):
        for item in reversed(self.patches):item.stop()
        self.temp.cleanup()
    def test_alias_selects_requested_account_among_multiple_processes(self):
        self.assertEqual(self.client.bind('alias-b'),{'accountId':'id_b','pid':20})
        self.assertEqual(json.loads(self.client.config_path.read_text())['accountId'],'id_b')
        self.assertEqual(self.client._reader.account,'id_b_5678')
    def test_unknown_alias_preserves_previous_binding(self):
        with self.assertRaises(PluginError):self.client.bind('missing')
        self.assertEqual(self.client.config_path.read_text(),self.original)
        self.assertEqual(self.client.config['accountId'],'id_a')
    def test_database_ambiguity_preserves_previous_binding(self):
        self.directories.append(str(self.folder/'id_b_abcd'))
        with self.assertRaises(PluginError):self.client.bind('id_b')
        self.assertEqual(self.client.config_path.read_text(),self.original)
    def test_exact_id_does_not_open_unrelated_broken_account(self):
        self.directories=[str(self.folder/'id_b_5678')]
        with patch.object(self.client,'account_reader',wraps=self.client.account_reader) as read:
            self.assertEqual(self.client.bind('id_b')['accountId'],'id_b')
        self.assertEqual([call.args[1]['accountId'] for call in read.call_args_list],['id_b'])
    def test_alias_skips_an_unrelated_unreadable_database(self):
        original=self.client.account_reader
        def reader(db,live,**kwargs):
            if live['accountId']=='id_a':raise sqlite3.DatabaseError('synthetic unreadable database')
            return original(db,live,**kwargs)
        self.client.account_reader=reader
        self.assertEqual(self.client.bind('alias-b')['accountId'],'id_b')
    def test_unreadable_target_database_preserves_previous_binding(self):
        with patch.object(self.db.WeChatDB,'_open',side_effect=sqlite3.DatabaseError('synthetic unreadable database')):
            with self.assertRaises(PluginError) as caught:self.client.bind('id_b')
        self.assertEqual(caught.exception.code,'DATABASE_NOT_READY')
        self.assertEqual(self.client.config_path.read_text(),self.original)
        self.assertEqual(self.client.config['accountId'],'id_a')
    def test_database_without_self_record_preserves_previous_binding(self):
        original=self.db.WeChatDB._open
        def open_without_self(reader,rel):
            conn=original(reader,rel);conn.execute('DELETE FROM contact');return conn
        with patch.object(self.db.WeChatDB,'_open',open_without_self):
            with self.assertRaises(PluginError) as caught:self.client.bind('id_b')
        self.assertEqual(caught.exception.code,'DATABASE_ACCOUNT_MISMATCH')
        self.assertEqual(self.client.config_path.read_text(),self.original)
    def test_reader_is_confined_to_selected_pid_account_and_cache(self):
        live={'pid':20,'accountId':'id_b'}
        reader=self.client.account_reader(self.db,live,contacts_only=True)
        self.assertEqual(reader._find_weixin_pids(),[20])
        self.assertEqual([row[0] for row in reader._db_files],['contact.db'])
        self.assertFalse(reader._try_other_accounts())
        self.assertFalse(reader._select_account_by_keys([]))
        other=self.client.account_reader(self.db,{'pid':10,'accountId':'id_a'})
        self.assertNotEqual(reader._stable_key_dirs(),other._stable_key_dirs())
        self.assertTrue(Path(reader.workdir).is_relative_to(self.client.data))
    def test_unbound_multiple_accounts_require_explicit_selection(self):
        self.client.config={}
        with self.assertRaises(PluginError) as caught:self.client.bind()
        self.assertEqual(caught.exception.code,'ACCOUNT_AMBIGUOUS')
        self.assertEqual(self.client.config_path.read_text(),self.original)


if __name__=='__main__':unittest.main()
