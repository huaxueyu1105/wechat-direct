"""用完整 RTTI 类型名定位跨版本虚表，保留所有候选，不自动调用。"""
import json
import argparse
from pathlib import Path
import struct
import sys
sys.path.insert(0, str(Path(__file__).parent / 'python-deps'))
import pefile

ROOT = Path(__file__).resolve().parents[1]
parser=argparse.ArgumentParser(description=__doc__)
parser.add_argument('--reference',type=Path,default=ROOT/'research/reference-4.1.10.27/Weixin.dll')
parser.add_argument('--target',type=Path,default=Path(r'C:\Program Files\Tencent\Weixin\4.1.15.50\Weixin.dll'))
parser.add_argument('--output',type=Path,default=ROOT/'reports/rtti-relocation.json')
args=parser.parse_args()
old = pefile.PE(str(args.reference), fast_load=True)
new_path = args.target
raw = new_path.read_bytes()
new = pefile.PE(data=raw, fast_load=True)
ob, nb = old.OPTIONAL_HEADER.ImageBase, new.OPTIONAL_HEADER.ImageBase
targets = {'txt_message_vtable':0x8279358, 'param1_vtable':0x84ec9c8,
           'param2_1':0x8595f58, 'param2_2':0x8595e98, 'param2_3':0x8595dd8,
           'async_send_vtable':0x84ecaa8, 'image_control':0x84f96b8, 'image_vtable':0x84f9748}
result = {}
for name, vtable in targets.items():
    locator = struct.unpack('<Q', old.get_data(vtable-8,8))[0]-ob
    descriptor = struct.unpack('<IIIIII', old.get_data(locator,24))[3]
    type_name = old.get_data(descriptor+16,2048).split(b'\0',1)[0]
    name_matches = []; vt_matches = []
    at = 0
    while True:
        at = raw.find(type_name+b'\0', at)
        if at < 0: break
        td = new.get_rva_from_offset(at)-16
        name_matches.append(hex(td))
        # 类型描述符指针在 x64 COL 中是 RVA；校验 signature 和 pSelf。
        needle = struct.pack('<I', td)
        for section in new.sections:
            if section.Characteristics & 0x20000000: continue
            data = section.get_data(); start = 0
            while True:
                pos = data.find(needle,start)
                if pos < 0: break
                start = pos+1
                if pos<12:continue
                col_rva=section.VirtualAddress+pos-12
                fields=struct.unpack('<IIIIII',data[pos-12:pos+12])
                if fields[0]!=1 or fields[5]!=col_rva:continue
                pointer=struct.pack('<Q',nb+col_rva)
                for ref_section in new.sections:
                    if ref_section.Characteristics & 0x20000000:continue
                    refs=ref_section.get_data(); p=0
                    while True:
                        p=refs.find(pointer,p)
                        if p<0:break
                        new_vt=ref_section.VirtualAddress+p+8
                        entries=struct.unpack('<QQQQQQ',new.get_data(new_vt,48))
                        vt_matches.append({'rva':hex(new_vt),'entries':[hex(x-nb) for x in entries]})
                        p+=1
        at+=1
    result[name]={'oldRva':hex(vtable),'fullTypeNameBytes':len(type_name),
                  'typeDescriptorMatches':name_matches,'vtableMatches':vt_matches,'runtimeValidated':False}
    print(name,json.dumps(result[name]),flush=True)
args.output.parent.mkdir(parents=True,exist_ok=True)
args.output.write_text(json.dumps(result,indent=2),encoding='utf-8')
