import json
from pathlib import Path
import struct
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'plugin/scripts'))
from wechat import WeChat,PluginError
from native_host import NativeError


class ClientSelectionTests(unittest.TestCase):
    def service(self):
        client=object.__new__(WeChat)
        client._live=None
        client.config={'accountId':'self'}
        client.support={'coreSha256':'supported'}
        client.module=lambda:type('DB',(),{'WeChatDB':type('Finder',(),{'_find_weixin_pids':staticmethod(lambda _: [123])}),
                                         'extract_master_key_from_cfg':staticmethod(lambda _:None)})
        return client

    def test_unknown_binary_still_rejects_before_account_extraction(self):
        client=self.service()
        with patch('wechat.modules',return_value=[{'name':'Weixin.dll','path':'core.dll'}]),patch('wechat.digest',return_value='changed'):
            with self.assertRaises(PluginError) as caught:client.live()
        self.assertEqual(caught.exception.code,'CLIENT_UNSUPPORTED')

    def test_supported_binary_without_login_is_not_a_version_failure(self):
        client=self.service()
        with patch('wechat.modules',return_value=[{'name':'Weixin.dll','path':'core.dll'}]),patch('wechat.digest',return_value='supported'):
            with self.assertRaises(PluginError) as caught:client.live()
        self.assertEqual(caught.exception.code,'ACCOUNT_NOT_READY')


class NativePreparationTests(unittest.TestCase):
    def setUp(self):
        # Windows CI may expose TEMP through an 8.3 alias (RUNNER~1).
        # Production resolves the private root before staging the driver.
        self.temp=tempfile.TemporaryDirectory();self.folder=Path(self.temp.name).resolve()
        self.driver=self.folder/'test-driver.dll';self.driver.write_bytes(b'test-only')
        self.calls=[];self.stage='prepared';self.identity='123:creation-one'
        self.client=object.__new__(WeChat)
        self.client.support={'preflightBeforeFirstSubmission':True,'coreSha256':'supported'}
        self.client.stage_driver=lambda sticker=False:self.driver
        self.client.live=lambda:{'pid':123,'identity':self.identity,'corePath':'core.dll'}
        self.patches=[patch('wechat.invoke',side_effect=self.invoke),patch('wechat.modules',return_value=[{'path':str(self.driver)}])]
        for item in self.patches:item.start()
    def tearDown(self):
        for item in reversed(self.patches):item.stop()
        self.temp.cleanup()
    def invoke(self,pid,core,sha,driver,may_send):
        raw=(self.folder/'bridge-request.bin').read_bytes()
        _,action,_,_,job=struct.unpack('<8sIII32s',raw[:52]);self.calls.append(action)
        assert may_send==(action in (2,5))
        report={'pid':123,'job':job.decode(),'stage':self.stage if action in (1,4) else 'submitted',
                'exceptionCode':0,'nativeSubmissionReturned':action in (2,5)}
        (self.folder/'bridge-result.json').write_text(json.dumps(report),encoding='utf8')
    def call(self,action=2):return self.client.native('peer','body','a'*32,action)
    def test_failed_preparation_never_reaches_send(self):
        self.stage='constructed_layout_mismatch'
        with self.assertRaises(NativeError) as caught:self.call()
        self.assertFalse(caught.exception.possible_submission)
        self.assertEqual(self.calls,[1])
    def test_preparation_precedes_send_and_is_cached_for_same_process(self):
        self.call();self.call()
        self.assertEqual(self.calls,[1,2,2])
    def test_restart_revalidates_even_with_same_pid(self):
        self.call();self.identity='123:creation-two';self.call()
        self.assertEqual(self.calls,[1,2,1,2])
    def test_sticker_has_separate_preparation(self):
        self.call();self.call(5)
        self.assertEqual(self.calls,[1,2,4,5])


if __name__=='__main__':unittest.main()
