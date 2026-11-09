import hashlib
from pathlib import Path
import sys
import tempfile
import time
import types
import unittest
from unittest.mock import patch

from PIL import Image

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'plugin/scripts'))
from media import MediaError,inspect_file,stage_file,verify_staged,receipt_metadata
from reply_policy import decision
from wechat import WeChat,PluginError


class MediaTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.root=Path(self.temp.name).resolve()
        self.picture=self.root/'截图.png'
        Image.new('RGB',(640,320),'red').save(self.picture)
        self.file=self.root/'资料.pdf'
        self.file.write_bytes(b'%PDF-test-only')
        self.calls=[];self.rows=[]
        self.client=object.__new__(WeChat)
        self.client.data=self.root/'state';self.client.data.mkdir()
        self.client.config={'accountId':'self'}
        self.client.support={'mediaCapabilities':{'image':True,'file':True}}
        self.client.live=lambda:{'accountId':'self','pid':123,'identity':'fake:process'}
        self.client.recipient=lambda value:value
        self.client.module=lambda:types.SimpleNamespace(extract_master_key_from_cfg=lambda _:(None,None,'self'))
        self.client.rows=lambda *args:list(self.rows)
        def native(recipient,path,job,action):
            self.calls.append(action)
            raw=Path(path).read_bytes()
            row={'direction':'outgoing','serverId':str(100+len(self.calls)),'localId':str(len(self.calls)),
                 'text':'[media]','createdAt':int(time.time()),'typeCode':3 if action==7 else 49,
                 'mediaMd5':hashlib.md5(raw).hexdigest()}
            if action==9:row.update(appType=6,fileName=Path(path).name,fileBytes=len(raw))
            self.rows.append(row)
            return {'stage':'submitted'}
        self.client.native=native

    def tearDown(self):self.temp.cleanup()

    def test_normal_image_preserves_bytes_dimensions_and_type(self):
        asset=stage_file(self.client.data,'self',str(self.picture),'image')
        self.assertEqual(Path(asset['nativePath']).read_bytes(),self.picture.read_bytes())
        self.assertEqual((asset['width'],asset['height']),(640,320))
        result=self.client.send_media('image','peer',str(self.picture),'image-operation-001',0)
        self.assertEqual(result['status'],'confirmed')
        self.assertEqual(result['messageType'],3)
        self.assertEqual(self.calls,[7])
        self.assertNotIn('nativePath',result['attachment'])

    def test_file_preserves_filename_and_replay_after_original_removal(self):
        result=self.client.send_media('file','peer',str(self.file),'file-operation-001',0)
        self.file.unlink()
        replay=self.client.send_media('file','peer',str(self.file),'file-operation-001',0)
        self.assertEqual(result,replay)
        self.assertEqual(result['attachment']['fileName'],'资料.pdf')
        self.assertEqual(self.calls,[9])

    def test_changed_target_or_path_cannot_reuse_operation(self):
        self.client.send_media('file','peer',str(self.file),'file-operation-002',0)
        for recipient,path in [('other',self.file),('peer',self.picture)]:
            with self.assertRaises(PluginError) as caught:
                self.client.send_media('file',recipient,str(path),'file-operation-002',0)
            self.assertEqual(caught.exception.code,'OPERATION_CONFLICT')
        self.assertEqual(self.calls,[9])

    def test_missing_native_capability_fails_before_submission(self):
        self.client.support={'mediaCapabilities':{'image':True,'file':False}}
        with self.assertRaises(PluginError) as caught:
            self.client.send_media('file','peer',str(self.file),'file-unavailable',0)
        self.assertEqual(caught.exception.code,'MEDIA_NATIVE_UNAVAILABLE')
        self.assertEqual(self.calls,[])

    def test_changed_scheduled_file_fails_before_native_call(self):
        original=inspect_file(str(self.file),'file')['sha256']
        self.file.write_bytes(b'different content')
        with self.assertRaises(MediaError) as caught:
            self.client.send_media('file','peer',str(self.file),'changed-file-001',0,original)
        self.assertEqual(caught.exception.code,'MEDIA_CHANGED')
        self.assertEqual(self.calls,[])

    def test_send_copy_is_independent_of_original_and_tampering_is_detected(self):
        asset=stage_file(self.client.data,'self',str(self.file),'file')
        self.file.write_bytes(b'changed original')
        self.assertEqual(verify_staged(asset),asset['nativePath'])
        Path(asset['nativePath']).write_bytes(b'tampered copy')
        with self.assertRaises(MediaError):verify_staged(asset)

    def test_invalid_paths_formats_and_size_are_rejected(self):
        for path in ['relative.pdf','https://example.com/file.pdf',str(self.root),'\\\\server\\share\\file']:
            with self.assertRaises(MediaError):inspect_file(path,'file')
        gif=self.root/'animation.gif';Image.new('RGB',(8,8)).save(gif)
        with self.assertRaises(MediaError):inspect_file(str(gif),'image')
        with patch.dict('media.LIMITS',{'file':4}):
            with self.assertRaises(MediaError):inspect_file(str(self.file),'file')

    def test_receipt_parser_distinguishes_media_and_rejects_entities(self):
        md5='a'*32
        self.assertEqual(receipt_metadata('<msg><img md5="'+md5+'"/></msg>',3),{'mediaMd5':md5})
        xml='<msg><appmsg><type>6</type><title>资料.pdf</title><appattach><totallen>42</totallen><md5>'+md5+'</md5></appattach></appmsg></msg>'
        result=receipt_metadata(xml,49)
        self.assertEqual((result['fileName'],result['fileBytes'],result['appType']),('资料.pdf',42,6))
        self.assertEqual(receipt_metadata(xml.replace('<type>6','<type>5'),49),{})
        self.assertEqual(receipt_metadata('<!DOCTYPE x>'+xml,49),{})
        for invalid_length in ('9'*5000, '\u00b2', '-1'):
            self.assertEqual(receipt_metadata(xml.replace('>42<','>'+invalid_length+'<'),49),{})
        item={'kind':'file','attachment':{'fileName':'资料.pdf','bytes':42,'md5':md5}}
        self.assertTrue(WeChat.receipt_matches({'typeCode':49,**result},item))
        self.assertFalse(WeChat.receipt_matches({'typeCode':49,**result,'fileBytes':43},item))

    def test_missing_media_digest_stays_unknown_and_never_resends(self):
        self.client.native=lambda *args:self.calls.append(args[-1]) or {'stage':'submitted'}
        result=self.client.send_media('image','peer',str(self.picture),'unknown-image-001',0)
        self.assertEqual(result['status'],'unknown')
        self.client.send_media('image','peer',str(self.picture),'unknown-image-001',0)
        self.assertEqual(self.calls,[7])

    def test_ordinary_attachments_do_not_earn_plain_text_expression_spacing(self):
        self.assertFalse(decision('casual',[{'kind':'file','text':''}]+[{'text':'plain'}]*18)['useExpression'])
        self.assertTrue(decision('casual',[{'text':'plain'}]*4+[{'kind':'image','text':''}]*15)['useExpression'])


if __name__=='__main__':unittest.main()
