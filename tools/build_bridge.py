"""编译当前版本直连驱动；不加载、不发送。"""
import hashlib
from pathlib import Path
import subprocess
import sys
import argparse
import json
import struct
import os
import shutil
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'tools/python-deps'))
import pefile
parser=argparse.ArgumentParser()
parser.add_argument('--output')
parser.add_argument('--profile',type=Path)
parser.add_argument('--core',type=Path)
parser.add_argument('--zig',default=os.environ.get('ZIG_EXE') or shutil.which('zig') or str(ROOT/'tools/zig/zig-x86_64-windows-0.17.0/zig.exe'))
args=parser.parse_args()
profile_path=args.profile or ROOT/'native/targets'/(json.loads((ROOT/'config/native-target.json').read_text(encoding='utf8'))['clientVersion']+'.json')
profile=json.loads(profile_path.read_text(encoding='utf8'))
args.output=args.output or f"direct-bridge-{profile['clientVersion']}.dll"
core=args.core or Path(json.loads((ROOT/'config/native-target.json').read_text(encoding='utf8'))['corePath'])
raw=core.read_bytes()
if hashlib.sha256(raw).hexdigest()!=profile['coreSha256']:raise RuntimeError('Core digest mismatch')
pe=pefile.PE(data=raw,fast_load=True)
constants={key:int(value,0) for key,value in profile['constants'].items()}
base=pe.OPTIONAL_HEADER.ImageBase
if struct.unpack('<Q',pe.get_data(constants['VECTOR_VTABLE']+16,8))[0]!=base+constants['SEND_RVA']:
    raise RuntimeError('Send entry does not match the verified RTTI vtable')
# 独立核对原生调用者的请求计数器读写目标，不能把旧地址只换成新版本号。
counter_load=pe.get_data(0xf055,7);counter_store=pe.get_data(0xf05f,7)
if counter_load[:3]!=b'\x48\x8b\x05' or counter_store[:3]!=b'\x48\x89\x05':raise RuntimeError('Counter instruction changed')
if 0xf05c+struct.unpack('<i',counter_load[3:])[0]!=constants['REQUEST_COUNTER'] or 0xf066+struct.unpack('<i',counter_store[3:])[0]!=constants['REQUEST_COUNTER']:
    raise RuntimeError('Request counter relocation mismatch')
lines=[f'#define BRIDGE_{k} 0x{v:x}u' for k,v in constants.items()]
for key in ('CONSTRUCTOR','CONTEXT','SEND','IMAGE_CONSTRUCTOR'):
    values=pe.get_data(constants[key+'_RVA'],32)
    lines.append('static const unsigned char BRIDGE_'+key+'_PREFIX[]={'+','.join(hex(b) for b in values)+'};')
(ROOT/'native/bridge_target.h').write_text('\n'.join(lines)+'\n',encoding='ascii')
if Path(args.output).name!=args.output or not args.output.endswith('.dll'):raise RuntimeError('Expected a DLL filename')
subprocess.run([args.zig,'cc','-shared','-O1','-fms-extensions',
    '-target','x86_64-windows-gnu',str(ROOT/'native/direct_bridge.c'),'-o',str(ROOT/'native'/args.output)],cwd=ROOT,check=True)
print(f'Built {args.output}; no send was attempted')
