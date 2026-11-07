import {cpSync,existsSync,mkdirSync,readFileSync,readdirSync,statSync,writeFileSync} from 'node:fs';
import {spawnSync} from 'node:child_process';
import {homedir} from 'node:os';
import {dirname,join,resolve} from 'node:path';
import {fileURLToPath} from 'node:url';

const bundle=dirname(fileURLToPath(import.meta.url));
const source=join(bundle,'plugins/wechat-desktop');
if (!existsSync(join(source,'plugin.json'))) throw new Error('请从完整安装包目录运行。');
const codexHome=process.env.CODEX_HOME||join(homedir(),'.codex');
const destination=join(codexHome,'local-marketplaces/wechat-direct');
const plugin=join(destination,'plugins/wechat-desktop');
const version=JSON.parse(readFileSync(join(source,'plugin.json'),'utf8')).version;
if (resolve(bundle)!==resolve(destination)) {
  mkdirSync(destination,{recursive:true});
  cpSync(bundle,destination,{recursive:true,filter:p=>!p.endsWith('.zip')});
}
const candidates=[process.env.CODEX_CLI_PATH,
  ...(existsSync(join(homedir(),'AppData/Local/OpenAI/Codex/bin'))?
    readdirSync(join(homedir(),'AppData/Local/OpenAI/Codex/bin')).map(x=>join(homedir(),'AppData/Local/OpenAI/Codex/bin',x,'codex.exe')):[])].filter(p=>p&&existsSync(p));
candidates.sort((a,b)=>statSync(b).mtimeMs-statSync(a).mtimeMs);
const codex=candidates[0];
if (!codex) throw new Error('未找到本机 Codex 命令，请先安装 Codex 桌面应用。');
const mcp={mcpServers:{wechat_direct:{command:'./scripts/launch.cmd',args:[],
  env:{PYTHONUTF8:'1',WECHAT_DIRECT_NODE:process.execPath,USERPROFILE:homedir(),LOCALAPPDATA:process.env.LOCALAPPDATA||join(homedir(),'AppData/Local'),
    ...(process.env.WECHAT_DIRECT_PYTHON?{WECHAT_DIRECT_PYTHON:process.env.WECHAT_DIRECT_PYTHON}:{})}}}};
writeFileSync(join(plugin,'mcp.json'),JSON.stringify({$schema:'https://agent-plugins.org/schemas/1.0.0/mcp.schema.json',mcpServers:{wechat_direct:{type:'stdio',...mcp.mcpServers.wechat_direct}}},null,2));
writeFileSync(join(plugin,'.mcp.json'),JSON.stringify(mcp,null,2));
const config=join(codexHome,'config.toml');
const backup=join(process.env.LOCALAPPDATA,'CodexWeChatDirect/install-backup');mkdirSync(backup,{recursive:true});
if (existsSync(config)) writeFileSync(join(backup,'config-'+Date.now()+'.toml'),readFileSync(config));
const run=(exe,args)=>{
  const result=spawnSync(exe,args,{encoding:'utf8',windowsHide:true,maxBuffer:8*1024*1024,timeout:120000});
  if (result.error||result.status!==0) throw new Error((result.stderr||result.stdout||result.error?.message||'安装失败').trim());
  return result.stdout.trim();
};
const accountPath=join(process.env.LOCALAPPDATA||join(homedir(),'AppData/Local'),'CodexWeChatDirect/account.json');
const existing=existsSync(accountPath)?JSON.parse(readFileSync(accountPath,'utf8')):null;
// 更新可离线进行，保留现有绑定；首次安装才从当前登录的微信初始化。
const initialized=existing?.accountId?{ok:true,result:{bindingPreserved:true,transport:existing.transport}}:
  JSON.parse(run(process.execPath,[join(plugin,'scripts/wechat.mjs'),'bind']));
if (!initialized.ok) throw new Error(initialized.error.message);
const registered=run(codex,['plugin','marketplace','add',destination,'--json']);
const before=JSON.parse(run(codex,['plugin','list','--marketplace','local-wechat-direct','--json']));
const current=before.installed?.find(x=>x.pluginId==='wechat-desktop@local-wechat-direct'&&x.version===version&&x.enabled);
const cache=join(codexHome,'plugins/cache/local-wechat-direct/wechat-desktop',version);
// 同版且内容完全一致时不再重命名已存在的 Windows 缓存目录。
const matches=(from,to)=>existsSync(to)&&readdirSync(from,{withFileTypes:true}).every(entry=>{
  if(entry.name==='__pycache__'||entry.name.endsWith('.pyc'))return true;
  const left=join(from,entry.name),right=join(to,entry.name);
  return entry.isDirectory()?matches(left,right):existsSync(right)&&statSync(right).isFile()&&readFileSync(left).equals(readFileSync(right));
});
const installed=current&&matches(plugin,cache)?JSON.stringify({...current,unchanged:true}):
  run(codex,['plugin','add','wechat-desktop@local-wechat-direct','--json']);
const listed=run(codex,['plugin','list','--marketplace','local-wechat-direct','--json']);
const report={version,sourceRoot:plugin,initialized:initialized.result,registered:JSON.parse(registered),
  installed:JSON.parse(installed),listed:JSON.parse(listed),testMessageSentDuringInstall:false};
writeFileSync(join(backup,'installation-result.json'),JSON.stringify(report,null,2));
console.log(JSON.stringify(report,null,2));
