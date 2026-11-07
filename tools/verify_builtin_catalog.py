"""从当前适配核心重新核对内置编码/标签，不读取账号数据，不发送消息。"""
import argparse
import hashlib
import json
from pathlib import Path
import re
import struct
import xml.etree.ElementTree as ET
import zlib

ROOT=Path(__file__).resolve().parents[1]
parser=argparse.ArgumentParser(description=__doc__)
parser.add_argument('--core',type=Path)
args=parser.parse_args()
target=json.loads((ROOT/'config/native-target.json').read_text(encoding='utf8'))
core=args.core or Path(target['corePath'])
raw=core.read_bytes()
if hashlib.sha256(raw).hexdigest()!=target['coreSha256']:raise RuntimeError('Core hash does not match the selected target')
matches=[]
for match in re.finditer(rb'\x78[\x01\x5e\x9c\xda]',raw):
    pos=match.start()
    if pos<4:continue
    length=struct.unpack_from('>I',raw,pos-4)[0]
    if not 32<=length<=1024*1024:continue
    try:
        decoder=zlib.decompressobj();value=decoder.decompress(raw[pos:pos+1024*1024],length+1)
    except zlib.error:continue
    if decoder.eof and len(value)==length and b'cn-value' in value and b'en-value' in value:
        xml=ET.fromstring(value)
        if xml.tag=='newemoji':matches.append((pos-4,value,xml))
if len(matches)!=1:raise RuntimeError('Expected one unambiguous embedded newemoji resource')
offset,xml_bytes,xml=matches[0]
modern={x.findtext('key'):x.findtext('cn-value') for x in xml.findall('emoji')}
path=ROOT/'plugin/runtime/builtin-expressions.json'
catalog=json.loads(path.read_text(encoding='utf8'));classic_count=modern_count=0
for item in catalog['items']:
    if item['text'].startswith('/:'):
        if item['text'].encode('utf8') not in raw or item['label'].encode('utf8') not in raw:
            raise RuntimeError('Classic panel expression changed: '+item['stickerId'])
        classic_count+=1
    else:
        if modern.get(item['text'])!=item['displayText']:raise RuntimeError('Modern panel expression changed: '+item['stickerId'])
        modern_count+=1
catalog.update(clientVersion=target['clientVersion'],coreSha256=target['coreSha256'],verification={
    'classicCodesAndLabels':'verified in client emoji-panel constants',
    'modernCodesAndLabels':'verified against embedded newemoji-config.xml','modernResourceFileOffset':hex(offset)})
path.write_text(json.dumps(catalog,ensure_ascii=False,indent=2)+'\n',encoding='utf8')
(ROOT/'reports').mkdir(parents=True,exist_ok=True)
(ROOT/f"reports/newemoji-config-{target['clientVersion']}.xml").write_bytes(xml_bytes)
report={'clientVersion':target['clientVersion'],'classicVerified':classic_count,'modernVerified':modern_count,
        'total':classic_count+modern_count,'resourceFileOffset':hex(offset),'passed':True}
(ROOT/f"reports/builtin-verification-{target['clientVersion']}.json").write_text(json.dumps(report,indent=2),encoding='utf8')
print(json.dumps(report))
