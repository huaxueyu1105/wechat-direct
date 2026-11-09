"""Local image/file inputs, immutable send copies, and conservative receipt matching."""
import hashlib
import os
from pathlib import Path
import re
import tempfile
import warnings
import xml.etree.ElementTree as ET

from PIL import Image

LIMITS={'image':20*1024*1024,'file':100*1024*1024}


class MediaError(ValueError):
    def __init__(self,code,message):super().__init__(message);self.code=code


def require(value,code,message):
    if not value:raise MediaError(code,message)


def local_file(value):
    require(isinstance(value,str) and value and '\0' not in value,'INVALID_MEDIA_PATH','请提供本地文件绝对路径。')
    path=Path(value)
    require(path.is_absolute() and not value.startswith(('\\\\','//')),
            'INVALID_MEDIA_PATH','仅接受本机磁盘的绝对路径，不接受网址或网络共享。')
    path=path.resolve()
    require(not str(path).startswith(('\\\\','//')),'INVALID_MEDIA_PATH','解析后的路径不是本机磁盘文件。')
    require(path.is_file(),'MEDIA_FILE_UNAVAILABLE','文件不存在或不是普通文件。')
    return path


def inspect_file(value,kind,expected_sha256=None):
    require(kind in LIMITS,'INVALID_MEDIA_KIND','消息类型需为 image 或 file。')
    if expected_sha256 is not None:
        require(isinstance(expected_sha256,str) and bool(re.fullmatch('[0-9a-fA-F]{64}',expected_sha256)),
                'INVALID_MEDIA_DIGEST','fileSha256 必须为 SHA-256。')
    path=local_file(value)
    before=path.stat()
    require(0<before.st_size<=LIMITS[kind],'MEDIA_TOO_LARGE','文件为空或超过插件限制。')
    sha,md5=hashlib.sha256(),hashlib.md5()
    count=0
    with path.open('rb') as stream:
        while chunk:=stream.read(1024*1024):
            count+=len(chunk)
            require(count<=LIMITS[kind],'MEDIA_TOO_LARGE','文件超过插件限制。')
            sha.update(chunk);md5.update(chunk)
    after=path.stat()
    require((before.st_size,before.st_mtime_ns)==(after.st_size,after.st_mtime_ns) and count==before.st_size,
            'MEDIA_CHANGED','读取期间文件发生变化，未发送。')
    checksum=sha.hexdigest()
    require(expected_sha256 is None or checksum==expected_sha256.lower(),'MEDIA_CHANGED','文件内容已变化，未发送。')
    result={'kind':kind,'sourcePath':str(path),'fileName':path.name,'bytes':count,'sha256':checksum,'md5':md5.hexdigest()}
    if kind=='image':
        try:
            with warnings.catch_warnings():
                warnings.simplefilter('error',Image.DecompressionBombWarning)
                with Image.open(path) as image:
                    require(image.format in ('PNG','JPEG'),'IMAGE_FORMAT_UNSUPPORTED','普通图片目前接受 PNG 或 JPEG；其他格式可作为文件发送。')
                    require(getattr(image,'n_frames',1)==1 and 0<image.width*image.height<=40000000,
                            'IMAGE_DECODE_LIMIT','普通图片需为不超过四千万像素的静态图。')
                    image.verify()
                    result.update(format=image.format,width=image.width,height=image.height)
        except MediaError:raise
        except Exception as error:raise MediaError('INVALID_IMAGE','图片无法完整解码。') from error
    return result


def stage_file(data,account,value,kind,expected_sha256=None):
    item=inspect_file(value,kind,expected_sha256)
    root=Path(data)/'media'/hashlib.sha256(account.encode()).hexdigest()[:16]/item['sha256']
    root.mkdir(parents=True,exist_ok=True)
    destination=root/item['fileName']
    if not destination.exists():
        fd,name=tempfile.mkstemp(prefix='.copy-',dir=root)
        temporary=Path(name)
        try:
            with os.fdopen(fd,'wb') as output,Path(item['sourcePath']).open('rb') as source:
                total=0
                while chunk:=source.read(1024*1024):
                    total+=len(chunk)
                    require(total<=LIMITS[kind],'MEDIA_TOO_LARGE','文件超过插件限制。')
                    output.write(chunk)
                output.flush();os.fsync(output.fileno())
            check=inspect_file(str(temporary),kind,item['sha256'])
            require(check['bytes']==item['bytes'],'MEDIA_CHANGED','复制期间文件发生变化，未发送。')
            temporary.replace(destination)
        finally:
            if temporary.exists():temporary.unlink()
    item['nativePath']=str(destination.resolve())
    verify_staged(item)
    return item


def verify_staged(item):
    check=inspect_file(item['nativePath'],item['kind'],item['sha256'])
    require(check['bytes']==item['bytes'] and check['md5']==item['md5'],
            'MEDIA_CHANGED','待发送的文件副本发生变化，未发送。')
    return item['nativePath']


def receipt_metadata(content,type_code):
    if not isinstance(content,str) or len(content)>100000 or '<!DOCTYPE' in content.upper() or '<!ENTITY' in content.upper():return {}
    if type_code==3:
        match=re.fullmatch(r'\[图片 md5=([0-9a-fA-F]{32})\]',content)
        if match:return {'mediaMd5':match[1].lower()}
    try:root=ET.fromstring(content)
    except ET.ParseError:return {}
    if type_code==3:
        node=root if root.tag=='img' else root.find('.//img')
        value=node.get('md5','') if node is not None else ''
        return {'mediaMd5':value.lower()} if re.fullmatch('[0-9a-fA-F]{32}',value) else {}
    if type_code==49:
        node=root if root.tag=='appmsg' else root.find('.//appmsg')
        if node is None or node.findtext('type')!='6':return {}
        value=node.findtext('appattach/md5') or node.findtext('md5') or ''
        length=node.findtext('appattach/totallen') or ''
        if not re.fullmatch('[0-9a-fA-F]{32}',value) or not re.fullmatch('[0-9]{1,12}',length):return {}
        return {'mediaMd5':value.lower(),'fileName':node.findtext('title'),'fileBytes':int(length),'appType':6}
    return {}


def receipt_matches(row,item):
    asset=item['attachment']
    if row.get('mediaMd5')!=asset['md5']:return False
    if item['kind']=='image':return row.get('typeCode')==3
    return (row.get('typeCode')==49 and row.get('appType')==6 and
            row.get('fileName')==asset['fileName'] and row.get('fileBytes')==asset['bytes'])
