"""Bound rebuildable local caches; never remove source files or send journals."""
import json
import os
from pathlib import Path
import sqlite3
import time

DEFAULT={'maxMiB':1024,'maxAgeDays':7}


class CacheError(ValueError):
    def __init__(self,code,message):super().__init__(message);self.code=code


def policy(data):
    file=Path(data)/'cache-policy.json'
    try:value=json.loads(file.read_text(encoding='utf8')) if file.exists() else dict(DEFAULT)
    except (OSError,ValueError) as error:raise CacheError('CACHE_POLICY_INVALID','缓存配置无法读取。') from error
    if (not isinstance(value,dict) or set(value)!=set(DEFAULT) or
            type(value.get('maxMiB')) is not int or not 1<=value['maxMiB']<=65536 or
            type(value.get('maxAgeDays')) is not int or not 1<=value['maxAgeDays']<=365):
        raise CacheError('CACHE_POLICY_INVALID','缓存限制需为 1–65536 MiB，保留期限需为 1–365 天。')
    return value


def configure(data,max_mib,max_age_days):
    value={'maxMiB':max_mib,'maxAgeDays':max_age_days}
    if type(max_mib) is not int or not 1<=max_mib<=65536 or type(max_age_days) is not int or not 1<=max_age_days<=365:
        raise CacheError('CACHE_POLICY_INVALID','缓存限制需为 1–65536 MiB，保留期限需为 1–365 天。')
    file=Path(data)/'cache-policy.json';temporary=file.with_suffix('.tmp')
    temporary.write_text(json.dumps(value),encoding='utf8');temporary.replace(file)
    return status(data)


def _safe(path,root):
    try:
        if not path.resolve().is_relative_to(root):return False
        current=path
        while current!=root:
            if current.is_symlink() or current.is_junction():return False
            current=current.parent
        return True
    except OSError:return False


def _files(folder,root):
    if not folder.is_dir() or not _safe(folder,root):return
    for base,dirs,names in os.walk(folder,followlinks=False):
        parent=Path(base)
        dirs[:]=[name for name in dirs if _safe(parent/name,root)]
        for name in names:
            path=parent/name
            if _safe(path,root) and path.is_file():yield path


def _inventory(data,include_keys=False):
    root=Path(data).resolve()
    if root==Path(root.anchor):raise CacheError('CACHE_ROOT_INVALID','缓存目录不能是磁盘根目录。')
    protected=set();uncertain=False
    journal=root/'operations.sqlite3'
    if journal.exists():
        try:
            connection=sqlite3.connect(journal.as_uri()+'?mode=ro',uri=True)
            try:
                for row in connection.execute("SELECT json_extract(body,'$.attachment.nativePath') FROM operations "
                        "WHERE status IN ('reserved','submitting','submitted','unknown','closed')"):
                    if row[0]:protected.add(Path(row[0]).resolve())
            finally:connection.close()
        except (sqlite3.Error,ValueError,OSError):uncertain=True
    bases=[root]
    accounts=root/'accounts'
    if accounts.is_dir() and _safe(accounts,root):bases.extend(p for p in accounts.iterdir() if p.is_dir() and _safe(p,root))
    entries=[]
    for base in bases:
        for name in ('db','identity'):
            folder=base/name
            for path in _files(folder,root):
                if path.name.endswith('.db'):
                    stamp=Path(str(path)+'.stamp')
                    files=[path]+([stamp] if stamp.is_file() and _safe(stamp,root) else [])
                    entries.append((files,False))
                elif '.tmp' in path.name or (include_keys and path.name in ('keys.json','keys.json.bak')):
                    entries.append(([path],False))
                elif path.name.endswith('.stamp') and not Path(str(path)[:-6]).exists():
                    entries.append(([path],False))
            if include_keys:
                index=base/(name+'.tables.json')
                if index.is_file() and _safe(index,root):entries.append(([index],False))
        if include_keys:
            entries.extend(([path],False) for path in _files(base/'keys',root))
        # Native drivers, account bindings, operations and schedules are excluded.
    for path in _files(root/'media',root):entries.append(([path],uncertain or path.resolve() in protected))
    groups=[]
    seen=set()
    for files,keep in entries:
        unique=[path for path in files if path not in seen]
        if not unique:continue
        seen.update(unique)
        try:stats=[path.stat() for path in unique]
        except FileNotFoundError:continue
        groups.append({'files':unique,'bytes':sum(x.st_size for x in stats),'usedAt':max(x.st_mtime for x in stats),'protected':keep})
    return groups,uncertain


def status(data):
    value=policy(data);groups,uncertain=_inventory(data)
    total=sum(x['bytes'] for x in groups)
    return {'policy':value,'bytes':total,'files':sum(len(x['files']) for x in groups),
            'protectedBytes':sum(x['bytes'] for x in groups if x['protected']),
            'overLimit':total>value['maxMiB']*1024*1024,'pendingAttachmentsUnknown':uncertain}


def clean(data,purge=False,include_keys=False,now=None,keep_paths=()):
    if type(purge) is not bool or type(include_keys) is not bool:
        raise CacheError('INVALID_ARGUMENT','purge / includeKeys 必须为布尔值。')
    root=Path(data).resolve();value=policy(root);groups,uncertain=_inventory(root,include_keys)
    keep={Path(path).resolve() for path in keep_paths}
    for group in groups:
        if any(path.resolve() in keep for path in group['files']):group['protected']=True
    total=sum(x['bytes'] for x in groups);removed=removed_bytes=errors=0
    cutoff=(time.time() if now is None else now)-value['maxAgeDays']*86400
    for group in sorted(groups,key=lambda x:x['usedAt']):
        if group['protected'] or not (purge or group['usedAt']<cutoff or total>value['maxMiB']*1024*1024):continue
        for path in group['files']:
            try:
                if not _safe(path,root):continue
                size=path.stat().st_size;path.unlink();removed+=1;removed_bytes+=size;total-=size
            except FileNotFoundError:pass
            except OSError:errors+=1
    return {'removedFiles':removed,'removedBytes':removed_bytes,'remainingBytes':max(0,total),
            'overLimit':total>value['maxMiB']*1024*1024,'cleanupErrors':errors,'pendingAttachmentsUnknown':uncertain}
