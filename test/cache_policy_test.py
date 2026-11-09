import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'plugin/scripts'))
import cache_policy


class CachePolicyTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(dir=ROOT.parent);self.root=Path(self.temp.name).resolve()
        self.now=time.time();cache_policy.configure(self.root,1,7)
    def tearDown(self):self.temp.cleanup()
    def file(self,name,size=20,age=0):
        path=self.root/name;path.parent.mkdir(parents=True,exist_ok=True)
        path.write_bytes(b'x'*size);os.utime(path,(self.now-age,self.now-age));return path
    def test_quota_evicts_oldest_database_with_stamp(self):
        old=self.file('accounts/a/db/old.db',700000,age=60)
        stamp=self.file('accounts/a/db/old.db.stamp',20,age=60)
        recent=self.file('accounts/a/db/recent.db',700000)
        result=cache_policy.clean(self.root,now=self.now)
        self.assertFalse(old.exists());self.assertFalse(stamp.exists());self.assertTrue(recent.exists())
        self.assertFalse(result['overLimit'])
    def test_expired_cache_is_removed_without_touching_originals_or_journals(self):
        old=self.file('db/old.db',age=8*86400)
        keep=[self.file(name) for name in ('original.txt','account.json','schedules.sqlite3','native/driver.dll')]
        cache_policy.clean(self.root,now=self.now)
        self.assertFalse(old.exists());self.assertTrue(all(path.exists() for path in keep))
    def test_unknown_and_manually_closed_attachments_stay_protected(self):
        unknown=self.file('media/a/one/unknown.txt',1100000)
        closed=self.file('media/a/two/closed.txt',20)
        finished=self.file('media/a/three/finished.txt',20)
        connection=sqlite3.connect(self.root/'operations.sqlite3')
        connection.execute('CREATE TABLE operations(status,body)')
        for status,path in [('unknown',unknown),('closed',closed),('confirmed',finished)]:
            connection.execute('INSERT INTO operations VALUES(?,?)',(status,json.dumps({'attachment':{'nativePath':str(path)}})))
        connection.commit();connection.close()
        result=cache_policy.clean(self.root,purge=True)
        self.assertTrue(unknown.exists());self.assertTrue(closed.exists());self.assertFalse(finished.exists())
        self.assertTrue(result['overLimit'])
        self.assertTrue((self.root/'operations.sqlite3').exists())
    def test_invalid_journal_protects_all_attachments(self):
        asset=self.file('media/a/one/file.txt')
        self.file('operations.sqlite3')
        result=cache_policy.clean(self.root,purge=True)
        self.assertTrue(asset.exists());self.assertTrue(result['pendingAttachmentsUnknown'])
    def test_key_removal_requires_explicit_option_and_preserves_binding(self):
        keys=self.file('accounts/a/keys/derived.json')
        local_keys=self.file('accounts/a/db/keys.json')
        binding=self.file('account.json')
        cache_policy.clean(self.root,purge=True)
        self.assertTrue(keys.exists());self.assertTrue(local_keys.exists())
        cache_policy.clean(self.root,purge=True,include_keys=True)
        self.assertFalse(keys.exists());self.assertFalse(local_keys.exists());self.assertTrue(binding.exists())
    def test_active_database_is_protected_even_over_quota(self):
        opened=self.file('db/open.db',1100000)
        result=cache_policy.clean(self.root,purge=True,keep_paths=[opened])
        self.assertTrue(opened.exists());self.assertTrue(result['overLimit'])
    def test_linked_cache_directory_is_never_followed(self):
        protected=self.file('db/keep.db')
        original=Path.is_junction
        with patch.object(Path,'is_junction',lambda path:path==self.root/'db' or original(path)):
            cache_policy.clean(self.root,purge=True)
        self.assertTrue(protected.exists())
    def test_invalid_policy_is_rejected_before_deleting_files(self):
        old=self.file('db/old.db',age=8*86400)
        with self.assertRaises(cache_policy.CacheError):cache_policy.configure(self.root,0,7)
        self.assertTrue(old.exists())
    def test_shard_cleanup_cannot_remove_attachment_before_it_is_reserved(self):
        from wechat import WeChat
        source=self.file('original.txt',2*1024*1024)
        client=object.__new__(WeChat);client.data=self.root
        client.recipient=lambda value:value;client.live=lambda:{'accountId':'self','identity':'synthetic'}
        client.support={'mediaCapabilities':{'file':True}}
        def rows(*args):
            cache_policy.clean(self.root)
            return []
        client.rows=rows
        def submit(key,item,log,wait):
            self.assertTrue(Path(item['attachment']['nativePath']).is_file())
            cache_policy.clean(self.root,purge=True)
            self.assertTrue(Path(item['attachment']['nativePath']).is_file())
            return {'status':'synthetic-no-send'}
        client.submit=submit
        self.assertEqual(client.send_media('file','peer',str(source),'stable-cache-test')['status'],'synthetic-no-send')


if __name__=='__main__':unittest.main()
