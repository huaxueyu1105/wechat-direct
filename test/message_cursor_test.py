"""Cursor regression tests over real SQLite queries and synthetic messages only."""
import base64
import hashlib
import json
from pathlib import Path
import sqlite3
import sys
import unittest

ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT/'plugin/scripts'),str(ROOT/'plugin/runtime/python-deps')]
from wechat import WeChat,PluginError

loader=object.__new__(WeChat)
loader.support={'readerSha256':hashlib.sha256((ROOT/'plugin/runtime/vendor/wechat_db.py').read_bytes()).hexdigest()}
Database=loader.module().WeChatDB


class MessageCursorTests(unittest.TestCase):
    def setUp(self):
        self.connections=[];self.tables=[];self.names={}
        owner=self
        class Reader(Database):
            def __init__(self):pass
            def _run_msg_query(self,recipient,build):return build(owner.tables)
            def _shard_label(self,connection):return owner.names[id(connection)]
            def _shard_maps(self,tables):return {}
            def _msg_row_to_dict(self,row,maps):
                return {'sort_seq':row['sort_seq'],'shard':row['shard'],'sender_username':'peer',
                        'content':row['message_content'],'type':'text','create_time':row['create_time']}
        self.reader=Reader()
        self.client=object.__new__(WeChat)
        self.client.live=lambda:{'accountId':'self'}
        self.client.reader=lambda:self.reader
        self.client.recipient=lambda value:value
    def tearDown(self):
        for connection in self.connections:connection.close()
    def shard(self,name,count,sequence=100):
        connection=sqlite3.connect(':memory:');connection.row_factory=sqlite3.Row
        connection.execute('CREATE TABLE messages(local_id INTEGER PRIMARY KEY,local_type,real_sender_id,create_time,'
                           'message_content,source,packed_info_data,compress_content,server_id,sort_seq)')
        connection.executemany('INSERT INTO messages VALUES(?,1,1,1000,?,NULL,NULL,NULL,?,?)',
                               [(i,'synthetic',str(i),sequence) for i in range(1,count+1)])
        self.connections.append(connection);self.names[id(connection)]=name;self.tables.append((connection,'messages'))
        return connection
    def drain(self,cursor=None,limit=2):
        cursor=cursor or self.client.cursor('peer',[]);found=[]
        for _ in range(100):
            page=self.client.poll('peer',cursor,timeout=0,limit=limit)
            cursor=page['cursor'];found.extend((r['shard'],r['localId']) for r in page['messages'])
            if page['timedOut']:return found,cursor
        self.fail('Cursor failed to finish')
    def test_same_sequence_crosses_page_boundary_without_loss_or_duplicates(self):
        self.shard('one',5)
        found,_=self.drain()
        self.assertEqual(found,[('one',str(i)) for i in range(1,6)])
    def test_equal_local_ids_in_different_shards_are_independent(self):
        self.shard('b',3);self.shard('a',3);self.shard('c',1,101)
        found,_=self.drain()
        self.assertEqual(found,[(s,str(i)) for s in ('a','b') for i in range(1,4)]+[('c','1')])
    def test_legacy_cursor_continues_from_seen_boundary(self):
        self.shard('one',5)
        old={'account':'self','recipient':'peer','sortSeq':'100','seen':['one:1','one:2']}
        encoded=base64.urlsafe_b64encode(json.dumps(old).encode()).decode()
        found,_=self.drain(encoded)
        self.assertEqual(found,[('one',str(i)) for i in range(3,6)])
    def test_new_row_at_same_sequence_is_returned_after_timeout(self):
        connection=self.shard('one',2)
        _,cursor=self.drain()
        connection.execute("INSERT INTO messages VALUES(3,1,1,1000,'later',NULL,NULL,NULL,'3',100)")
        found,_=self.drain(cursor)
        self.assertEqual(found,[('one','3')])
    def test_cursor_size_does_not_grow_with_tied_message_count(self):
        self.shard('one',1000)
        found,cursor=self.drain(limit=200)
        self.assertEqual(len(found),1000);self.assertLess(len(cursor),1000)
    def test_cursor_remains_bound_to_account_and_recipient(self):
        cursor=self.client.cursor('peer',[])
        with self.assertRaises(PluginError):self.client.decode_cursor(cursor,'other')
        self.client.live=lambda:{'accountId':'other'}
        with self.assertRaises(PluginError):self.client.decode_cursor(cursor,'peer')


if __name__=='__main__':unittest.main()
