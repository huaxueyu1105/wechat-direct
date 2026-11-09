import {spawnSync} from 'node:child_process';
import {existsSync} from 'node:fs';
import {homedir} from 'node:os';
import {join, resolve} from 'node:path';
import {fileURLToPath} from 'node:url';
import {parseArgs} from 'node:util';

const usage = '用法：node mcp-config.mjs <kimi|gemini|claude-desktop|claude-code|deepseek> [--full-access] [--check] [--opencode-v2]';
const clients = ['kimi', 'gemini', 'claude-desktop', 'claude-code', 'deepseek'];

async function main() {
  const {values, positionals} = parseArgs({allowPositionals:true, options:{
    'full-access':{type:'boolean'}, 'opencode-v2':{type:'boolean'}, check:{type:'boolean'}, help:{type:'boolean'},
  }});
  if (values.help) return console.log(usage);
  const [client] = positionals;
  if (positionals.length !== 1 || !clients.includes(client) || (values['opencode-v2'] && client !== 'deepseek')) {
    throw new Error(usage);
  }

  const bundleScripts = new URL('./plugins/wechat-desktop/scripts/', import.meta.url);
  const scripts = existsSync(new URL('mcp-server.mjs', bundleScripts))
    ? bundleScripts
    : new URL('../plugin/scripts/', import.meta.url);
  const {pythonPath} = await import(new URL('wechat.mjs', scripts));
  const command = process.execPath;
  const args = [fileURLToPath(new URL('mcp-server.mjs', scripts))];
  if (!values['full-access']) args.push('--read-only');
  const env = {
    WECHAT_DIRECT_PYTHON:pythonPath(),
    LOCALAPPDATA:process.env.LOCALAPPDATA || join(homedir(), 'AppData', 'Local'),
    ...(process.env.WECHAT_DIRECT_DATA ? {WECHAT_DIRECT_DATA:resolve(process.env.WECHAT_DIRECT_DATA)} : {}),
  };
  const stdio = {command, args, env};

  if (values.check) {
    const requests = [
      {id:1,method:'initialize',params:{protocolVersion:'2025-11-25',capabilities:{},clientInfo:{name:'wechat-direct-check',version:'1'}}},
      {method:'notifications/initialized'},
      {id:2,method:'tools/list'},
    ];
    const child = spawnSync(command, args, {
      env:{...process.env,...env}, input:requests.map(request => JSON.stringify({jsonrpc:'2.0',...request})).join('\n')+'\n',
      encoding:'utf8', timeout:10000, windowsHide:true,
    });
    if (child.error || child.status !== 0) throw new Error(child.error?.message || child.stderr || 'MCP 服务启动失败。');
    const [initialized, listed] = child.stdout.trim().split('\n').map(line => JSON.parse(line));
    const tools = listed?.result?.tools;
    if (initialized?.result?.protocolVersion !== '2025-11-25' || !tools?.some(tool => tool.name === 'wechat_status')
        || tools.some(tool => tool.name === 'wechat_send_text') !== Boolean(values['full-access'])) {
      throw new Error('MCP 握手或工具权限检查失败。');
    }
    return console.log(`MCP 连接正常，发现 ${tools.length} 个${values['full-access'] ? '' : '查询'}工具。`);
  }

  const name = 'wechat_direct';
  if (client === 'deepseek') {
    const local = {type:'local', command:[command,...args], environment:env};
    const mcp = values['opencode-v2']
      ? {servers:{[name]:{...local,codemode:false}}}
      : {[name]:{...local,enabled:true,timeout:125000}};
    return console.log(JSON.stringify({mcp},null,2));
  }
  const config = {mcpServers:{[name]:{
    ...stdio,
    ...(client === 'claude-code' ? {type:'stdio'} : {}),
    ...(client === 'gemini' ? {timeout:125000,trust:false} : {}),
    ...(client === 'kimi' ? {toolTimeoutMs:125000} : {}),
  }}};
  console.log(JSON.stringify(config,null,2));
}

main().catch(error => {
  console.error(error.message);
  process.exitCode = 1;
});
