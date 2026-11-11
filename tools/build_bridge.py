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
capabilities = profile.get('mediaCapabilities', {})
if any(type(capabilities.get(kind, False)) is not bool for kind in ('image', 'file')):
    raise RuntimeError('Media capabilities must be booleans')


def expect_bytes(rva, value, label):
    expected = bytes.fromhex(value)
    if pe.get_data(rva, len(expected)) != expected:
        raise RuntimeError(f'{label} at {rva:#x}')


def expect_relative(rva, opcode, target, label):
    expect_bytes(rva, opcode, label)
    displacement_rva = rva + len(bytes.fromhex(opcode))
    displacement = struct.unpack('<i', pe.get_data(displacement_rva, 4))[0]
    if displacement_rva + 4 + displacement != target:
        raise RuntimeError(f'{label} at {rva:#x}')


version = profile['clientVersion']
counter_instructions = {
    '4.2.0.12': (0xf055, '488b05', '48ffc0', '488905'),
    '4.2.0.13': (0xf055, '488b05', '48ffc0', '488905'),
    '4.2.0.52': (0xecf9, '488b3d', '48ffc7', '48893d'),
}
if version not in counter_instructions:
    raise RuntimeError('No request counter layout checks for this target')
counter_load, load_opcode, increment, store_opcode = counter_instructions[version]
expect_relative(counter_load, load_opcode, constants['REQUEST_COUNTER'], 'Request counter relocation mismatch')
expect_bytes(counter_load + 7, increment, 'Request counter increment changed')
expect_relative(counter_load + 10, store_opcode, constants['REQUEST_COUNTER'], 'Request counter relocation mismatch')

if version == '4.2.0.52':
    # The global_config initializer identifies the string scanned by the reader.
    landmark = 0xbb6f8a8
    expect_bytes(0x46641, '48b8676c6f62616c5f63', 'Account config landmark changed')
    expect_relative(0x4664b, '488905', landmark, 'Account config landmark moved')
    expect_bytes(0x46652, '48b86c5f636f6e666967', 'Account config landmark changed')
    expect_relative(0x4665c, '488905', landmark + 5, 'Account config landmark moved')
    pointer_back = int(profile['accountConfigPointerBack'], 0)
    expect_relative(0x1fc90, '488b05', landmark + 16 - pointer_back, 'Account config pointer relocation mismatch')

    # Check the context call, text control block and common message fields.
    expect_relative(0xef83, 'e8', constants['CONTEXT_RVA'], 'Context constructor mismatch')
    expect_relative(0x7773b3, '488d0d', constants['MESSAGE_CONTROL_VTABLE'], 'Text control vtable mismatch')
    expect_relative(constants['CONSTRUCTOR_RVA'] + 0x2a, 'e8', 0x7760a0, 'Text constructor mismatch')
    expect_bytes(constants['CONSTRUCTOR_RVA'] + 0x78, '0f11865807000048c7866807000000000000', 'Text body layout changed')
    expect_bytes(0x776139, '0f11b1b000000048c781c000000000000000', 'Recipient layout changed')
    expect_bytes(0x77617c, '48c781180100000000000048894de04881c120010000', 'Message type or path layout changed')
    expect_bytes(0x77623f, '0f11b2c8010000', 'Message length layout changed')
    expect_bytes(0x7762bd, '498d86f80600004989be08070000', 'Message UUID layout changed')
    for offset, instruction in ((0x32, '48c7464800000000'), (0x84, '48c7868800000000000000'),
                                (0xe0, '48c786c800000000000000'), (0x133, '48899ed0000000')):
        expect_bytes(constants['CONTEXT_RVA'] + offset, instruction, 'Send context layout changed')
    locator = struct.unpack('<Q', pe.get_data(constants['VECTOR_VTABLE'] - 8, 8))[0] - base
    signature, _, _, descriptor, _, self_rva = struct.unpack('<6I', pe.get_data(locator, 24))
    if signature != 1 or self_rva != locator or descriptor != constants['TYPE_DESCRIPTOR']:
        raise RuntimeError('Send vector type descriptor mismatch')

# The factory calls identify the media constructors. The handler reads the file path and name.
media_layouts = {
    '4.2.0.13': {
        'image': (0x2e86af7, 'c785085d000003000000'),
        'file': (0x2e86e23, '48b83100000006000000488985085d0000',
                 0xbe4a2f, 0xbe4f83, 0x3a6725f, 0x3ad3264, 0x3ad357c),
    },
    '4.2.0.52': {
        'image': (0x2dceb67, 'c785285d000003000000'),
        'file': (0x2dcee93, '48b83100000006000000488985285d0000',
                 0xbb973f, 0xbb9c93, 0x399086f, 0x39e8e54, 0x39e916c),
    },
}
if any(capabilities.values()) and version not in media_layouts:
    raise RuntimeError('No media layout checks for this target')
if capabilities.get('image') or version == '4.2.0.52':
    factory_call, image_type = media_layouts[version]['image']
    expect_relative(factory_call, 'e8', constants['IMAGE_CONSTRUCTOR_RVA'], 'Ordinary image constructor mismatch')
    expect_bytes(factory_call + 5, image_type, 'Image type store changed')
if version == '4.2.0.52':
    # Stickers share the image object and its control block.
    expect_relative(0x1a0e77f, '488d05', constants['IMAGE_VTABLE'], 'Image object vtable mismatch')
    expect_relative(0x18365c3, '488d0d', constants['IMAGE_CONTROL_VTABLE'], 'Image control vtable mismatch')
if capabilities.get('file'):
    factory_call, file_type, object_lea, control_lea, registration, path_load, filename_load = media_layouts[version]['file']
    label = 'File source constructor or vtable mismatch'
    expect_relative(factory_call, 'e8', constants['FILE_CONSTRUCTOR_RVA'], label)
    expect_relative(object_lea, '488d05', constants['FILE_VTABLE'], label)
    expect_relative(control_lea, '488d0d', constants['FILE_CONTROL_VTABLE'], label)
    expect_bytes(factory_call + 5, file_type, 'File type store changed')
    expect_bytes(registration, 'ba3100000041b806000000', 'File handler registration changed')
    expect_bytes(path_load, '498d9720010000', 'File source path layout changed')
    expect_bytes(filename_load, '4d8bb7b8010000', 'File name layout changed')
if args.verify_only:
    print(f'Verified {version} offline native layout; no DLL was loaded')
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
