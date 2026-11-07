"""微信本机直连插件：联系人、会话、消息轮询及带操作日志的文字发送。"""
import base64
import contextlib
import hashlib
import importlib.util
import json
import logging
import os
from pathlib import Path
import shutil
import struct
import sys
import tempfile
import time
import types
import uuid
import re
import xml.etree.ElementTree as ET

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'runtime/python-deps'))
from native_host import Exclusive,NativeError,modules,process_identity,invoke
from operations import Operations,OperationConflict
from reply_policy import decision,policy_info
from stickers import Stickers,StickerError
from favorite_source import favorite_records,fetch_favorite

VERSION=json.loads((ROOT/'plugin.json').read_text(encoding='utf8'))['version']

class PluginError(RuntimeError):
    def __init__(self,code,message):super().__init__(message);self.code=code
def require(condition,code,message):
    if not condition:raise PluginError(code,message)
def digest(path):
    with Path(path).open('rb') as stream:return hashlib.file_digest(stream,'sha256').hexdigest()
def text(value,label,maximum=16000):
    require(isinstance(value,str) and value.strip() and '\0' not in value,'INVALID_ARGUMENT',label+'不能为空或包含空字符。')
    require(len(value.encode('utf8'))<=maximum,'INVALID_ARGUMENT',label+'过长。');return value

def sticker_md5(content):
    if not isinstance(content,str) or len(content)>100000 or '<!DOCTYPE' in content.upper() or '<!ENTITY' in content.upper():return None
    try:
        root=ET.fromstring(content);emoji=root if root.tag=='emoji' else root.find('.//emoji')
        value=emoji.get('md5','').lower() if emoji is not None else ''
        return value if re.fullmatch('[0-9a-f]{32}',value) else None
    except ET.ParseError:return None

class WeChat:
    def __init__(self,data=None):
        self.data=Path(data or os.environ.get('WECHAT_DIRECT_DATA') or Path(os.environ['LOCALAPPDATA'])/'CodexWeChatDirect').resolve()
        self.data.mkdir(parents=True,exist_ok=True)
        self.support=json.loads((ROOT/'runtime/support.json').read_text(encoding='utf8'))
        self.config_path=self.data/'account.json'
        self.config=json.loads(self.config_path.read_text(encoding='utf8')) if self.config_path.exists() else {}
        self._reader=None;self._live=None
    def module(self):
        source=ROOT/'runtime/vendor/wechat_db.py'
        require(digest(source)==self.support['readerSha256'],'COMPONENT_CHANGED','数据库组件发生变化，请重新安装插件。')
        package=types.ModuleType('wechatauto');package.__path__=[]
        logger=types.ModuleType('wechatauto.logger');logger.wxlog=logging.getLogger('wechat-direct')
        logger.wxlog.addHandler(logging.NullHandler());logger.wxlog.setLevel(logging.CRITICAL)
        sys.modules['wechatauto']=package;sys.modules['wechatauto.logger']=logger
        spec=importlib.util.spec_from_file_location('wechat_direct_readonly_db',source)
        module=importlib.util.module_from_spec(spec);sys.modules[spec.name]=module;spec.loader.exec_module(module)
        return module
    def live(self,allow_unbound=False):
        if self._live:return self._live
        db=self.module();pids=db.WeChatDB._find_weixin_pids(None)
        preferred=self.config.get('pid')
        if preferred in pids:pids=[preferred]+[x for x in pids if x!=preferred]
        matched=[];unsupported=False
        for pid in pids:
            try:
                core=next((x for x in modules(pid) if x['name'].casefold()=='weixin.dll'),None)
                if not core:continue
                if digest(core['path'])!=self.support['coreSha256']:unsupported=True;continue
                with contextlib.redirect_stdout(sys.stderr):active=db.extract_master_key_from_cfg(pid)
                if not active or not active[2]:continue
                account=active[2]
                if self.config.get('accountId') and account!=self.config['accountId']:continue
                matched.append({'pid':pid,'accountId':account,'corePath':core['path'],'identity':process_identity(pid)})
                if preferred==pid:break
            except (OSError,StopIteration):continue
        if not matched:
            raise PluginError('CLIENT_UNSUPPORTED' if unsupported else 'ACCOUNT_NOT_READY',
                '此微信版本暂未适配，插件未执行发送。' if unsupported else '未找到已登录的绑定账号，请先正常打开并登录微信。')
        require(len(matched)==1,'ACCOUNT_AMBIGUOUS','检测到多个可用微信进程，请保留一个目标账号后再试。')
        require(allow_unbound or self.config.get('accountId'),'ACCOUNT_NOT_BOUND','请先运行插件初始化，绑定当前个人号。')
        self._live=matched[0];return self._live
    def bind(self):
        live=self.live(True)
        self.config={'accountId':live['accountId'],'pid':live['pid'],'boundAt':int(time.time()),'transport':'direct-only'}
        self.config_path.write_text(json.dumps(self.config,ensure_ascii=False,indent=2),encoding='utf8')
        self.reader()
        return self.status()
    def reader(self):
        if self._reader:return self._reader
        live=self.live();db=self.module();temp=self.data/'tmp';temp.mkdir(exist_ok=True);tempfile.tempdir=str(temp)
        owner=self
        class LocalReader(db.WeChatDB):
            def _stable_key_dirs(self):return [str(owner.data/'keys')]
        with contextlib.redirect_stdout(sys.stderr):
            reader=LocalReader(workdir=str(self.data/'db'))
        account=live['accountId']
        require(reader.account==account or reader.account.startswith(account+'_'),'DATABASE_ACCOUNT_MISMATCH','消息数据库与当前绑定账号不一致，未执行发送。')
        self._reader=reader;return reader
    def status(self):
        live=self.live()
        return {'ready':True,'pluginVersion':VERSION,'clientVersion':self.support['clientVersion'],
            'accountId':live['accountId'],'pid':live['pid'],'transport':'direct-only',
            'capabilities':['contacts','sessions','read_messages','poll_messages','send_text','send_reply','reply_policy','stickers','sticker_source','sticker_import','send_sticker','send_status'],
            'messageSource':'read-only local database and WAL','automaticReplies':False,'replyPolicy':policy_info(),
            'nativeValidation':self.support.get('nativeValidation',{}),'preflightBeforeFirstSubmission':self.support.get('preflightBeforeFirstSubmission',False)}
    def reply_policy(self,recipient=None,scene='casual'):
        result=policy_info()
        if recipient is not None:
            recipient=self.recipient(recipient);log=Operations(self.data/'operations.sqlite3')
            try:result.update(recipientId=recipient,nextReply=decision(scene,log.conversation_history(self.live()['accountId'],recipient)))
            finally:log.close()
        return result
    def contacts(self,query):
        query=text(query,'搜索词',200);rows=self.reader().search_contact(query)
        return {'contacts':[{'recipientId':x['username'],'nickname':x['nick_name'],'remark':x['remark'],
            'isGroup':x['username'].endswith('@chatroom'),'exact':query in (x['username'],x['nick_name'],x['remark'])} for x in rows]}
    def sticker_library(self,verify_favorites=False):
        account=self.config.get('accountId')
        require(account,'ACCOUNT_NOT_BOUND','请先初始化绑定当前账号。')
        require(digest(ROOT/'runtime/builtin-expressions.json')==self.support.get('builtinCatalogSha256'),
                'COMPONENT_CHANGED','内置表情目录发生变化，请重新安装插件。')
        library=Stickers(self.data,account)
        if verify_favorites:library.set_favorites(favorite_records(self.reader()))
        return library
    def sticker_source(self,directory=None,user_created=False):
        require(directory is None and not user_created,'EXPRESSION_SOURCE_LOCKED','表情来源限定为微信内置和当前账号添加的单个表情。')
        return self.sticker_library(True).source_info()
    def stickers(self,query='',scene=None,limit=12,include_preview=False,category='all',offset=0,refresh=False,favorite_offset=0):
        library=self.sticker_library(True);result={}
        if refresh:
            require(isinstance(favorite_offset,int) and not isinstance(favorite_offset,bool) and favorite_offset>=0,'INVALID_ARGUMENT','单个表情同步偏移量无效。')
            reader=self.reader();records=favorite_records(reader);library.set_favorites(records)
            errors=[];imported=[];selected=records[favorite_offset:favorite_offset+6]
            for number,record in enumerate(selected,favorite_offset+1):
                try:
                    cached=self.data/'panel-originals'/record['md5']
                    raw=cached.read_bytes() if cached.is_file() and cached.stat().st_size<=10*1024*1024 else b''
                    if hashlib.md5(raw).hexdigest()!=record['md5']:raw=fetch_favorite(record,reader.account_dir)
                    imported.append(library.import_favorite(raw,record,number)['stickerId'])
                except Exception as error:errors.append({'index':number,'code':getattr(error,'code','STICKER_FETCH_UNAVAILABLE')})
            next_offset=favorite_offset+len(selected)
            result['favoriteSync']={'imported':len(imported),'errors':errors,'nextFavoriteOffset':next_offset,'hasMore':next_offset<len(records),'favoriteCount':len(records)}
        result['source']=library.source_info()
        items=library.list(query,scene,limit,category,offset);result['stickers']=items
        result['total']=len(library.matching(query,scene,category))
        result['totalAllExpressions']=len(library.builtins)+sum(library.allowed_origin(x) for x in library.items.values())
        result['nextOffset']=offset+len(items)
        if include_preview:result['preview']=library.preview(items)
        return result
    def sticker_import(self,path,label,scenes,identity=None):
        library=self.sticker_library()
        require(bool(path)!=bool(identity),'INVALID_ARGUMENT','导入文件或标注已有 stickerId 二选一。')
        item=self.sticker_library(True).label(identity,label,scenes) if identity else library.import_file(path,label,scenes)
        return {'sticker':{k:v for k,v in item.items() if k!='nativePath'}}
    def recipient(self,value):
        value=text(value,'联系人内部账号',256)
        rows=self.reader().search_contact(value)
        exact=[x for x in rows if x['username']==value]
        if value=='filehelper':return value
        require(len(exact)==1,'CONTACT_NOT_FOUND','请先搜索联系人，使用唯一匹配的 recipientId，不能直接填写昵称。')
        return value
    def rows(self,recipient,limit=30,since=None):
        reader=self.reader();limit=max(1,min(int(limit),200))
        sql='WHERE sort_seq>=?' if since is not None else ''
        params=(int(since),) if since is not None else ()
        order=reader._MSG_ORDER_ASC if since is not None else reader._MSG_ORDER_DESC
        result=reader._run_msg_query(recipient,lambda tables:(reader._shard_rows(tables,sql,params,
            order_ext=order,per_shard_limit=limit+1),reader._shard_maps(tables)))
        if not result:return []
        rows,maps=result;out=[]
        for row in rows:
            item=reader._msg_row_to_dict(row,maps)
            out.append({'localId':str(row['local_id']),'serverId':str(row['server_id']),
                'senderId':item['sender_username'],'direction':'outgoing' if item['sender_username']==self.live()['accountId'] else 'incoming',
                'createdAt':item['create_time'],'text':item['content'],'type':item['type'],'typeCode':int(row['local_type'])&0xffffffff,
                'sortSeq':str(item['sort_seq']),'shard':item['shard']})
            if out[-1]['typeCode']==47:out[-1]['stickerMd5']=sticker_md5(item['content'])
        return sorted(out,key=lambda x:(int(x['sortSeq']),x['shard'],int(x['localId'])),reverse=since is None)
    def cursor(self,recipient,rows,previous=None):
        maximum=max([int(x['sortSeq']) for x in rows]+[int((previous or {}).get('sortSeq',0))])
        seen=set((previous or {}).get('seen',[])) if int((previous or {}).get('sortSeq',0))==maximum else set()
        seen.update(x['shard']+':'+x['localId'] for x in rows if int(x['sortSeq'])==maximum)
        data={'account':self.live()['accountId'],'recipient':recipient,'sortSeq':str(maximum),'seen':sorted(seen)}
        return base64.urlsafe_b64encode(json.dumps(data,separators=(',',':')).encode()).decode().rstrip('=')
    def decode_cursor(self,value,recipient):
        try:
            require(isinstance(value,str) and len(value)<20000,'INVALID_CURSOR','消息游标无效。')
            decoded=json.loads(base64.urlsafe_b64decode(value+'='*(-len(value)%4)))
            require(decoded['account']==self.live()['accountId'] and decoded['recipient']==recipient,'CURSOR_MISMATCH','游标属于其他账号或会话。')
            require(str(decoded['sortSeq']).isdigit() and isinstance(decoded['seen'],list),'INVALID_CURSOR','消息游标无效。')
            return decoded
        except (ValueError,KeyError,TypeError):raise PluginError('INVALID_CURSOR','消息游标无效。')
    def messages(self,recipient,limit=30):
        recipient=self.recipient(recipient);limit=max(1,min(int(limit),200));rows=self.rows(recipient,limit)[:limit]
        return {'recipientId':recipient,'messages':rows,'cursor':self.cursor(recipient,rows)}
    def poll(self,recipient,cursor,timeout=10,limit=50):
        recipient=self.recipient(recipient);old=self.decode_cursor(cursor,recipient)
        timeout=max(0,min(float(timeout),30));limit=max(1,min(int(limit),200));deadline=time.monotonic()+timeout
        while True:
            rows=self.rows(recipient,limit,int(old['sortSeq']))
            rows=[x for x in rows if int(x['sortSeq'])>int(old['sortSeq']) or x['shard']+':'+x['localId'] not in old['seen']][:limit]
            if rows or time.monotonic()>=deadline:
                return {'recipientId':recipient,'messages':rows,'cursor':self.cursor(recipient,rows,old),'timedOut':not rows}
            time.sleep(min(.5,max(0,deadline-time.monotonic())))
    def stage_driver(self,sticker=False):
        source=ROOT/('runtime/native/direct-bridge-v3.dll' if sticker else 'runtime/native/direct-bridge-v2.dll')
        expected=self.support['stickerDriverSha256' if sticker else 'driverSha256']
        require(digest(source)==expected,'DRIVER_CHANGED','原生驱动校验失败，请重新安装插件。')
        directory=self.data/'native'/expected[:16];directory.mkdir(parents=True,exist_ok=True)
        destination=directory/('wechat-native-'+expected[:12]+'.dll')
        if not destination.exists():shutil.copyfile(source,destination)
        require(digest(destination)==expected,'DRIVER_CHANGED','本地驱动副本校验失败，请重新安装插件。')
        return destination
    def native(self,recipient,body,job,action):
        live=self.live();driver=self.stage_driver(sticker=True) if action in (4,5) else self.stage_driver();directory=driver.parent
        if action in (2,5) and self.support.get('preflightBeforeFirstSubmission'):
            identity={'process':live['identity'],'driver':digest(driver),'core':self.support['coreSha256'],'kind':'sticker' if action==5 else 'text'}
            marker=directory/('prepared-'+hashlib.sha256(json.dumps(identity,sort_keys=True).encode()).hexdigest()[:24]+'.json')
            try:cached=json.loads(marker.read_text(encoding='utf8')) if marker.exists() else None
            except (OSError,ValueError):cached=None
            loaded=any(Path(x['path']).resolve()==driver for x in modules(live['pid']))
            if cached!=identity or not loaded:
                # 新客户端的首次发送先只构造对象，未通过时不能进入提交分支。
                try:prepared=self.native('filehelper',body if action==5 else '微信直连准备自检',uuid.uuid4().hex,4 if action==5 else 1)
                except (NativeError,PluginError,OSError,ValueError) as error:
                    raise NativeError('新版微信原生准备自检失败，未提交消息：'+str(error)) from error
                if prepared.get('stage')!='prepared' or prepared.get('exceptionCode') or prepared.get('nativeSubmissionReturned'):
                    raise NativeError('新版微信原生准备自检未通过，未提交消息。')
                try:
                    temporary=marker.with_suffix('.tmp');temporary.write_text(json.dumps(identity,sort_keys=True),encoding='utf8');temporary.replace(marker)
                except OSError as error:raise NativeError('原生准备状态无法保存，未提交消息。') from error
        recipient=recipient.encode();body=body.encode();request=struct.pack('<8sIII32s',b'WXDR01\0\0',action,len(recipient),len(body),job.encode())+recipient+body
        temporary=directory/'bridge-request.tmp';temporary.write_bytes(request);temporary.replace(directory/'bridge-request.bin')
        report=directory/'bridge-result.json'
        if report.exists():report.unlink()
        invoke(live['pid'],live['corePath'],self.support['coreSha256'],driver,action in (2,5))
        require(report.exists(),'NATIVE_REPORT_MISSING','调用结果尚未确认，请先查询原操作，不要重发。')
        result=json.loads(report.read_text(encoding='utf8'))
        require(result.get('pid')==live['pid'] and result.get('job')==job,'NATIVE_REPORT_MISMATCH','原生结果与本次操作不符，请先核对，不要重发。')
        return result
    def probe(self):
        self.reader();result=self.native('filehelper','微信直连插件发送准备自检',uuid.uuid4().hex,1)
        require(result['stage']=='prepared','NATIVE_PREPARATION_FAILED','原生发送准备未通过，未发送消息。')
        return {'ready':True,'stage':'prepared','messageSent':False,'requestId':result.get('requestId')}
    def probe_sticker(self,identity):
        asset=self.sticker_library(not str(identity).startswith('builtin:')).get(identity)
        result=self.native('filehelper',asset.get('nativePath') or asset['text'],uuid.uuid4().hex,1 if asset.get('kind')=='builtin' else 4)
        require(result['stage']=='prepared','NATIVE_PREPARATION_FAILED','图片表情构造自检未通过，未发送。')
        return {'ready':True,'stage':'prepared','messageSent':False,'stickerId':identity,'native':result}
    def reconcile(self,key,item,log,wait=0):
        if item['status'] in ('confirmed','not_submitted'):return self.public(item)
        require(item['accountId']==self.live()['accountId'],'ACCOUNT_MISMATCH','发送操作属于其他账号。')
        deadline=time.monotonic()+max(0,min(float(wait),30));baseline=set(item['baselineServerIds'])
        while True:
            rows=self.rows(item['recipientId'],200)
            found=[x for x in rows if x['direction']=='outgoing' and x['serverId'].isdigit() and int(x['serverId'])>0 and
                x['serverId'] not in baseline and self.receipt_matches(x,item) and x['createdAt']>=int(item['startedAt'])-1]
            if len(found)==1:
                item['status']='confirmed';item['record']=found[0];item['confirmedAt']=time.time();log.save(key,item)
                return self.public(item)
            if len(found)>1:raise PluginError('AMBIGUOUS_RECEIPT','存在多条匹配记录，请人工核对，不要自动重发。')
            if time.monotonic()>=deadline:
                item['status']='unknown';log.save(key,item);return self.public(item)
            time.sleep(min(.5,max(0,deadline-time.monotonic())))
    @staticmethod
    def receipt_matches(row,item):
        if item.get('kind')=='sticker':
            return row.get('typeCode')==47 and row.get('stickerMd5')==item['sticker']['md5']
        return row['text']==item['text']
    @staticmethod
    def public(item):
        result={'status':item['status'],'recipientId':item['recipientId'],'text':item['text'],
            'operationKey':item.get('operationKey'),'operationHash':item['operationHash'],'deliveredToRecipient':'unknown','readByRecipient':'unknown'}
        if item.get('record'):result.update(serverId=item['record']['serverId'],localId=item['record']['localId'])
        if item.get('error'):result['error']=item['error']
        if item.get('replyPolicy'):result['replyPolicy']=item['replyPolicy']
        if item.get('kind') in ('sticker','builtin'):result.update(messageType=1 if item['kind']=='builtin' else 47,sticker=item['sticker'])
        if item['status']=='unknown':result['message']='结果尚未确认；请用同一操作编号查询，不要重发。'
        return result
    def send(self,recipient,body,key,wait=15,reply_scene=None):
        body=text(body,'正文');recipient=self.recipient(recipient);live=self.live()
        rows=self.rows(recipient,200);baseline=[x['serverId'] for x in rows if x['direction']=='outgoing']
        log=Operations(self.data/'operations.sqlite3')
        try:
            item,fresh=log.reserve(key,live['accountId'],recipient,body,live['identity'],baseline,reply_scene=reply_scene)
            if not fresh:return self.reconcile(key,item,log,wait)
            body=item['text']
            text(body,'正文')
            return self.submit(key,item,log,wait)
        finally:log.close()
    def send_sticker(self,recipient,identity,key,scene='serious',mode='contextual',wait=15):
        decision(scene,[])
        require(mode in ('contextual','explicit'),'INVALID_ARGUMENT','mode 需为 contextual 或 explicit。')
        recipient=self.recipient(recipient);live=self.live();log=Operations(self.data/'operations.sqlite3')
        try:
            old=log.get(key)
            if old:
                require(old.get('kind') in ('sticker','builtin') and old['sticker']['stickerId']==identity,'OPERATION_CONFLICT','同编号发送内容变化，未发送。')
                asset=old['sticker']
            else:
                asset=self.sticker_library(not str(identity).startswith('builtin:')).get(identity);asset={k:v for k,v in asset.items() if k!='nativePath'}
                if mode=='contextual':require(asset.get('reviewed') and scene in asset['scenes'],'STICKER_NOT_REVIEWED','请先预览并标注表情含义及适用场景，未发送。')
            rows=self.rows(recipient,200);baseline=[x['serverId'] for x in rows if x['direction']=='outgoing']
            item,fresh=log.reserve(key,live['accountId'],recipient,'',live['identity'],baseline,
                sticker=asset,sticker_mode=mode,sticker_scene=scene)
            if not fresh:return self.reconcile(key,item,log,wait)
            return self.submit(key,item,log,wait)
        finally:log.close()
    def submit(self,key,item,log,wait):
        live=self.live();item['status']='submitting';log.save(key,item);native_started=False
        try:
            current=self.module().extract_master_key_from_cfg(live['pid'])
            require(current and current[2]==live['accountId'],'ACCOUNT_CHANGED','发送前账号发生变化，未提交。')
            body=item['text'];action=2
            if item.get('kind')=='sticker':
                body=self.sticker_library(True).get(item['sticker']['stickerId'])['nativePath'];action=5
            elif item.get('kind')=='builtin':body=self.sticker_library().get(item['sticker']['stickerId'])['text']
            native_started=True
            native=self.native(item['recipientId'],body,item['job'],action);item['native']=native;stage=native['stage']
            item['status']='submitted' if stage=='submitted' else ('unknown' if stage in ('submission_started','native_exception_outcome_unknown','operation_already_reserved') else 'not_submitted')
            if stage!='submitted':item['error']=stage
        except NativeError as error:
            item['status']='unknown' if error.possible_submission else 'not_submitted';item['error']=str(error)
        except Exception as error:
            item['status']='unknown' if native_started else 'not_submitted';item['error']=getattr(error,'code',type(error).__name__)
        log.save(key,item)
        return self.reconcile(key,item,log,wait)
    def send_status(self,key,wait=0):
        log=Operations(self.data/'operations.sqlite3')
        try:
            if not key:
                require(self.config.get('accountId'),'ACCOUNT_NOT_BOUND','请先初始化绑定账号。')
                return {'operations':[self.public(x) for x in log.recent(self.config['accountId'])]}
            item=log.get(key);require(item,'OPERATION_NOT_FOUND','没有此操作编号的发送记录。')
            return self.reconcile(key,item,log,wait)
        finally:log.close()

def dispatch(service,request):
    action=request.get('action')
    if action=='bind':return service.bind()
    if action=='status':return service.status()
    if action=='reply-policy':return service.reply_policy(request.get('recipientId'),request.get('scene','casual'))
    if action=='probe':return service.probe()
    if action=='sticker-probe':return service.probe_sticker(request.get('stickerId'))
    if action=='sticker-source':return service.sticker_source(request.get('directory'),request.get('userCreated',False))
    if action=='stickers':
        return service.stickers(request.get('query',''),request.get('scene'),request.get('limit',12),request.get('includePreview',False),
            request.get('category','all'),request.get('offset',0),request.get('refreshFavorites',False),request.get('favoriteOffset',0))
    if action=='sticker-import':return service.sticker_import(request.get('filePath'),request.get('label'),request.get('scenes'),request.get('stickerId'))
    if action=='send-sticker':return service.send_sticker(request.get('recipientId'),request.get('stickerId'),request.get('operationKey'),request.get('scene','serious'),request.get('mode','contextual'),request.get('wait',15))
    if action=='contacts':return service.contacts(request.get('query'))
    if action=='sessions':return {'sessions':service.reader().get_sessions(max(1,min(int(request.get('limit',30)),200)))}
    if action=='messages':return service.messages(request.get('recipientId'),request.get('limit',30))
    if action=='poll':return service.poll(request.get('recipientId'),request.get('cursor'),request.get('timeout',10),request.get('limit',50))
    if action=='send':return service.send(request.get('recipientId'),request.get('text'),request.get('operationKey'),request.get('wait',15))
    if action=='send-reply':
        scene=request.get('scene','serious');decision(scene,[])
        return service.send(request.get('recipientId'),request.get('text'),request.get('operationKey'),request.get('wait',15),scene)
    if action=='send-status':return service.send_status(request.get('operationKey'),request.get('wait',0))
    raise PluginError('UNKNOWN_ACTION','不支持的插件操作。')

def main():
    try:
        raw=sys.stdin.buffer.read(128*1024+1)
        require(len(raw)<=128*1024,'REQUEST_TOO_LARGE','请求过大。')
        request=json.loads(raw.decode('utf8'));require(isinstance(request,dict),'INVALID_REQUEST','请求应为 JSON 对象。')
        service=WeChat()
        with Exclusive(service.data),contextlib.redirect_stdout(sys.stderr):result=dispatch(service,request)
        output={'ok':True,'result':result}
    except Exception as error:
        output={'ok':False,'error':{'code':getattr(error,'code','OPERATION_CONFLICT' if isinstance(error,OperationConflict) else 'LOCAL_BACKEND_ERROR'),'message':str(error)}}
    sys.stdout.write(json.dumps(output,ensure_ascii=False)+'\n');sys.stdout.flush()
    return 0 if output['ok'] else 2

if __name__=='__main__':raise SystemExit(main())
