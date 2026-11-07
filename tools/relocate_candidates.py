"""跨版本只读重定位线索。结果不构成可调用 ABI 或运行时兼容性证明。"""
import bisect
import argparse
import json
from pathlib import Path
import re
import struct
import sys
sys.path.insert(0, str(Path(__file__).parent / 'python-deps'))
import pefile
from capstone import Cs, CS_ARCH_X86, CS_MODE_64, CS_GRP_JUMP, CS_GRP_CALL
from capstone.x86 import X86_OP_MEM, X86_REG_RIP
from pe_helpers import function_ranges

ROOT = Path(__file__).resolve().parents[1]
parser=argparse.ArgumentParser(description=__doc__)
parser.add_argument('--reference',type=Path,default=ROOT/'research/reference-4.1.10.27/Weixin.dll')
parser.add_argument('--target',type=Path,default=Path(r'C:\Program Files\Tencent\Weixin\4.1.15.50\Weixin.dll'))
parser.add_argument('--output',type=Path,default=ROOT/'reports/relocation-candidates.json')
args=parser.parse_args()
old = pefile.PE(str(args.reference), fast_load=True)
new = pefile.PE(str(args.target), fast_load=True)
md = Cs(CS_ARCH_X86, CS_MODE_64)
md.detail = True
old_functions = sorted(function_ranges(old))
starts = [x[0] for x in old_functions]
sections = [(s.VirtualAddress, s.get_data()) for s in new.sections if s.Characteristics & 0x20000000]


def locate(rva):
    index = bisect.bisect_right(starts, rva) - 1
    end = old_functions[index][1] if index >= 0 and old_functions[index][0] == rva else rva + 128
    length = min(end-rva, 1024)
    data = old.get_data(rva, length)
    mask = bytearray(b'\1' * len(data))
    for instruction in md.disasm(data, old.OPTIONAL_HEADER.ImageBase + rva):
        relative = any(o.type == X86_OP_MEM and o.mem.base == X86_REG_RIP for o in instruction.operands)
        offset = instruction.address - old.OPTIONAL_HEADER.ImageBase - rva
        enc = instruction.encoding
        if relative and enc.disp_size:
            mask[offset + enc.disp_offset:offset + enc.disp_offset + enc.disp_size] = b'\0' * enc.disp_size
        if (instruction.group(CS_GRP_CALL) or instruction.group(CS_GRP_JUMP)) and enc.imm_size:
            mask[offset + enc.imm_offset:offset + enc.imm_offset + enc.imm_size] = b'\0' * enc.imm_size
    # 从较长前缀开始，逐步缩短，短前缀只用于产生候选，不能作为执行依据。
    for prefix in (len(data), 768, 512, 384, 256, 192, 96, 64, 48, 32):
        if prefix > len(data):
            continue
        pattern = b''.join(re.escape(bytes([b])) if mask[i] else b'.' for i,b in enumerate(data[:prefix]))
        expression = re.compile(pattern, re.S)
        matches = []
        for base, code in sections:
            matches.extend(base + m.start() for m in expression.finditer(code))
        if matches:
            return {'oldRva': hex(rva), 'prefixLength': prefix, 'matches': [hex(x) for x in matches[:30]],
                    'totalMatches': len(matches), 'runtimeValidated': False}
    return {'oldRva': hex(rva), 'matches': [], 'runtimeValidated': False}


result = {'nativeRuntimeValidated': False, 'functions': {}, 'vtableFunctions': {}}
for name, rva in {'create_param2': 0xdf40, 'send_message': 0x1677a30, 'txt_message_ctor': 0x6b2c30}.items():
    result['functions'][name] = locate(rva)
    print(name, json.dumps(result['functions'][name]), flush=True)
for name, rva in {'param1_vtable':0x84ec9c8, 'param2_1':0x8595f58, 'param2_2':0x8595e98,
                  'param2_3':0x8595dd8, 'txt_message_vtable':0x8279358}.items():
    entries = struct.unpack('<QQQQ', old.get_data(rva, 32))
    mapped = [locate(x-old.OPTIONAL_HEADER.ImageBase) for x in entries
              if old.OPTIONAL_HEADER.ImageBase <= x < old.OPTIONAL_HEADER.ImageBase + old.OPTIONAL_HEADER.SizeOfImage]
    result['vtableFunctions'][name] = mapped
    print(name, json.dumps(mapped), flush=True)
args.output.parent.mkdir(parents=True,exist_ok=True)
args.output.write_text(json.dumps(result, indent=2), encoding='utf-8')
