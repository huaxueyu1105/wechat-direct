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
import sqlite3
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
from scheduler import Scheduler,ScheduleError
from media import inspect_file,stage_file,verify_staged,receipt_metadata,receipt_matches as media_receipt_matches
import cache_policy

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
        pointer_back = self.support.get('accountConfigPointerBack')
        if pointer_back is not None:
            module.CFG_PTR_BACK = int(pointer_back, 0)
        return module
    def live(self,allow_unbound=False,selector=None):
        if self._live and selector is None:return self._live
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
                if selector is None and self.config.get('accountId') and account!=self.config['accountId']:continue
                candidate={'pid':pid,'accountId':account,'corePath':core['path'],'identity':process_identity(pid)}
                matched.append(candidate)
                if preferred==pid and selector is None:break
            except (OSError,StopIteration):continue
        if selector is not None:
            exact=[candidate for candidate in matched if candidate['accountId']==selector]
            if exact:matched=exact
            else:
                aliases=[]
                for candidate in matched:
                    try:
                        reader=self.account_reader(db,candidate,contacts_only=True)
                        contact=next((rel for rel,_,_ in reader._db_files if Path(rel).name=='contact.db'),None)
                        if not contact:continue
                        conn=reader._open(contact)
                        try:matches=conn.execute('SELECT 1 FROM contact WHERE username=? AND alias=? LIMIT 1',(candidate['accountId'],selector)).fetchone()
                        finally:conn.close()
                        if matches:aliases.append(candidate)
                    except (OSError,RuntimeError,sqlite3.Error,StopIteration):continue
                matched=aliases
        if not matched:
            raise PluginError('CLIENT_UNSUPPORTED' if unsupported else 'ACCOUNT_NOT_READY',
                '此微信版本暂未适配，插件未执行发送。' if unsupported else '未找到指定的已登录账号，请核对微信号并正常登录微信。')
        require(len(matched)==1,'ACCOUNT_AMBIGUOUS','检测到多个可用微信进程，请用 bind --account 指定微信号。')
        require(allow_unbound or self.config.get('accountId'),'ACCOUNT_NOT_BOUND','请先运行插件初始化，绑定当前个人号。')
        if selector is None:self._live=matched[0]
        return matched[0]
    def bind(self,account=None):
        if account is not None:account=text(account,'微信号',256)
        live=self.live(True,selector=account)
        reader=self.account_reader(self.module(),live)
        try:
            contact=next((rel for rel,_,_ in reader._db_files if Path(rel).name=='contact.db'),None)
            require(contact,'DATABASE_NOT_READY','目标账号的联系人数据库尚不可用，原绑定未更改。')
            conn=reader._open(contact)
            try:own=conn.execute('SELECT 1 FROM contact WHERE username=? LIMIT 1',(live['accountId'],)).fetchone()
            finally:conn.close()
            require(own,'DATABASE_ACCOUNT_MISMATCH','未在目标数据库中确认账号身份，原绑定未更改。')
        except (OSError,sqlite3.Error) as error:
            raise PluginError('DATABASE_NOT_READY','目标账号的联系人数据库尚不可用，原绑定未更改。') from error
        config={'accountId':live['accountId'],'pid':live['pid'],'boundAt':int(time.time()),'transport':'direct-only'}
        temporary=self.config_path.with_suffix('.tmp')
        temporary.write_text(json.dumps(config,ensure_ascii=False,indent=2),encoding='utf8')
        temporary.replace(self.config_path)
        self.config=config;self._live=live;self._reader=reader
        return self.status()
    def account_reader(self,db,live,contacts_only=False):
        account=live['accountId'];base=db.auto_detect_db_dir()
        require(base,'DATABASE_ACCOUNT_MISMATCH','未找到微信数据库目录。')
        candidates=[Path(path).name for path in db._find_account_dirs(base)
                    if Path(path).name==account or re.fullmatch(re.escape(account)+r'_\w{4}',Path(path).name)]
        require(len(candidates)==1,'DATABASE_ACCOUNT_MISMATCH','未找到与当前账号唯一匹配的数据库目录。')
        private=self.data/'accounts'/hashlib.sha256(account.encode()).hexdigest()[:16]
        temp=private/'tmp';temp.mkdir(parents=True,exist_ok=True)
        owner=self
        class LocalReader(db.WeChatDB):
            def _stable_key_dirs(self):return [str(private/'keys')]
            def _find_weixin_pids(self):return [live['pid']]
            def _try_other_accounts(self):return False
            def _select_account_by_keys(self,candidates):return False
            def _collect_db_files(self):
                files=super()._collect_db_files()
                return [row for row in files if Path(row[0]).name=='contact.db'] if contacts_only else files
            def _open(self,rel):
                connection=super()._open(rel)
                try:os.utime(Path(self.workdir)/rel.replace(os.sep,'__'),None)
                except OSError:pass
                return connection
            def _trim_read_cache(self,keep):
                try:cache_policy.clean(owner.data,keep_paths=[Path(self.workdir)/name for name in keep])
                except (OSError,ValueError):pass
        previous_temp=tempfile.tempdir
        try:
            tempfile.tempdir=str(temp)
            with contextlib.redirect_stdout(sys.stderr):
                reader=LocalReader(db_dir=base,account=candidates[0],workdir=str(private/('identity' if contacts_only else 'db')))
        finally:tempfile.tempdir=previous_temp
        require(reader.account==candidates[0],'DATABASE_ACCOUNT_MISMATCH','消息数据库与当前绑定账号不一致，未执行发送。')
        return reader
    def reader(self):
        if self._reader:return self._reader
        reader=self.account_reader(self.module(),self.live())
        self._reader=reader;return reader
    def status(self):
        live=self.live()
        return {'ready':True,'pluginVersion':VERSION,'clientVersion':self.support['clientVersion'],
            'accountId':live['accountId'],'pid':live['pid'],'transport':'direct-only',
            'capabilities':['contacts','sessions','read_messages','poll_messages','send_text','send_reply','reply_policy','stickers','sticker_source','sticker_import','send_sticker','send_status','schedule_create','schedules','schedule_update',
                            *['send_'+kind for kind in ('image','file') if self.support.get('mediaCapabilities',{}).get(kind) is True]],
            'messageSource':'read-only local database and WAL','automaticReplies':False,'replyPolicy':policy_info(),
            'nativeValidation':self.support.get('nativeValidation',{}),'preflightBeforeFirstSubmission':self.support.get('preflightBeforeFirstSubmission',False),
            'mediaCapabilities':self.support.get('mediaCapabilities',{'image':False,'file':False})}
    def reply_policy(self,recipient=None,scene='casual'):
        result=policy_info()
        if recipient is not None:
            recipient=self.recipient(recipient);log=Operations(self.data/'operations.sqlite3')
            try:result.update(recipientId=recipient,nextReply=decision(scene,log.conversation_history(self.live()['accountId'],recipient)))
            finally:log.close()
        return result
    def schedule(self, action, request):
        account=self.config.get('accountId')
        require(account,'ACCOUNT_NOT_BOUND','请先初始化绑定账号。')
        jobs=Scheduler(self.data/'schedules.sqlite3',account)
        try:
            if action=='schedule-create':
                payload=request.get('payload')
                require(isinstance(payload,dict),'INVALID_ARGUMENT','定时发送内容需为对象。')
                payload=payload.copy()
                require(payload.get('action') in ('send','send-sticker','send-image','send-file'),'SCHEDULE_TYPE_UNSUPPORTED','不支持此定时消息类型。')
                try:task_id=str(uuid.UUID(request.get('taskId')))
                except (ValueError,TypeError,AttributeError):raise PluginError('INVALID_TASK_ID','创建任务需要稳定的 UUID taskId；重试沿用同一编号。')
                try:previous=jobs.get(task_id)
                except ScheduleError as error:
                    if error.code!='SCHEDULE_NOT_FOUND':raise
                    previous=None
                if previous:
                    if payload['action'] in ('send-image','send-file'):
                        require(isinstance(payload.get('filePath'),str),'INVALID_MEDIA_PATH','请提供文件绝对路径。')
                        payload['filePath']=str(Path(payload['filePath']).resolve())
                        # A creation retry refers to the originally authorized bytes,
                        # even if the source has since changed or disappeared.
                        payload.setdefault('fileSha256',previous['payload'].get('fileSha256'))
                        if isinstance(payload.get('fileSha256'),str):payload['fileSha256']=payload['fileSha256'].lower()
                else:
                    payload['recipientId']=self.recipient(payload.get('recipientId'))
                    require(self.live()['accountId']==account,'ACCOUNT_MISMATCH','当前账号与任务绑定不一致。')
                    if payload['action']=='send':text(payload.get('text'),'正文')
                    elif payload['action']=='send-sticker':
                        identity=payload.get('stickerId')
                        self.sticker_library(not str(identity).startswith('builtin:')).get(identity)
                    else:
                        kind=payload['action'][5:];self.require_media(kind)
                        asset=inspect_file(payload.get('filePath'),kind,payload.get('fileSha256'))
                        payload.update(filePath=asset['sourcePath'],fileSha256=asset['sha256'])
                result=jobs.create(payload,request.get('schedule'),timezone=request.get('timezone','Asia/Shanghai'),task_id=task_id)
                return {'task':result,'workerRequired':True,
                        'message':'任务已保存；本机定时执行器运行且微信已登录时才会发送。'}
            if action=='schedule-list':return {'tasks':jobs.list()}
            if action=='schedule-update':
                operation=request.get('operation')
                if operation=='close-run':
                    require(request.get('reviewed') is True,'REVIEW_REQUIRED','必须先人工核对本次发送结果。')
                    task=jobs.get(request.get('taskId'));run=task['lastRun']
                    require(run,'SCHEDULE_RUN_NOT_FOUND','此任务尚无执行记录。')
                    log=Operations(self.data/'operations.sqlite3')
                    try:exists=log.get(run['operationKey']) is not None
                    finally:log.close()
                    outcome=self.close_send(run['operationKey'],True) if exists else None
                    return {'task':jobs.close_run(task['taskId'],reviewed=True,result=outcome)}
                require(operation in ('pause','resume','cancel'),'INVALID_ARGUMENT','任务操作需为 pause、resume、cancel 或 close-run。')
                return {'task':getattr(jobs,operation)(request.get('taskId'))}
            if action=='schedule-run':
                def run(payload,key):
                    try:
                        require(self.live()['accountId']==account,'ACCOUNT_MISMATCH','执行账号与任务绑定不一致。')
                        return dispatch(self,{**payload,'operationKey':key,'wait':0})
                    except Exception as error:
                        log=Operations(self.data/'operations.sqlite3')
                        try:
                            # Every native send first reserves its operation. A known
                            # error before reservation is a failure, not an unknown send.
                            if log.get(key) is None:
                                return {'status':'not_submitted','operationKey':key,'error':getattr(error,'code',type(error).__name__)}
                        finally:log.close()
                        raise
                return jobs.run_due(run,self.send_status,limit=1)
            raise PluginError('UNKNOWN_ACTION','不支持的定时任务操作。')
        finally:jobs.close()
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
        def query(tables):
            if isinstance(since,dict):
                rows=[];sequence=int(since['sortSeq'])
                for conn,table in tables:
                    last=int(since['positions'].get(reader._shard_label(conn),'0'))
                    rows.extend(reader._shard_rows([(conn,table)],
                        'WHERE sort_seq>? OR (sort_seq=? AND local_id>?)',(sequence,sequence,last),
                        order_ext=reader._MSG_ORDER_ASC,per_shard_limit=limit))
            else:
                rows=reader._shard_rows(tables,'WHERE sort_seq>=?' if since is not None else '',
                    (int(since),) if since is not None else (),
                    order_ext=reader._MSG_ORDER_ASC if since is not None else 'ORDER BY sort_seq DESC, local_id DESC',
                    per_shard_limit=limit)
            return rows,reader._shard_maps(tables)
        result=reader._run_msg_query(recipient,query)
        if not result:return []
        rows,maps=result;out=[]
        for row in rows:
            item=reader._msg_row_to_dict(row,maps)
            out.append({'localId':str(row['local_id']),'serverId':str(row['server_id']),
                'senderId':item['sender_username'],'direction':'outgoing' if item['sender_username']==self.live()['accountId'] else 'incoming',
                'createdAt':item['create_time'],'text':item['content'],'type':item['type'],'typeCode':int(row['local_type'])&0xffffffff,
                'sortSeq':str(item['sort_seq']),'shard':item['shard']})
            if out[-1]['typeCode']==47:out[-1]['stickerMd5']=sticker_md5(item['content'])
            elif out[-1]['typeCode'] in (3,49):out[-1].update(receipt_metadata(item['content'],out[-1]['typeCode']))
        return sorted(out,key=lambda x:(int(x['sortSeq']),x['shard'],int(x['localId'])),reverse=since is None)
    def cursor(self,recipient,rows,previous=None):
        maximum=max([int(x['sortSeq']) for x in rows]+[int((previous or {}).get('sortSeq',0))])
        positions=dict((previous or {}).get('positions',{})) if int((previous or {}).get('sortSeq',0))==maximum else {}
        for row in rows:
            if int(row['sortSeq'])==maximum:
                positions[row['shard']]=str(max(int(positions.get(row['shard'],0)),int(row['localId'])))
        data={'version':2,'account':self.live()['accountId'],'recipient':recipient,'sortSeq':str(maximum),'positions':positions}
        return base64.urlsafe_b64encode(json.dumps(data,separators=(',',':')).encode()).decode().rstrip('=')
    def decode_cursor(self,value,recipient):
        try:
            require(isinstance(value,str) and len(value)<20000,'INVALID_CURSOR','消息游标无效。')
            decoded=json.loads(base64.urlsafe_b64decode(value+'='*(-len(value)%4)))
            require(isinstance(decoded,dict),'INVALID_CURSOR','消息游标无效。')
            require(decoded['account']==self.live()['accountId'] and decoded['recipient']==recipient,'CURSOR_MISMATCH','游标属于其他账号或会话。')
            require(str(decoded['sortSeq']).isdigit() and 0<=int(decoded['sortSeq'])<2**63,'INVALID_CURSOR','消息游标无效。')
            if decoded.get('version',1)==1:
                require(isinstance(decoded.get('seen'),list),'INVALID_CURSOR','消息游标无效。')
                positions={}
                for item in decoded['seen']:
                    require(isinstance(item,str) and ':' in item,'INVALID_CURSOR','消息游标无效。')
                    shard,local=item.rsplit(':',1)
                    require(local.isdigit() and 0<=int(local)<2**63,'INVALID_CURSOR','消息游标无效。')
                    positions[shard]=str(max(int(positions.get(shard,0)),int(local)))
                decoded.update(version=2,positions=positions)
            positions=decoded.get('positions')
            require(decoded.get('version')==2 and isinstance(positions,dict) and
                    all(isinstance(k,str) and 0<len(k)<=256 and isinstance(v,str) and v.isdigit() and 0<=int(v)<2**63
                        for k,v in positions.items()),'INVALID_CURSOR','消息游标无效。')
            return decoded
        except (ValueError,KeyError,TypeError):raise PluginError('INVALID_CURSOR','消息游标无效。')
    def messages(self,recipient,limit=30):
        recipient=self.recipient(recipient);limit=max(1,min(int(limit),200));rows=self.rows(recipient,limit)[:limit]
        return {'recipientId':recipient,'messages':rows,'cursor':self.cursor(recipient,rows)}
    def poll(self,recipient,cursor,timeout=10,limit=50):
        recipient=self.recipient(recipient);old=self.decode_cursor(cursor,recipient)
        timeout=max(0,min(float(timeout),30));limit=max(1,min(int(limit),200));deadline=time.monotonic()+timeout
        while True:
            rows=self.rows(recipient,limit,old)[:limit]
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
        live=self.live();driver=self.stage_driver(sticker=True) if action in (4,5,6,7,8,9) else self.stage_driver();directory=driver.parent
        if action in (2,5,7,9) and self.support.get('preflightBeforeFirstSubmission'):
            kind={2:'text',5:'sticker',7:'image',9:'file'}[action]
            identity={'process':live['identity'],'driver':digest(driver),'core':self.support['coreSha256'],'kind':kind}
            marker=directory/('prepared-'+hashlib.sha256(json.dumps(identity,sort_keys=True).encode()).hexdigest()[:24]+'.json')
            try:cached=json.loads(marker.read_text(encoding='utf8')) if marker.exists() else None
            except (OSError,ValueError):cached=None
            loaded=any(Path(x['path']).resolve()==driver for x in modules(live['pid']))
            if cached!=identity or not loaded:
                # 新客户端的首次发送先只构造对象，未通过时不能进入提交分支。
                try:prepared=self.native('filehelper',body if action!=2 else '微信直连准备自检',uuid.uuid4().hex,{2:1,5:4,7:6,9:8}[action])
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
        invoke(live['pid'],live['corePath'],self.support['coreSha256'],driver,action in (2,5,7,9))
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
        account=getattr(self,'config',{}).get('accountId') or self.live()['accountId']
        require(item['accountId']==account,'ACCOUNT_MISMATCH','发送操作属于其他账号。')
        # All public calls hold Exclusive. Native submission is only reachable
        # after submit() durably changes reserved to submitting.
        if item['status']=='reserved':
            item.update(status='not_submitted',error='INTERRUPTED_BEFORE_SUBMISSION')
            log.save(key,item)
        if item['status'] in ('confirmed','not_submitted','closed'):return self.public(item)
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
        if item.get('kind') in ('image','file'):return media_receipt_matches(row,item)
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
        if item.get('kind') in ('image','file'):
            result.update(messageType=3 if item['kind']=='image' else 49,
                          attachment={k:v for k,v in item['attachment'].items() if k not in ('sourcePath','nativePath')})
        if item['status']=='unknown':result['message']='结果尚未确认；请用同一操作编号查询，不要重发。'
        if item['status']=='closed':result['message']='已人工关闭待核对操作，真实发送结果仍未知；原编号不会重新发送。'
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
    def require_media(self,kind):
        require(self.support.get('mediaCapabilities',{}).get(kind) is True,'MEDIA_NATIVE_UNAVAILABLE',
                '此构建尚未适配该媒体类型，请先构建匹配版本的原生驱动；未发送。')
    def send_media(self,kind,recipient,path,key,wait=15,expected_sha256=None):
        require(kind in ('image','file'),'INVALID_ARGUMENT','媒体类型无效。')
        recipient=self.recipient(recipient);live=self.live();log=Operations(self.data/'operations.sqlite3')
        try:
            old=log.get(key)
            if old:
                require(isinstance(path,str) and bool(path) and '\0' not in path,'INVALID_MEDIA_PATH','文件路径无效。')
                same=(old.get('kind')==kind and old['accountId']==live['accountId'] and old['recipientId']==recipient and
                      old.get('attachment',{}).get('sourcePath')==str(Path(path).resolve()))
                if expected_sha256 is not None:same=same and isinstance(expected_sha256,str) and old.get('attachment',{}).get('sha256')==expected_sha256.lower()
                require(same,'OPERATION_CONFLICT','同编号的账号、收件人或文件发生变化，未发送。')
                return self.reconcile(key,old,log,wait)
            self.require_media(kind)
            baseline=[x['serverId'] for x in self.rows(recipient,200) if x['direction']=='outgoing']
            # Shard discovery may trim caches. Stage only after it finishes, then
            # reserve immediately so cleanup protects the in-flight attachment.
            asset=stage_file(self.data,live['accountId'],path,kind,expected_sha256)
            item,fresh=log.reserve(key,live['accountId'],recipient,'',live['identity'],baseline,attachment=asset)
            return self.submit(key,item,log,wait) if fresh else self.reconcile(key,item,log,wait)
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
            elif item.get('kind') in ('image','file'):
                self.require_media(item['kind']);body=verify_staged(item['attachment']);action=7 if item['kind']=='image' else 9
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
    def close_send(self,key,reviewed=False):
        require(reviewed is True,'REVIEW_REQUIRED','必须由用户人工核对并明确同意解除阻塞；不会重发原消息。')
        require(self.config.get('accountId'),'ACCOUNT_NOT_BOUND','请先初始化绑定账号。')
        log=Operations(self.data/'operations.sqlite3')
        try:
            item=log.get(key);require(item,'OPERATION_NOT_FOUND','没有此操作编号的发送记录。')
            require(item['accountId']==self.config['accountId'],'ACCOUNT_MISMATCH','发送操作属于其他账号。')
            if item['status'] in ('reserved','confirmed','not_submitted','closed'):
                return self.reconcile(key,item,log)
            item.update(closedFromStatus=item['status'],status='closed',closedAt=time.time())
            log.save(key,item)
            return self.public(item)
        finally:log.close()

def dispatch(service,request):
    action=request.get('action')
    if action=='cache-status':return cache_policy.status(service.data)
    if action=='cache-clean':return cache_policy.clean(service.data,request.get('purge',False),request.get('includeKeys',False))
    if action=='cache-configure':return cache_policy.configure(service.data,request.get('maxMiB'),request.get('maxAgeDays'))
    if action in ('schedule-create','schedule-list','schedule-update','schedule-run'):return service.schedule(action,request)
    if action=='bind':return service.bind(request.get('account'))
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
    if action in ('send-image','send-file'):
        return service.send_media(action[5:],request.get('recipientId'),request.get('filePath'),request.get('operationKey'),request.get('wait',15),request.get('fileSha256'))
    if action=='send-reply':
        scene=request.get('scene','serious');decision(scene,[])
        return service.send(request.get('recipientId'),request.get('text'),request.get('operationKey'),request.get('wait',15),scene)
    if action=='send-status':return service.send_status(request.get('operationKey'),request.get('wait',0))
    if action=='send-close':return service.close_send(request.get('operationKey'),request.get('reviewed',False))
    raise PluginError('UNKNOWN_ACTION','不支持的插件操作。')

def main():
    try:
        raw=sys.stdin.buffer.read(128*1024+1)
        require(len(raw)<=128*1024,'REQUEST_TOO_LARGE','请求过大。')
        request=json.loads(raw.decode('utf8'));require(isinstance(request,dict),'INVALID_REQUEST','请求应为 JSON 对象。')
        service=WeChat()
        with Exclusive(service.data),contextlib.redirect_stdout(sys.stderr):
            try:result=dispatch(service,request)
            finally:
                if not str(request.get('action','')).startswith('cache-'):
                    try:cache_policy.clean(service.data)
                    except (OSError,ValueError):print('CACHE_CLEANUP_FAILED',file=sys.stderr)
        output={'ok':True,'result':result}
    except Exception as error:
        output={'ok':False,'error':{'code':getattr(error,'code','OPERATION_CONFLICT' if isinstance(error,OperationConflict) else 'LOCAL_BACKEND_ERROR'),'message':str(error)}}
    sys.stdout.write(json.dumps(output,ensure_ascii=False)+'\n');sys.stdout.flush()
    return 0 if output['ok'] else 2

if __name__=='__main__':raise SystemExit(main())
