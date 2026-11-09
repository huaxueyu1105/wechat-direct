"""Persist only schema metadata, and skip unchanged unrelated shards."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import sys
import tempfile
import unittest

ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT/'plugin/scripts'),str(ROOT/'plugin/runtime/python-deps')]
from wechat import WeChat
loader=object.__new__(WeChat)
loader.support={'readerSha256':hashlib.sha256((ROOT/'plugin/runtime/vendor/wechat_db.py').read_bytes()).hexdigest()}
Database=loader.module().WeChatDB


class ShardCacheTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(dir=ROOT.parent);self.root=Path(self.temp.name)
        self.sources={};self.opened=[];owner=self
        class Reader(Database):
            def __init__(self):
                self.workdir=str(owner.root/'cache');self._keys={name:b'synthetic' for name in owner.sources}
            def _message_dbs(self):return list(owner.sources)
            def _db_path(self,rel):return str(owner.sources[rel])
            def _decrypt_file(self,src,dst,key):shutil.copyfile(src,dst)
            def _open(self,rel):owner.opened.append(rel);return super()._open(rel)
        self.Reader=Reader
    def tearDown(self):self.temp.cleanup()
    def add(self,name,user):
        path=self.root/name;connection=sqlite3.connect(path)
        table='Msg_'+hashlib.md5(user.encode()).hexdigest()
        connection.execute('PRAGMA page_size=4096');connection.execute('CREATE TABLE '+table+'(id INTEGER)')
        connection.commit();connection.close();self.sources[name]=path
        return path
    def read(self,reader,user='target'):
        found=reader._msg_conns(user)
        count=len(found)
        for connection,_ in found:connection.close()
        return count
    def test_new_reader_skips_unchanged_unrelated_shard(self):
        self.add('one.db','target');self.add('two.db','other')
        self.assertEqual(self.read(self.Reader()),1);self.assertEqual(self.opened,['one.db','two.db'])
        self.opened.clear()
        self.assertEqual(self.read(self.Reader()),1);self.assertEqual(self.opened,['one.db'])
    def test_changed_shard_and_new_shard_are_discovered(self):
        self.add('one.db','target');path=self.add('two.db','other')
        self.read(self.Reader());self.opened.clear()
        connection=sqlite3.connect(path)
        connection.execute('CREATE TABLE Msg_'+hashlib.md5(b'target').hexdigest()+'(id INTEGER)')
        connection.commit();connection.close()
        info=path.stat();os.utime(path,ns=(info.st_atime_ns,info.st_mtime_ns+1000000000))
        self.add('three.db','target')
        self.assertEqual(self.read(self.Reader()),3)
        self.assertEqual(self.opened,['one.db','two.db','three.db'])
    def test_unrelated_plaintext_cache_can_be_evicted_without_forcing_rescan(self):
        self.add('one.db','target');self.add('two.db','other')
        self.read(self.Reader())
        (self.root/'cache/two.db').unlink();(self.root/'cache/two.db.stamp').unlink()
        self.opened.clear();self.assertEqual(self.read(self.Reader()),1)
        self.assertEqual(self.opened,['one.db'])
    def test_stale_fallback_does_not_create_negative_schema_entry(self):
        path=self.add('one.db','other');self.read(self.Reader())
        connection=sqlite3.connect(path)
        connection.execute('CREATE TABLE Msg_'+hashlib.md5(b'target').hexdigest()+'(id INTEGER)')
        connection.commit();connection.close()
        info=path.stat();os.utime(path,ns=(info.st_atime_ns,info.st_mtime_ns+1000000000))
        reader=self.Reader()
        reader._open=lambda rel:sqlite3.connect(self.root/'cache'/rel)
        self.assertEqual(self.read(reader),0)
        index=json.loads((self.root/'cache.tables.json').read_text())
        self.assertNotIn('one.db',index['shards'])
        self.assertEqual(self.read(self.Reader()),1)


if __name__=='__main__':unittest.main()
