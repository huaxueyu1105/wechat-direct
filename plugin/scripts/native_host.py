"""Windows 原生模块调用。仅加载本插件自己的驱动，不修改微信程序文件。"""
import ctypes as C
from ctypes import wintypes as W
import hashlib
from pathlib import Path

k=C.WinDLL('kernel32',use_last_error=True)
class Module(C.Structure):
    _fields_=[('dwSize',W.DWORD),('th32ModuleID',W.DWORD),('th32ProcessID',W.DWORD),
        ('GlblcntUsage',W.DWORD),('ProccntUsage',W.DWORD),('base',C.c_void_p),('size',W.DWORD),
        ('hModule',W.HMODULE),('name',W.WCHAR*256),('path',W.WCHAR*260)]
def api(name,result,args):
    fn=getattr(k,name);fn.restype=result;fn.argtypes=args;return fn
api('CreateToolhelp32Snapshot',W.HANDLE,[W.DWORD,W.DWORD])
api('Module32FirstW',W.BOOL,[W.HANDLE,C.POINTER(Module)])
api('Module32NextW',W.BOOL,[W.HANDLE,C.POINTER(Module)])
api('CloseHandle',W.BOOL,[W.HANDLE])
api('OpenProcess',W.HANDLE,[W.DWORD,W.BOOL,W.DWORD])
api('VirtualAllocEx',C.c_void_p,[W.HANDLE,C.c_void_p,C.c_size_t,W.DWORD,W.DWORD])
api('VirtualFreeEx',W.BOOL,[W.HANDLE,C.c_void_p,C.c_size_t,W.DWORD])
api('WriteProcessMemory',W.BOOL,[W.HANDLE,C.c_void_p,C.c_void_p,C.c_size_t,C.POINTER(C.c_size_t)])
api('CreateRemoteThread',W.HANDLE,[W.HANDLE,C.c_void_p,C.c_size_t,C.c_void_p,C.c_void_p,W.DWORD,C.c_void_p])
api('WaitForSingleObject',W.DWORD,[W.HANDLE,W.DWORD])
api('GetExitCodeThread',W.BOOL,[W.HANDLE,C.POINTER(W.DWORD)])
api('GetModuleHandleW',W.HMODULE,[W.LPCWSTR])
api('GetProcAddress',C.c_void_p,[W.HMODULE,C.c_char_p])
api('GetCurrentProcessId',W.DWORD,[])
api('GetProcessTimes',W.BOOL,[W.HANDLE,C.POINTER(W.FILETIME),C.POINTER(W.FILETIME),C.POINTER(W.FILETIME),C.POINTER(W.FILETIME)])
api('CreateMutexW',W.HANDLE,[C.c_void_p,W.BOOL,W.LPCWSTR])
api('ReleaseMutex',W.BOOL,[W.HANDLE])

class NativeError(RuntimeError):
    def __init__(self,message,possible_submission=False):
        super().__init__(message);self.possible_submission=possible_submission

class Exclusive:
    def __init__(self,data_path):self.name='Local\\CodexWeChatDirect_'+hashlib.sha256(str(data_path).casefold().encode()).hexdigest()[:24]
    def __enter__(self):
        self.handle=k.CreateMutexW(None,False,self.name)
        if not self.handle:raise C.WinError(C.get_last_error())
        if k.WaitForSingleObject(self.handle,20000) not in (0,0x80):
            k.CloseHandle(self.handle);raise NativeError('插件正忙，请稍后重试；未执行新的发送。')
        return self
    def __exit__(self,*args):k.ReleaseMutex(self.handle);k.CloseHandle(self.handle)

def modules(pid):
    handle=k.CreateToolhelp32Snapshot(0x18,pid)
    if handle==C.c_void_p(-1).value:raise C.WinError(C.get_last_error())
    try:
        m=Module();m.dwSize=C.sizeof(m);found=k.Module32FirstW(handle,C.byref(m));result=[]
        while found:
            result.append({'name':m.name,'path':m.path,'base':m.base,'size':m.size})
            found=k.Module32NextW(handle,C.byref(m))
        return result
    finally:k.CloseHandle(handle)

def process_identity(pid):
    handle=k.OpenProcess(0x1000,False,pid)
    if not handle:raise C.WinError(C.get_last_error())
    try:
        times=[W.FILETIME() for _ in range(4)]
        if not k.GetProcessTimes(handle,*[C.byref(x) for x in times]):raise C.WinError(C.get_last_error())
        return f'{pid}:{(times[0].dwHighDateTime<<32)|times[0].dwLowDateTime}'
    finally:k.CloseHandle(handle)

def invoke(pid,core_path,core_digest,dll_path,possible_submission):
    import pefile
    dll_path=Path(dll_path).resolve()
    with Path(core_path).open('rb') as stream:
        if hashlib.file_digest(stream,'sha256').hexdigest()!=core_digest:raise NativeError('微信版本已变化，请更新插件适配。')
    remote=modules(pid)
    core=next((x for x in remote if x['name'].casefold()=='weixin.dll'),None)
    if not core or Path(core['path']).resolve()!=Path(core_path).resolve():raise NativeError('微信进程已变化，请重新检查连接。')
    process=k.OpenProcess(0x0002|0x0400|0x0008|0x0010|0x0020,False,pid)
    if not process:raise NativeError('无法访问当前微信进程，请在同一 Windows 用户下运行。')
    def call(address,argument,may_send=False):
        thread=k.CreateRemoteThread(process,None,0,address,argument,0,None)
        if not thread:raise NativeError('无法启动原生调用，尚未提交。')
        try:
            if k.WaitForSingleObject(thread,10000)!=0:
                raise NativeError('原生调用尚未返回，请先查询发送结果，不要重发。',may_send)
            code=W.DWORD()
            if not k.GetExitCodeThread(thread,C.byref(code)):
                raise NativeError('无法读取原生调用结果，请先核对发送记录。',may_send)
            return code.value
        finally:k.CloseHandle(thread)
    try:
        loaded=next((x for x in remote if Path(x['path']).resolve()==dll_path),None)
        if not loaded:
            loader=k.GetProcAddress(k.GetModuleHandleW('kernel32.dll'),b'LoadLibraryW')
            owner=next(x for x in modules(k.GetCurrentProcessId()) if x['base']<=loader<x['base']+x['size'])
            other=next(x for x in remote if x['name'].casefold()==owner['name'].casefold())
            buffer=C.create_unicode_buffer(str(dll_path));size=C.sizeof(buffer)
            argument=k.VirtualAllocEx(process,None,size,0x3000,4)
            if not argument:raise NativeError('无法分配驱动路径参数，尚未提交。')
            finished=False
            try:
                written=C.c_size_t()
                if not k.WriteProcessMemory(process,argument,buffer,size,C.byref(written)) or written.value!=size:
                    raise NativeError('驱动路径写入失败，尚未提交。')
                call(other['base']+loader-owner['base'],argument);finished=True
            finally:
                if finished:k.VirtualFreeEx(process,argument,0,0x8000)
            loaded=next((x for x in modules(pid) if Path(x['path']).resolve()==dll_path),None)
        if not loaded:raise NativeError('Windows 未加载驱动。请检查应用控制/签名拦截记录；尚未发送。')
        pe=pefile.PE(str(dll_path),fast_load=True)
        pe.parse_data_directories([pefile.DIRECTORY_ENTRY['IMAGE_DIRECTORY_ENTRY_EXPORT']])
        entry=next(x.address for x in pe.DIRECTORY_ENTRY_EXPORT.symbols if x.name==b'ExecuteJob')
        return call(loaded['base']+entry,None,possible_submission)
    finally:k.CloseHandle(process)
