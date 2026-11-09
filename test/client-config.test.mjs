import test from 'node:test';
import assert from 'node:assert/strict';
import {spawnSync} from 'node:child_process';
import {mkdtemp, rmdir} from 'node:fs/promises';
import {isAbsolute, join} from 'node:path';
import {tmpdir} from 'node:os';
import {fileURLToPath} from 'node:url';

const generator = fileURLToPath(new URL('../packaging/mcp-config.mjs', import.meta.url));
const invoke = (command, args, options = {}) => {
  const child = spawnSync(command, args, {encoding:'utf8', windowsHide:true, timeout:20000, ...options});
  assert.ifError(child.error);
  assert.equal(child.status, 0, child.stderr);
  return child.stdout.trim();
};
const requests = (command, args, env, payloads) => {
  const input = payloads.map(payload => JSON.stringify({jsonrpc:'2.0', ...payload})).join('\n')+'\n';
  return invoke(command, args, {env:{...process.env,...env}, input}).split('\n').map(JSON.parse);
};
const init = protocolVersion => ({
  id:1, method:'initialize', params:{protocolVersion,capabilities:{},clientInfo:{name:'config-test',version:'1'}},
});
const initialized = {method:'notifications/initialized'};
const list = {id:2, method:'tools/list'};

for (const [client, ...options] of [['kimi'], ['gemini'], ['claude-desktop'], ['claude-code'],
  ['deepseek'], ['deepseek', '--opencode-v2']]) {
  test(`${[client,...options].join(' ')} config launches the read-only server`, () => {
    const config = JSON.parse(invoke(process.execPath, [generator, client, ...options]));
    const opencode = config.mcp?.servers?.wechat_direct ?? config.mcp?.wechat_direct;
    const [command, ...args] = opencode?.command
      ?? [config.mcpServers.wechat_direct.command, ...config.mcpServers.wechat_direct.args];
    const env = opencode?.environment ?? config.mcpServers.wechat_direct.env;
    assert.ok(isAbsolute(command) && isAbsolute(args[0]) && isAbsolute(env.WECHAT_DIRECT_PYTHON));
    if (client === 'deepseek') assert.equal(opencode.type, 'local');
    if (client === 'gemini') assert.equal(config.mcpServers.wechat_direct.trust, false);
    if (client === 'claude-code') assert.equal(config.mcpServers.wechat_direct.type, 'stdio');
    const [hello, listed, blocked] = requests(command, args, env, [init('2025-11-25'), initialized, list,
      {id:3, method:'tools/call', params:{name:'wechat_send_text', arguments:{}}}]);
    assert.equal(hello.result.protocolVersion, '2025-11-25');
    assert.equal(listed.result.tools.length, 11);
    assert.ok(listed.result.tools.every(tool => tool.annotations.readOnlyHint));
    assert.ok(listed.result.tools.some(tool => tool.name === 'wechat_messages'));
    assert.equal(blocked.error.code, -32602);
  });
}

test('external full-access config discovers all tools over the legacy protocol', () => {
  const config = JSON.parse(invoke(process.execPath, [generator, 'claude-code', '--full-access']));
  const {command, args, env} = config.mcpServers.wechat_direct;
  const [hello, listed] = requests(command, args, env, [init('2024-11-05'), initialized, list]);
  assert.equal(hello.result.protocolVersion, '2024-11-05');
  assert.equal(listed.result.tools.length, 25);
  assert.ok(listed.result.tools.some(tool => tool.name === 'wechat_schedule_create'));
});

test('generated config calls a Python tool with isolated state and no WeChat account', async () => {
  const state = await mkdtemp(join(tmpdir(), 'wechat-mcp-'));
  try {
    const config = JSON.parse(invoke(process.execPath, [generator, 'gemini'], {
      env:{...process.env,WECHAT_DIRECT_DATA:state},
    }));
    const {command, args, env} = config.mcpServers.wechat_direct;
    const [, result] = requests(command, args, env, [init('2025-06-18'), initialized,
      {id:3, method:'tools/call', params:{name:'wechat_reply_policy'}}]);
    assert.equal(result.result.isError, false);
    assert.equal(result.result.structuredContent.ok, true);
  } finally {
    await rmdir(state);
  }
});
