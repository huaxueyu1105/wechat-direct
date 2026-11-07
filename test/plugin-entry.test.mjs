import test from 'node:test';
import assert from 'node:assert/strict';
import {mkdtemp,writeFile,rm} from 'node:fs/promises';
import {join} from 'node:path';
import {tmpdir} from 'node:os';
import {parseArgs} from '../plugin/scripts/wechat.mjs';

test('request file text is data and cannot override the selected action',async()=>{
  const dir=await mkdtemp(join(tmpdir(),'wechat-plugin-entry-'));
  try {
    const file=join(dir,'request.json');const text='妈，测试 `$(do-not-run)` ${literal}';
    await writeFile(file,JSON.stringify({action:'bind',recipientId:'peer',text,operationKey:'operation-001'}));
    const result=parseArgs(['send','--request-file',file]);
    assert.equal(result.action,'send');assert.equal(result.text,text);
  }finally{await rm(dir,{recursive:true,force:true});}
});
test('unsupported CLI actions and arguments fail before execution',()=>{
  assert.throws(()=>parseArgs(['run-shell','--command','unsafe']));
  assert.throws(()=>parseArgs(['send','--shell','unsafe']));
  assert.throws(()=>parseArgs(['messages','--recipient']));
});
