"""生成不含账号、聊天或密钥的本机插件安装包。"""
import hashlib
import json
from pathlib import Path
import shutil
import zipfile
import importlib.metadata
import PIL
import argparse

parser=argparse.ArgumentParser(description=__doc__)
parser.add_argument('--prepare-only',action='store_true',help='Only refresh runtime hashes before live validation')
args=parser.parse_args()

ROOT=Path(__file__).resolve().parents[1]
SOURCE=ROOT/'plugin'
(ROOT/'reports').mkdir(parents=True,exist_ok=True)
(ROOT/'release').mkdir(parents=True,exist_ok=True)
VERSION=json.loads((SOURCE/'plugin.json').read_text(encoding='utf8'))['version']
bundle=ROOT/f'release/wechat-local-plugin-{VERSION}'
plugin=bundle/'plugins/wechat-desktop'
target=json.loads((ROOT/'config/native-target.json').read_text(encoding='utf8'))
profile=json.loads((ROOT/'native/targets'/(target['clientVersion']+'.json')).read_text(encoding='utf8'))
if target['coreSha256']!=profile['coreSha256']:raise RuntimeError('Packaging target and native profile disagree')
support={'builtinCatalogSha256':hashlib.sha256((SOURCE/'runtime/builtin-expressions.json').read_bytes()).hexdigest(),'clientVersion':profile['clientVersion'],'coreSha256':profile['coreSha256'],
    'driverSha256':hashlib.sha256((SOURCE/'runtime/native/direct-bridge-v2.dll').read_bytes()).hexdigest(),
    'stickerDriverSha256':hashlib.sha256((SOURCE/'runtime/native/direct-bridge-v3.dll').read_bytes()).hexdigest(),
    'readerSha256':hashlib.sha256((SOURCE/'runtime/vendor/wechat_db.py').read_bytes()).hexdigest(),
    'platform':'windows-x64','python':'3.12-x64','maxSendOperationsPerClientProcess':1024,
    'nativeValidation':profile['validation'],'preflightBeforeFirstSubmission':True,
    'mediaCapabilities':profile.get('mediaCapabilities',{'image':False,'file':False})}
(SOURCE/'runtime/support.json').write_text(json.dumps(support,indent=2)+'\n',encoding='utf8')
if args.prepare_only:
    print(json.dumps({'clientVersion':support['clientVersion'],'supportPrepared':True}));raise SystemExit(0)
deps=SOURCE/'runtime/python-deps'
if deps.resolve().parent!=(SOURCE/'runtime').resolve() or deps.is_symlink():raise RuntimeError('Unsafe runtime dependency directory')
if deps.exists():shutil.rmtree(deps)
deps.mkdir(parents=True,exist_ok=True)
names=['cryptography','cryptography-50.0.2.dist-info','cffi','cffi-2.1.1.dist-info','pycparser','pycparser-3.12.dist-info',
    'pefile.py','pefile-2024.8.26.dist-info','ordlookup','zstandard','zstandard-0.25.0.dist-info']
names.extend(x.name for x in (ROOT/'tools/python-deps').glob('_cffi_backend*.pyd'))
for name in names:
    source=ROOT/'tools/python-deps'/name;destination=deps/name
    if source.is_dir():shutil.copytree(source,destination,dirs_exist_ok=True,ignore=shutil.ignore_patterns('__pycache__','*.pyc'))
    else:shutil.copyfile(source,destination)
pil_source=Path(PIL.__file__).parent
shutil.copytree(pil_source,deps/'PIL',dirs_exist_ok=True,ignore=shutil.ignore_patterns('__pycache__','*.pyc'))
pil_meta=Path(importlib.metadata.distribution('pillow')._path)
shutil.copytree(pil_meta,deps/pil_meta.name,dirs_exist_ok=True,ignore=shutil.ignore_patterns('__pycache__','*.pyc'))
if (pil_source.parent/'pillow.libs').exists():shutil.copytree(pil_source.parent/'pillow.libs',deps/'pillow.libs',dirs_exist_ok=True)
release_root=(ROOT/'release').resolve()
if bundle.resolve().parent!=release_root or bundle.is_symlink():raise RuntimeError('Unsafe bundle directory')
if bundle.exists():shutil.rmtree(bundle)
shutil.copytree(SOURCE,plugin,dirs_exist_ok=True,ignore=shutil.ignore_patterns('__pycache__','*.pyc','.mcp.json','mcp.json'))
shutil.copyfile(ROOT/'LICENSE',bundle/'LICENSE')
shutil.copyfile(ROOT/'THIRD_PARTY_NOTICES.md',bundle/'THIRD_PARTY_NOTICES.md')
native_sources=plugin/'runtime/native/source'
native_sources.mkdir(parents=True,exist_ok=True)
for name in ('direct_bridge.c','bridge_target.h','media_validation.h'):
    shutil.copyfile(ROOT/'native'/name,native_sources/name)
shutil.copyfile(ROOT/'native/targets'/(profile['clientVersion']+'.json'),native_sources/('target-'+profile['clientVersion']+'.json'))
market={'name':'local-wechat-direct','interface':{'displayName':'微信原号直连'},'plugins':[{
    'name':'wechat-desktop','source':{'source':'local','path':'./plugins/wechat-desktop'},
    'policy':{'installation':'AVAILABLE','authentication':'ON_INSTALL'},'category':'Productivity'}]}
catalog=bundle/'.agents/plugins';catalog.mkdir(parents=True,exist_ok=True)
(catalog/'marketplace.json').write_text(json.dumps(market,ensure_ascii=False,indent=2),encoding='utf8')
for name in ('install.mjs','install.cmd','mcp-config.mjs','使用说明.md'):
    shutil.copyfile(ROOT/'packaging'/name,bundle/name)
shutil.copyfile(ROOT/'docs/clients.md',bundle/'其他 AI 客户端.md')
archive=ROOT/f'release/wechat-direct-{VERSION}-windows-x64.zip'
entries=[]
with zipfile.ZipFile(archive,'w',zipfile.ZIP_DEFLATED) as output:
    for path in sorted(bundle.rglob('*')):
        if not path.is_file() or '__pycache__' in path.parts or path.suffix=='.pyc':continue
        relative=path.relative_to(bundle)
        if any(part in ('state','keys','profiles') for part in relative.parts):raise RuntimeError('Private data in package')
        output.write(path,Path(bundle.name)/relative);entries.append(str(relative))
with zipfile.ZipFile(archive) as check:
    if check.testzip():raise RuntimeError('ZIP integrity check failed')
summary={'plugin':'wechat-desktop','version':VERSION,'marketplace':'local-wechat-direct','archive':str(archive),
    'archiveSha256':hashlib.sha256(archive.read_bytes()).hexdigest(),'archiveBytes':archive.stat().st_size,
    'fileCount':len(entries),'privateDataIncluded':False}
(ROOT/'reports/plugin-package.json').write_text(json.dumps(summary,indent=2),encoding='utf8')
print(json.dumps(summary))
