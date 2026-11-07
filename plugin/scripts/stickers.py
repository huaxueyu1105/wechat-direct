"""本机表情库：导入静态图片/动图，统一生成 GIF，缓存及预览均留在本机。"""
import base64
import hashlib
import io
import json
from pathlib import Path
import re
import warnings

from PIL import Image, ImageDraw, ImageOps

SCENES = ('casual', 'greeting', 'thanks', 'celebration')
SOURCE_LIMIT = 10 * 1024 * 1024
SEND_LIMIT = 2 * 1024 * 1024
MAX_EDGE = 320
EXTENSIONS = {'.gif', '.png', '.jpg', '.jpeg', '.webp'}


class StickerError(ValueError):
    def __init__(self, code, message):super().__init__(message);self.code=code


def require(value, code, message):
    if not value:raise StickerError(code, message)


def validate_metadata(label, scenes):
    require(isinstance(label, str) and label.strip() and '\0' not in label and len(label) <= 100,
            'INVALID_STICKER_LABEL', '表情名称需为 1–100 个字符。')
    require(isinstance(scenes, list) and len(scenes) <= 4 and all(x in SCENES for x in scenes),
            'INVALID_STICKER_SCENES', '表情场景仅支持 casual、greeting、thanks、celebration；未审核的表情可留空。')


def normalize(raw):
    require(0 < len(raw) <= SOURCE_LIMIT, 'STICKER_TOO_LARGE', '表情源文件为空或超过 10 MiB。')
    try:
        with warnings.catch_warnings():
            warnings.simplefilter('error', Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(raw)) as image:
                require(image.format in ('GIF', 'PNG', 'JPEG', 'WEBP'), 'STICKER_FORMAT_UNSUPPORTED', '仅支持 GIF、PNG、JPEG、WebP 图片。')
                width,height=image.size;count=getattr(image,'n_frames',1)
                require(0 < width <= 4096 and 0 < height <= 4096 and count <= 160 and width*height*count <= 48000000,
                        'STICKER_DECODE_LIMIT', '表情尺寸或帧数过大，未导入。')
                frames=[];durations=[]
                preserve=image.format == 'GIF' and max(width,height) <= MAX_EDGE and len(raw) <= SEND_LIMIT
                for index in range(count):
                    image.seek(index);image.load()
                    if not preserve:
                        frame=ImageOps.exif_transpose(image).convert('RGBA')
                        frame.thumbnail((MAX_EDGE,MAX_EDGE),Image.Resampling.LANCZOS)
                        frames.append(frame)
                        durations.append(max(40,min(int(image.info.get('duration',100)),1000)))
                if not preserve:
                    # Keep the complete animation, reducing dimensions further for long/complex GIFs.
                    for edge in (MAX_EDGE,256,192,128,96,64):
                        if edge<MAX_EDGE:
                            for frame in frames:frame.thumbnail((edge,edge),Image.Resampling.LANCZOS)
                        out=io.BytesIO()
                        frames[0].save(out,format='GIF',save_all=count>1,append_images=frames[1:],
                                       duration=durations if count>1 else durations[0],loop=0,disposal=2,optimize=True)
                        raw=out.getvalue()
                        if len(raw)<=SEND_LIMIT:break
        require(len(raw) <= SEND_LIMIT, 'STICKER_TOO_LARGE', '处理后的表情超过 2 MiB，未导入。')
        with Image.open(io.BytesIO(raw)) as result:
            metadata={'width':result.width,'height':result.height,'frames':result.n_frames,'animated':result.n_frames>1}
        return raw, metadata
    except StickerError:raise
    except Exception as error:raise StickerError('INVALID_STICKER_IMAGE', '表情图片无法完整解码，未导入。') from error


class Stickers:
    def __init__(self, data, account):
        self.account=account
        self.root=Path(data)/'stickers'/hashlib.sha256(account.encode('utf8')).hexdigest()[:16]
        self.root.mkdir(parents=True,exist_ok=True)
        self.index=self.root/'catalog.json'
        self.items=json.loads(self.index.read_text(encoding='utf-8-sig')) if self.index.exists() else {}
        self.source_file=self.root/'source.json'
        self.source=json.loads(self.source_file.read_text(encoding='utf-8-sig')) if self.source_file.exists() else {}
        catalog=json.loads((Path(__file__).resolve().parents[1]/'runtime/builtin-expressions.json').read_text(encoding='utf8'))
        self.builtins={x['stickerId']:x for x in catalog['items']}
        self.favorite_md5s=set(self.source.get('favoriteMd5s',[])) if self.source.get('accountId')==account else set()

    def configure_source(self, directory, user_created):
        raise StickerError('EXPRESSION_SOURCE_LOCKED','仅使用微信内置表情和当前账号添加的单个表情，不再使用文件夹导入。')

    def set_favorites(self, records):
        md5s=[x['md5'] for x in records]
        require(all(isinstance(x,str) and re.fullmatch('[0-9a-f]{32}',x) for x in md5s),
                'STICKER_SOURCE_INVALID','单个表情目录摘要无效。')
        self.favorite_md5s=set(md5s)
        source={'panels':['wechat_builtin','wechat_favorite'],'accountId':self.account,'favoriteMd5s':list(dict.fromkeys(md5s))}
        if self.source!=source:
            temp=self.source_file.with_suffix('.tmp')
            temp.write_text(json.dumps(source,ensure_ascii=False,indent=2),encoding='utf8');temp.replace(self.source_file)
        self.source=source

    def allowed_origin(self, item):
        if item.get('kind')=='builtin':return item.get('stickerId') in self.builtins and self.builtins[item['stickerId']]==item
        return (item.get('origin')=='wechat_favorite' and item.get('sourceAccount')==self.account and
                item.get('favoriteMd5') in self.favorite_md5s)

    def source_info(self):
        return {'allowedSources':['wechat_builtin','wechat_favorite'],'builtinCount':len(self.builtins),
                'favoriteCount':len(self.favorite_md5s),'importedFavoriteCount':sum(self.allowed_origin(x) for x in self.items.values()),
                'favoritesSynchronized':self.source.get('accountId')==self.account,
                'directoryImportAllowed':False,'stickerStoreAllowed':False,'otherMediaAllowed':False}

    def save(self):
        temp=self.index.with_suffix('.tmp')
        temp.write_text(json.dumps(self.items,ensure_ascii=False,indent=2),encoding='utf8');temp.replace(self.index)

    def import_file(self, path, label, scenes):
        raise StickerError('EXPRESSION_SOURCE_LOCKED','只使用当前账号添加的单个表情，不接受其他本地或网络素材。')

    def import_favorite(self, raw, record, number=0):
        original=record['md5']
        require(original in self.favorite_md5s,'STICKER_NOT_IN_PANEL','此素材不在当前账号的单个表情面板中。')
        require(hashlib.md5(raw).hexdigest()==original,'STICKER_SOURCE_MISMATCH','表情源文件摘要不匹配。')
        raw,metadata=normalize(raw);identity=hashlib.sha256(raw).hexdigest()
        old=self.items.get(identity,{})
        reviewed=self.allowed_origin(old) and old.get('reviewed',False)
        label=old['label'] if reviewed else record.get('caption') or f'单个表情 {number}'
        item={'stickerId':identity,'kind':'sticker','label':label,'scenes':old.get('scenes',[]) if reviewed else [],
              'reviewed':bool(reviewed),'md5':hashlib.md5(raw).hexdigest(),'bytes':len(raw),'mimeType':'image/gif',**metadata,
              'origin':'wechat_favorite','sourceAccount':self.account,'favoriteMd5':original}
        target=self.root/(identity+'.gif')
        if not target.exists():
            temp=target.with_suffix('.tmp');temp.write_bytes(raw);temp.replace(target)
        require(hashlib.sha256(target.read_bytes()).hexdigest()==identity,'STICKER_CHANGED','本机表情内容变化，未使用。')
        self.items[identity]=item;self.save();return self.get(identity)

    def get(self, identity):
        require(isinstance(identity,str),'INVALID_STICKER_ID','请从表情库选择有效 stickerId。')
        if identity in self.builtins:return self.builtins[identity].copy()
        require(re.fullmatch('[0-9a-f]{64}',identity),'INVALID_STICKER_ID','请从表情库选择有效 stickerId。')
        require(identity in self.items,'STICKER_NOT_FOUND','表情不在当前账号的本机表情库中。')
        item=self.items[identity].copy();path=self.root/(identity+'.gif')
        require(self.allowed_origin(item),'STICKER_NOT_IN_PANEL','此素材不在当前账号的单个表情面板中，未使用。')
        require(path.is_file() and path.stat().st_size <= SEND_LIMIT,'STICKER_FILE_UNAVAILABLE','本机表情文件不可用。')
        raw=path.read_bytes()
        require(raw[:6] in (b'GIF87a',b'GIF89a') and hashlib.sha256(raw).hexdigest()==identity and
                hashlib.md5(raw).hexdigest()==item['md5'],'STICKER_CHANGED','本机表情内容变化，未执行发送。')
        item['nativePath']=str(path.resolve());return item

    def label(self, identity, label, scenes):
        validate_metadata(label,scenes);item=self.get(identity)
        require(item.get('kind')!='builtin','BUILTIN_LABEL_FIXED','内置表情使用微信原有名称。')
        self.items[identity].update(label=label,scenes=list(dict.fromkeys(scenes)),reviewed=bool(scenes));self.save()
        return self.get(identity)

    def matching(self, query='', scene=None, category='all'):
        require(isinstance(query,str) and len(query)<=100,'INVALID_ARGUMENT','表情搜索词过长。')
        require(scene is None or scene in (*SCENES,'formal','serious','conflict','condolence'),'INVALID_ARGUMENT','表情场景无效。')
        require(category in ('all','builtin','favorites'),'INVALID_ARGUMENT','表情类别需为 all、builtin 或 favorites。')
        candidates=list(self.builtins.values()) if category!='favorites' else []
        if category!='builtin':
            order={md5:i for i,md5 in enumerate(self.source.get('favoriteMd5s',[]))}
            favorites=sorted((x for x in self.items.values() if self.allowed_origin(x)),
                             key=lambda x:order.get(x['favoriteMd5'],-1),reverse=True)
            candidates.extend(favorites)
        return [x.copy() for x in candidates if (not scene or scene in x['scenes']) and
               (not query or query.casefold() in x['label'].casefold() or query in x['stickerId'] or query.casefold() in x.get('text','').casefold())]

    def list(self, query='', scene=None, limit=12, category='all', offset=0):
        require(isinstance(offset,int) and not isinstance(offset,bool) and offset>=0,'INVALID_ARGUMENT','表情偏移量无效。')
        return self.matching(query,scene,category)[offset:offset+max(1,min(int(limit),24))]

    def preview(self, items):
        items=[x for x in items if x.get('kind')!='builtin']
        if not items:return None
        cells=[]
        for number,item in enumerate(items,1):
            cell=Image.new('RGB',(312,128),'#f2f4f7');draw=ImageDraw.Draw(cell)
            path=self.get(item['stickerId'])['nativePath']
            with Image.open(path) as image:
                frames=sorted(set([0,image.n_frames//2,max(0,image.n_frames-1)]))
                for index,frame in enumerate(frames):
                    image.seek(frame);thumb=image.convert('RGBA');thumb.thumbnail((96,96))
                    cell.paste(thumb,(index*104+(96-thumb.width)//2,(96-thumb.height)//2),thumb)
            draw.text((6,108),f'{number}: {item["stickerId"][:12]}',fill='#202630');cells.append(cell)
        sheet=Image.new('RGB',(624,((len(cells)+1)//2)*128),'white')
        for index,cell in enumerate(cells):sheet.paste(cell,((index%2)*312,(index//2)*128))
        out=io.BytesIO();sheet.save(out,format='PNG')
        return {'mimeType':'image/png','data':base64.b64encode(out.getvalue()).decode('ascii')}
