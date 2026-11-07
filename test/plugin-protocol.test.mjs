import test from 'node:test';
import assert from 'node:assert/strict';
import {spawnSync} from 'node:child_process';
import {readFileSync} from 'node:fs';
import {fileURLToPath} from 'node:url';

test('MCP exposes the versioned reply tools with correct read and write annotations',()=>{
  const server=fileURLToPath(new URL('../plugin/scripts/mcp-server.mjs',import.meta.url));
  const version=JSON.parse(readFileSync(new URL('../plugin/plugin.json',import.meta.url),'utf8')).version;
  const input=[{jsonrpc:'2.0',id:1,method:'initialize'}, {jsonrpc:'2.0',id:2,method:'tools/list'}].map(JSON.stringify).join('\n')+'\n';
  const child=spawnSync(process.execPath,[server],{input,encoding:'utf8',windowsHide:true,timeout:10000});
  assert.equal(child.status,0,child.stderr);
  const [initialized,listed]=child.stdout.trim().split(/\r?\n/).map(JSON.parse);
  assert.equal(initialized.result.serverInfo.version,version);
  assert.equal(listed.result.tools.length,13);
  const tools=Object.fromEntries(listed.result.tools.map(x=>[x.name,x]));
  assert.equal(tools.wechat_stickers.annotations.openWorldHint,true);
  assert.equal(tools.wechat_sticker_source.annotations.readOnlyHint,true);
  assert.equal(tools.wechat_sticker_import.annotations.readOnlyHint,false);
  assert.equal(tools.wechat_stickers.inputSchema.properties.refreshFavorites.type,'boolean');
  assert.equal(tools.wechat_sticker_import.inputSchema.properties.filePath,undefined);
  assert.ok(initialized.result.instructions.includes('添加的单个表情'));
  assert.equal(tools.wechat_reply_policy.annotations.readOnlyHint,true);
  for (const name of ['wechat_send_text','wechat_send_reply','wechat_send_sticker']) {
    assert.equal(tools[name].annotations.readOnlyHint,false);
    assert.equal(tools[name].annotations.openWorldHint,true);
    assert.ok(tools[name].inputSchema.required.includes('operationKey'));
  }
  assert.ok(tools.wechat_send_reply.inputSchema.required.includes('scene'));
  assert.ok(tools.wechat_send_reply.inputSchema.properties.scene.enum.includes('serious'));
});
