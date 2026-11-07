"""发送操作日志：先持久化、同键不重发、结果未知时仅核对记录。"""
import hashlib
import json
import sqlite3
import time
from reply_policy import compose,decision

class OperationConflict(RuntimeError):pass
class StickerRateLimit(OperationConflict):code='STICKER_RATE_LIMIT'

class Operations:
    def __init__(self,path):
        self.db=sqlite3.connect(path,timeout=15)
        self.db.execute('PRAGMA journal_mode=WAL');self.db.execute('PRAGMA synchronous=FULL')
        self.db.execute('CREATE TABLE IF NOT EXISTS operations (keyhash TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, account TEXT NOT NULL, recipient TEXT NOT NULL, status TEXT NOT NULL, process_identity TEXT NOT NULL, body TEXT NOT NULL)')
        self.db.commit()
    @staticmethod
    def key(value):
        if not isinstance(value,str) or not 8<=len(value)<=200:raise OperationConflict('operationKey 需为 8–200 字符的稳定操作编号。')
        return hashlib.sha256(value.encode('utf8')).hexdigest()
    def get(self,key):
        row=self.db.execute('SELECT body FROM operations WHERE keyhash=?',(self.key(key),)).fetchone()
        return json.loads(row[0]) if row else None
    def reserve(self,key,account,recipient,text,process_identity,baseline,maximum=1024,reply_scene=None,
                sticker=None,sticker_mode='contextual',sticker_scene='serious'):
        keyhash=self.key(key)
        request=[account,recipient,text]
        if reply_scene is not None:request.append({'replyScene':reply_scene})
        if sticker is not None:request.append({'stickerId':sticker['stickerId'],'mode':sticker_mode,'scene':sticker_scene})
        fingerprint=hashlib.sha256(json.dumps(request,ensure_ascii=False).encode()).hexdigest()
        self.db.execute('BEGIN IMMEDIATE')
        try:
            row=self.db.execute('SELECT fingerprint,body FROM operations WHERE keyhash=?',(keyhash,)).fetchone()
            if row:
                if row[0]!=fingerprint:raise OperationConflict('同一操作编号的收件人或正文发生变化，未执行新的发送。')
                self.db.commit();return json.loads(row[1]),False
            pending=self.db.execute("SELECT 1 FROM operations WHERE account=? AND recipient=? AND status IN ('reserved','submitting','submitted','unknown') LIMIT 1",(account,recipient)).fetchone()
            if pending:raise OperationConflict('此会话有尚未确认结果的发送，请先查询原操作，不要换编号重发。')
            count=self.db.execute('SELECT COUNT(*) FROM operations WHERE process_identity=?',(process_identity,)).fetchone()[0]
            if count>=maximum:raise OperationConflict('当前微信进程已达到本版本的发送容量，请正常重启微信后继续。')
            reply_policy=None
            if reply_scene is not None:
                text,reply_policy=compose(text,reply_scene,self.conversation_history(account,recipient))
            if sticker is not None:
                reply_policy=decision(sticker_scene,self.conversation_history(account,recipient))
                if sticker_mode=='contextual' and not reply_policy['useExpression']:
                    raise StickerRateLimit('当前场景或表情频率不合适，未发送；已有文字已发出时不要再补发重复文字。')
                reply_policy.update(useExpression=True,explicitUserRequest=sticker_mode=='explicit')
            item={'operationKey':key,'operationHash':keyhash,'job':keyhash[:32],'fingerprint':fingerprint,'accountId':account,
                'recipientId':recipient,'text':text,'status':'reserved','processIdentity':process_identity,
                'baselineServerIds':baseline,'startedAt':time.time()}
            if reply_policy is not None:item['replyPolicy']=reply_policy
            if sticker is not None:
                item.update(kind=sticker.get('kind','sticker'),sticker=sticker,stickerMode=sticker_mode,stickerScene=sticker_scene)
                if item['kind']=='builtin':item['text']=sticker['text']
            self.db.execute('INSERT INTO operations VALUES (?,?,?,?,?,?,?)',
                (keyhash,fingerprint,account,recipient,'reserved',process_identity,json.dumps(item,ensure_ascii=False)))
            self.db.commit();return item,True
        except Exception:self.db.rollback();raise
    def save(self,key,item):
        self.db.execute('UPDATE operations SET status=?,body=? WHERE keyhash=?',
            (item['status'],json.dumps(item,ensure_ascii=False),self.key(key)));self.db.commit()
    def recent(self,account,limit=20):
        return [json.loads(row[0]) for row in self.db.execute('SELECT body FROM operations WHERE account=? ORDER BY rowid DESC LIMIT ?',
            (account,max(1,min(int(limit),100))))]
    def conversation_history(self,account,recipient):
        return [json.loads(row[0]) for row in self.db.execute(
            "SELECT body FROM operations WHERE account=? AND recipient=? AND status!='not_submitted' ORDER BY rowid DESC LIMIT 19",
            (account,recipient))]
    def close(self):self.db.close()
