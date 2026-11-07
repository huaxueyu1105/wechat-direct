import hashlib
import io
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import types
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'plugin/scripts'))
sys.path.insert(0,str(ROOT/'plugin/runtime/python-deps'))
from PIL import Image
from stickers import Stickers,StickerError,normalize
from operations import Operations,OperationConflict,StickerRateLimit
from wechat import WeChat,PluginError,dispatch,sticker_md5


class StickerFixture:
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.folder=Path(self.temp.name)
        self.file=self.folder/'test.png';Image.new('RGBA',(640,320),(20,180,60,255)).save(self.file)
        self.library=Stickers(self.folder/'private','self')
        self.record={'md5':hashlib.md5(self.file.read_bytes()).hexdigest(),'caption':''}

    def tearDown(self):self.temp.cleanup()

    def asset(self,scenes=None):
        self.library.set_favorites([self.record])
        item=self.library.import_favorite(self.file.read_bytes(),self.record,1)
        return self.library.label(item['stickerId'],'测试素材',scenes if scenes is not None else ['casual'])


class PanelStickerTests(StickerFixture,unittest.TestCase):
    def test_builtin_catalog_available_without_a_material_folder(self):
        self.assertEqual(self.library.source_info()['builtinCount'],109)
        all_builtin=[]
        for offset in range(0,109,24):all_builtin.extend(self.library.list(category='builtin',offset=offset,limit=24))
        self.assertEqual(len(all_builtin),109)
        for name in ('捂脸','破涕为笑','合十','微笑'):
            item=self.library.list(query=name,category='builtin')[0]
            self.assertEqual(self.library.get(item['stickerId']),item)
        self.assertEqual(self.library.list(category='favorites'),[])

    def test_folder_and_arbitrary_images_cannot_extend_the_two_panels(self):
        with self.assertRaises(StickerError):self.library.configure_source(str(self.folder),True)
        with self.assertRaises(StickerError):self.library.import_file(str(self.file),'测试',['casual'])
        with self.assertRaises(StickerError):self.library.import_favorite(self.file.read_bytes(),self.record)

    def test_source_md5_must_match_current_added_item(self):
        self.library.set_favorites([self.record])
        with self.assertRaises(StickerError):self.library.import_favorite(b'other bytes',self.record)
        self.assertEqual(self.library.list(category='favorites'),[])

    def test_static_image_converts_without_distortion_and_persists_origin(self):
        item=self.asset()
        self.assertEqual((item['width'],item['height'],item['frames']),(320,160,1))
        self.assertEqual(item['origin'],'wechat_favorite')
        reopened=Stickers(self.folder/'private','self')
        self.assertEqual(reopened.get(item['stickerId']),item)
        self.assertTrue(Path(item['nativePath']).read_bytes().startswith(b'GIF'))
        self.assertEqual(reopened.preview(reopened.list(category='favorites'))['mimeType'],'image/png')

    def test_old_self_created_or_unattributed_assets_cannot_be_reused(self):
        item=self.asset();identity=item['stickerId']
        self.library.items[identity]['origin']='user_created';self.library.save()
        self.assertEqual(self.library.list(category='favorites'),[])
        with self.assertRaises(StickerError):self.library.get(identity)
        with self.assertRaises(StickerError):self.library.label(identity,'名称',['casual'])

    def test_removal_from_added_panel_disables_cached_asset(self):
        item=self.asset();self.library.set_favorites([])
        self.assertEqual(self.library.list(category='favorites'),[])
        with self.assertRaises(StickerError):self.library.get(item['stickerId'])

    def test_other_accounts_assets_are_rejected(self):
        item=self.asset();self.library.items[item['stickerId']]['sourceAccount']='other'
        with self.assertRaises(StickerError):self.library.get(item['stickerId'])

    def test_cached_bytes_must_match_recorded_hash(self):
        item=self.asset();Path(item['nativePath']).write_bytes(b'GIF89a tampered')
        with self.assertRaises(StickerError) as caught:self.library.get(item['stickerId'])
        self.assertEqual(caught.exception.code,'STICKER_CHANGED')

    def test_original_small_animation_and_frame_timing_preserved(self):
        stream=io.BytesIO()
        frames=[Image.new('RGB',(64,48),color) for color in ('red','green','blue')]
        frames[0].save(stream,format='GIF',save_all=True,append_images=frames[1:],duration=[80,150,250],loop=0)
        raw=stream.getvalue();normalized,meta=normalize(raw)
        self.assertEqual(normalized,raw)
        self.assertTrue(meta['animated']);self.assertEqual(meta['frames'],3)

    def test_large_animation_is_resized_with_frames_retained(self):
        stream=io.BytesIO()
        frames=[Image.new('RGB',(640,320),color) for color in ('red','blue')]
        frames[0].save(stream,format='GIF',save_all=True,append_images=frames[1:],duration=[100,200])
        raw,meta=normalize(stream.getvalue())
        self.assertEqual((meta['width'],meta['height'],meta['frames']),(320,160,2))
        with Image.open(io.BytesIO(raw)) as image:
            self.assertEqual(image.info['duration'],100);image.seek(1);self.assertEqual(image.info['duration'],200)

    def test_invalid_or_excessive_images_are_not_imported(self):
        for raw in (b'',b'not an image',b'x'*(10*1024*1024+1)):
            with self.assertRaises(StickerError):normalize(raw)
        stream=io.BytesIO();Image.new('RGB',(4097,1)).save(stream,format='PNG')
        with self.assertRaises(StickerError):normalize(stream.getvalue())

    def test_large_gif_reduces_dimensions_further_without_discarding_animation(self):
        import random
        rng=random.Random(17)
        frames=[Image.frombytes('RGB',(320,160),rng.randbytes(320*160*3)) for _ in range(10)]
        stream=io.BytesIO();frames[0].save(stream,format='GIF',save_all=True,append_images=frames[1:],duration=100)
        with patch('stickers.SEND_LIMIT',180000):raw,meta=normalize(stream.getvalue())
        self.assertLessEqual(len(raw),180000);self.assertLess(meta['width'],320)
        self.assertEqual(meta['frames'],10);self.assertTrue(meta['animated'])


class StickerSendTests(StickerFixture,unittest.TestCase):
    # Tests use temporary fake panel records and native transport; no live account is touched.
    def service(self):
        service=object.__new__(WeChat);service.data=self.folder/'private'
        service.config={'accountId':'self'}
        service.sticker_library=lambda verify_favorites=False:self.library
        service.live=lambda:{'accountId':'self','pid':123,'identity':'test:process'}
        service.recipient=lambda x:x
        service.module=lambda:types.SimpleNamespace(extract_master_key_from_cfg=lambda _: (None,None,'self'))
        self.sent=[];self.calls=[]
        service.rows=lambda *args:list(self.sent)
        def native(recipient,body,job,action):
            self.calls.append(action)
            row={'direction':'outgoing','serverId':str(100+len(self.calls)),
                 'localId':str(len(self.calls)),'text':body,'createdAt':int(time.time()),'typeCode':1}
            if action==5:row.update(text='<msg><emoji/></msg>',typeCode=47,stickerMd5=hashlib.md5(Path(body).read_bytes()).hexdigest())
            self.sent.append(row)
            return {'stage':'submitted'}
        service.native=native;return service

    def seed(self,count=4):
        log=Operations(self.folder/'private/operations.sqlite3')
        try:
            for i in range(count):
                key='seed-operation-'+str(i)
                item,_=log.reserve(key,'self','peer','plain','seed:process',[])
                item['status']='confirmed';log.save(key,item)
        finally:log.close()

    def test_contextual_send_is_rate_limited_atomically_and_replay_never_resends(self):
        item=self.asset();service=self.service()
        with self.assertRaises(StickerRateLimit):service.send_sticker('peer',item['stickerId'],'sticker-operation-001','casual',wait=0)
        self.assertEqual(self.calls,[])
        self.seed()
        result=service.send_sticker('peer',item['stickerId'],'sticker-operation-001','casual',wait=0)
        self.assertEqual(result['status'],'confirmed');self.assertEqual(self.calls,[5])
        self.assertEqual(result['messageType'],47)
        self.assertEqual(service.send_sticker('peer',item['stickerId'],'sticker-operation-001','casual',wait=0),result)
        with self.assertRaises(StickerRateLimit):service.send_sticker('peer',item['stickerId'],'sticker-operation-002','casual',wait=0)
        with self.assertRaises(OperationConflict):service.send_sticker('peer',item['stickerId'],'sticker-operation-001','thanks',wait=0)
        self.assertEqual(self.calls,[5])

    def test_explicit_mode_still_requires_current_panel_membership(self):
        item=self.asset();service=self.service()
        self.library.set_favorites([])
        with self.assertRaises(StickerError):service.send_sticker('peer',item['stickerId'],'explicit-operation-001','casual','explicit',0)
        self.assertEqual(self.calls,[])

    def test_unreviewed_or_wrong_scene_never_submits(self):
        item=self.asset([]);service=self.service();self.seed()
        with self.assertRaises(PluginError):service.send_sticker('peer',item['stickerId'],'unreviewed-operation','casual',wait=0)
        self.library.label(item['stickerId'],'测试',['thanks'])
        with self.assertRaises(PluginError):service.send_sticker('peer',item['stickerId'],'wrong-scene-operation','casual',wait=0)
        self.assertEqual(self.calls,[])

    def test_missing_receipt_blocks_new_key_and_same_key_only_reconciles(self):
        item=self.asset();service=self.service();self.seed();service.rows=lambda *args:[]
        first=service.send_sticker('peer',item['stickerId'],'unknown-operation-001','casual',wait=0)
        self.assertEqual(first['status'],'unknown')
        service.send_sticker('peer',item['stickerId'],'unknown-operation-001','casual',wait=0)
        with self.assertRaises(OperationConflict):service.send_sticker('peer',item['stickerId'],'unknown-operation-002','casual',wait=0)
        self.assertEqual(self.calls,[5])

    def test_receipt_requires_sticker_type_and_exact_md5(self):
        item={'kind':'sticker','sticker':{'md5':'a'*32},'text':''}
        for row in ({'typeCode':1,'stickerMd5':'a'*32},{'typeCode':47,'stickerMd5':'b'*32},{'typeCode':47}):
            self.assertFalse(WeChat.receipt_matches(row,item))
        self.assertTrue(WeChat.receipt_matches({'typeCode':47,'stickerMd5':'a'*32},item))

    def test_md5_parser_handles_xml_but_not_entities_or_placeholders(self):
        self.assertEqual(sticker_md5('<msg><emoji md5="'+'A'*32+'"/></msg>'),'a'*32)
        for content in ('[图片]','<msg/>','<!DOCTYPE x><msg><emoji md5="'+'a'*32+'"/></msg>'):
            self.assertIsNone(sticker_md5(content))

    def test_builtin_and_image_sends_share_one_quota_and_replay(self):
        image=self.asset();service=self.service();self.seed()
        builtin=self.library.list(query='捂脸',category='builtin')[0]
        result=service.send_sticker('peer',builtin['stickerId'],'builtin-operation-001','casual',wait=0)
        self.assertEqual(result['status'],'confirmed');self.assertEqual(result['text'],builtin['text'])
        self.assertEqual(result['messageType'],1);self.assertEqual(self.calls,[2])
        self.assertEqual(service.send_sticker('peer',builtin['stickerId'],'builtin-operation-001','casual',wait=0),result)
        with self.assertRaises(StickerRateLimit):service.send_sticker('peer',image['stickerId'],'image-after-builtin','casual',wait=0)
        self.assertEqual(self.calls,[2])

    def test_removal_between_reservation_and_submission_prevents_native_call(self):
        item=self.asset();service=self.service();self.seed()
        def switched(_):
            self.library.set_favorites([]);return None,None,'self'
        service.module=lambda:types.SimpleNamespace(extract_master_key_from_cfg=switched)
        result=service.send_sticker('peer',item['stickerId'],'removed-during-submit','casual',wait=0)
        self.assertEqual(result['status'],'not_submitted');self.assertEqual(self.calls,[])


class FavoriteSourceTests(unittest.TestCase):
    def test_urls_are_limited_to_client_media_hosts_without_credentials_or_custom_ports(self):
        from favorite_source import source_url
        self.assertEqual(source_url('http://vweixinf.tc.qq.com/a?b=1'),'http://vweixinf.tc.qq.com/a?b=1')
        for url in ('file:///local','https://evil.example/a','https://wxapp.tc.qq.com.evil/a',
                    'https://user:pass@wxapp.tc.qq.com/a','https://wxapp.tc.qq.com:8080/a'):
            with self.assertRaises(StickerError):source_url(url)

    def test_only_added_items_are_selected_from_account_database(self):
        import sqlite3
        from favorite_source import favorite_records
        conn=sqlite3.connect(':memory:');conn.row_factory=sqlite3.Row
        conn.execute('CREATE TABLE kNonStoreEmoticonTable(md5 TEXT,caption TEXT,cdn_url TEXT)')
        conn.execute('CREATE TABLE kFavEmoticonOrderTable(md5 TEXT)')
        for md5 in ('a'*32,'b'*32):conn.execute('INSERT INTO kNonStoreEmoticonTable VALUES (?,?,?)',(md5,'','https://wxapp.tc.qq.com/a'))
        conn.execute('INSERT INTO kFavEmoticonOrderTable VALUES (?)',('b'*32,))
        reader=types.SimpleNamespace(_db_files=[('emoticon/emoticon.db',None,None)],_open=lambda _:conn)
        self.assertEqual([r['md5'] for r in favorite_records(reader)],['b'*32])

    def test_interactive_dice_and_rps_do_not_block_added_images(self):
        import sqlite3
        from favorite_source import favorite_records
        conn=sqlite3.connect(':memory:');conn.row_factory=sqlite3.Row
        conn.execute('CREATE TABLE kNonStoreEmoticonTable(md5 TEXT,caption TEXT,cdn_url TEXT)')
        conn.execute('CREATE TABLE kFavEmoticonOrderTable(md5 TEXT)')
        for identity in ('jsb_emoticon_md5','b'*32,'dice_emoticon_md5'):
            conn.execute('INSERT INTO kNonStoreEmoticonTable VALUES (?,?,?)',(identity,'',''))
            conn.execute('INSERT INTO kFavEmoticonOrderTable VALUES (?)',(identity,))
        reader=types.SimpleNamespace(_db_files=[('emoticon/emoticon.db',None,None)],_open=lambda _:conn)
        self.assertEqual([x['md5'] for x in favorite_records(reader)],['b'*32])

    def test_unrecognized_invalid_favorite_still_fails_closed(self):
        import sqlite3
        from favorite_source import favorite_records
        conn=sqlite3.connect(':memory:');conn.row_factory=sqlite3.Row
        conn.execute('CREATE TABLE kNonStoreEmoticonTable(md5 TEXT,caption TEXT,cdn_url TEXT)')
        conn.execute('CREATE TABLE kFavEmoticonOrderTable(md5 TEXT)')
        conn.execute('INSERT INTO kNonStoreEmoticonTable VALUES (?,?,?)',('unexpected-value','',''))
        conn.execute('INSERT INTO kFavEmoticonOrderTable VALUES (?)',('unexpected-value',))
        reader=types.SimpleNamespace(_db_files=[('emoticon/emoticon.db',None,None)],_open=lambda _:conn)
        with self.assertRaises(StickerError) as caught:favorite_records(reader)
        self.assertEqual(caught.exception.code,'STICKER_SOURCE_INVALID')


if __name__=='__main__':unittest.main()
