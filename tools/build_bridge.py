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
parser.add_argument('--verify-only',action='store_true',help='Verify the offline target without writing or compiling a driver')
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
capabilities=profile.get('mediaCapabilities',{})
if any(type(capabilities.get(kind,False)) is not bool for kind in ('image','file')):
    raise RuntimeError('Media capabilities must be booleans')

def expect_bytes(rva,value):
    if pe.get_data(rva,len(value))!=value:raise RuntimeError(f'Media layout instruction changed at {rva:#x}')

def relative_target(rva,opcode):
    expect_bytes(rva,opcode)
    length=len(opcode)+4
    return rva+length+struct.unpack('<i',pe.get_data(rva+len(opcode),4))[0]

# Exact 4.2.0.13 factory/handler instructions independently identify the media ABI.
# A version-string change or a guessed constructor is insufficient to enable it.
if any(capabilities.values()):
    if profile['clientVersion']!='4.2.0.13':raise RuntimeError('No media layout checks for this target')
    if capabilities.get('image'):
        if relative_target(0x2e86af7,b'\xe8')!=constants['IMAGE_CONSTRUCTOR_RVA']:
            raise RuntimeError('Ordinary image constructor mismatch')
        expect_bytes(0x2e86afc,bytes.fromhex('c785085d000003000000')) # source +0x118: type 3
    if capabilities.get('file'):
        if relative_target(0x2e86e23,b'\xe8')!=constants['FILE_CONSTRUCTOR_RVA'] or \
                relative_target(0xbe4a2f,b'\x48\x8d\x05')!=constants['FILE_VTABLE'] or \
                relative_target(0xbe4f83,b'\x48\x8d\x0d')!=constants['FILE_CONTROL_VTABLE']:
            raise RuntimeError('File source constructor or vtable mismatch')
        expect_bytes(0x2e86e28,bytes.fromhex('48b83100000006000000488985085d0000')) # source +0x118: 49/6
        expect_bytes(0x3a6725f,bytes.fromhex('ba3100000041b806000000')) # FileMessageHandler registration
        expect_bytes(0x3ad3264,bytes.fromhex('498d9720010000')) # UTF-16 source path +0x120
        expect_bytes(0x3ad357c,bytes.fromhex('4d8bb7b8010000')) # optional filename length +0x1b8
if args.verify_only:
    print(f"Verified {profile['clientVersion']} offline media layout; no DLL was loaded")
    sys.exit(0)
lines=[f'#define BRIDGE_{k} 0x{v:x}u' for k,v in constants.items()]
lines.extend(f'#define BRIDGE_HAS_{kind.upper()} {int(capabilities.get(kind,False))}' for kind in ('image','file'))
prefixes=['CONSTRUCTOR','CONTEXT','SEND','IMAGE_CONSTRUCTOR']
if capabilities.get('file'):prefixes.append('FILE_CONSTRUCTOR')
for key in prefixes:
    values=pe.get_data(constants[key+'_RVA'],32)
    lines.append('static const unsigned char BRIDGE_'+key+'_PREFIX[]={'+','.join(hex(b) for b in values)+'};')
(ROOT/'native/bridge_target.h').write_text('\n'.join(lines)+'\n',encoding='ascii')
if Path(args.output).name!=args.output or not args.output.endswith('.dll'):raise RuntimeError('Expected a DLL filename')
subprocess.run([args.zig,'cc','-shared','-O1','-fms-extensions',
    '-target','x86_64-windows-gnu',str(ROOT/'native/direct_bridge.c'),'-o',str(ROOT/'native'/args.output)],cwd=ROOT,check=True)
print(f'Built {args.output}; no send was attempted')
