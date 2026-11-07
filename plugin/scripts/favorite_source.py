"""Read only the current account's individually added stickers; never search for other media."""
import hashlib
from pathlib import Path
import re
import time
import urllib.parse
import urllib.request

from stickers import StickerError,SOURCE_LIMIT,require

HOSTS=frozenset(('wxapp.tc.qq.com','vweixinf.tc.qq.com','mmbiz.qpic.cn','emoji.qpic.cn','res.wx.qq.com'))
# 微信客户端的交互入口没有图片 MD5 或素材地址，不作为添加的图片素材导入。
INTERACTIVE_ENTRIES=frozenset(('jsb_emoticon_md5','dice_emoticon_md5'))


def source_url(value):
    require(isinstance(value,str) and 0<len(value)<8192,'STICKER_SOURCE_UNSUPPORTED','微信表情来源地址无效。')
    try:
        parsed=urllib.parse.urlsplit(value)
        allowed=(parsed.scheme in ('http','https') and parsed.hostname in HOSTS and
                 parsed.username is None and parsed.password is None and parsed.port in (None,80,443))
    except ValueError:allowed=False
    require(allowed,'STICKER_SOURCE_UNSUPPORTED','仅读取当前账号单个表情记录指向的微信素材服务。')
    # Retain the actual client record's protocol. Some legacy WeChat CDN names have no matching TLS certificate.
    return urllib.parse.urlunsplit((parsed.scheme,parsed.hostname,parsed.path,parsed.query,''))


class SourceRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self,request,fp,code,msg,headers,newurl):
        return super().redirect_request(request,fp,code,msg,headers,source_url(newurl))


def favorite_records(reader):
    rel=next((rel for rel,_,_ in reader._db_files if str(rel).lower().endswith('emoticon.db')),None)
    require(rel is not None,'STICKER_DATABASE_UNAVAILABLE','当前账号尚无可读取的单个表情数据库。')
    conn=reader._open(rel)
    try:
        # This is the heart tab, not all images in chat history or installed sticker-store packs.
        rows=[dict(x) for x in conn.execute('SELECT n.md5,n.caption,n.cdn_url FROM kNonStoreEmoticonTable n '
              'JOIN kFavEmoticonOrderTable f ON n.md5=f.md5 ORDER BY f.rowid')]
    finally:conn.close()
    rows=[x for x in rows if x['md5'] not in INTERACTIVE_ENTRIES]
    require(all(isinstance(x['md5'],str) and re.fullmatch('[0-9a-f]{32}',x['md5']) for x in rows),
            'STICKER_SOURCE_INVALID','单个表情目录摘要无效，未导入。')
    return rows


def fetch_favorite(row,account_dir):
    md5=row['md5']
    require(isinstance(md5,str) and re.fullmatch('[0-9a-f]{32}',md5),'INVALID_STICKER_ID','表情来源摘要无效。')
    cached=Path(account_dir)/'business/emoticon/Persist'/md5[:2]/md5
    if cached.is_file() and cached.stat().st_size<=SOURCE_LIMIT:
        raw=cached.read_bytes()
        if hashlib.md5(raw).hexdigest()==md5:return raw
    request=urllib.request.Request(source_url(row['cdn_url']),headers={'User-Agent':'WeChatStickerLocal/2.3'})
    opener=urllib.request.build_opener(SourceRedirect());deadline=time.monotonic()+10
    with opener.open(request,timeout=4) as response:
        chunks=[];size=0
        while True:
            require(time.monotonic()<=deadline,'STICKER_DOWNLOAD_TIMEOUT','读取该微信单个表情超时。')
            block=response.read(min(65536,SOURCE_LIMIT+1-size))
            if not block:break
            chunks.append(block);size+=len(block)
            require(size<=SOURCE_LIMIT,'STICKER_TOO_LARGE','表情源文件超过 10 MiB，未导入。')
    raw=b''.join(chunks)
    require(hashlib.md5(raw).hexdigest()==md5,'STICKER_SOURCE_MISMATCH','微信单个表情摘要不匹配，未导入。')
    return raw
