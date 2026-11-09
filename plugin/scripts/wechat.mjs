import {spawn, spawnSync} from 'node:child_process';
import {existsSync, readFileSync} from 'node:fs';
import {homedir} from 'node:os';
import {join} from 'node:path';
import {fileURLToPath} from 'node:url';

const backend = fileURLToPath(new URL('./wechat.py', import.meta.url));
let python;
export function pythonPath() {
  if (python) return python;
  const candidates = [process.env.WECHAT_DIRECT_PYTHON,
    join(homedir(), '.cache/codex-runtimes/codex-primary-runtime/dependencies/python/python.exe')].filter(Boolean);
  for (const candidate of candidates) {
    if (!existsSync(candidate)) continue;
    const result = spawnSync(candidate, ['-c', 'import sys,struct;print(f"{sys.version_info.major}.{sys.version_info.minor}:{struct.calcsize(\'P\')*8}")'],
      {encoding:'utf8',windowsHide:true,timeout:5000});
    if (result.status === 0 && result.stdout.trim() === '3.12:64') return python = candidate;
  }
  throw new Error('未找到 Python 3.12 x64。请配置 WECHAT_DIRECT_PYTHON，或使用本机 Codex 附带的 Python。');
}
export async function invoke(request) {
  if (request.action === 'worker-status' || request.action === 'worker-stop') {
    try {
      const worker = await import('./scheduler.mjs');
      return {ok:true,result:request.action==='worker-status'?worker.workerStatus():worker.requestWorkerStop()};
    } catch (error) {return {ok:false,error:{code:error.code||'WORKER_CONTROL_ERROR',message:error.message}};}
  }
  const encoded = JSON.stringify(request);
  if (Buffer.byteLength(encoded) > 128 * 1024) throw new Error('请求过大。');
  return new Promise((resolve, reject) => {
    const child = spawn(pythonPath(), [backend], {windowsHide:true,stdio:['pipe','pipe','pipe'],
      env:{...process.env,PYTHONUTF8:'1',PYTHONIOENCODING:'utf-8'}});
    child.stdout.setEncoding('utf8'); child.stderr.setEncoding('utf8');
    let stdout = '', stderr = '', settled = false;
    const finish = (error, result) => {
      if (settled) return; settled = true; clearTimeout(timer);
      error ? reject(error) : resolve(result);
    };
    const timer = setTimeout(() => {
      child.kill(); finish(new Error('插件调用超时；发送操作请先查询原操作结果，不要重发。'));
    }, 120000);
    child.stdout.on('data', chunk => {
      stdout += chunk.toString('utf8');
      if (stdout.length > 8 * 1024 * 1024) {child.kill(); finish(new Error('返回内容过大，请减少读取条数。'));}
    });
    child.stderr.on('data', chunk => { if (stderr.length < 16000) stderr += chunk.toString('utf8'); });
    child.on('error', error => finish(error));
    child.on('close', () => {
      try { finish(null, JSON.parse(stdout.trim())); }
      catch { finish(new Error('本机后端未返回有效结果。'+(process.env.WECHAT_DIRECT_DEBUG ? stderr : ''))); }
    });
    child.stdin.on('error', () => {}); child.stdin.end(encoded);
  });
}

export function parseArgs(args) {
  const [action, ...rest] = args;
  const allowed = new Set(['bind','status','probe','sticker-probe','contacts','sessions','messages','poll','send','send-reply','send-image','send-file','reply-policy','stickers','sticker-source','sticker-import','send-sticker','send-status','send-close','schedule-create','schedule-list','schedule-update','schedule-run','worker-status','worker-stop','cache-status','cache-clean','cache-configure']);
  if (!allowed.has(action)) throw new Error('不支持的操作；定时任务使用 schedule-create|schedule-list|schedule-update|schedule-run。');
  let request = {action};
  const fields = {'--query':'query','--recipient':'recipientId','--limit':'limit','--cursor':'cursor',
    '--timeout':'timeout','--wait':'wait','--operation-key':'operationKey','--scene':'scene',
    '--sticker-id':'stickerId','--file':'filePath','--label':'label','--mode':'mode',
    '--category':'category','--offset':'offset','--refresh-favorites':'refreshFavorites','--favorite-offset':'favoriteOffset','--include-preview':'includePreview',
    '--task-id':'taskId','--operation':'operation','--timezone':'timezone','--file-sha256':'fileSha256','--reviewed':'reviewed','--account':'account',
    '--max-mib':'maxMiB','--max-age-days':'maxAgeDays','--purge':'purge','--include-keys':'includeKeys'};
  for (let i=0;i<rest.length;i+=2) {
    const flag=rest[i], value=rest[i+1];
    if (value === undefined) throw new Error('缺少参数值：'+flag);
    if (flag === '--request-file') {
      const file = readFileSync(value,'utf8'); if (Buffer.byteLength(file)>128*1024) throw new Error('请求文件过大。');
      request = {...JSON.parse(file), ...request}; continue;
    }
    if (!fields[flag]) throw new Error('未知参数：'+flag);
    const field=fields[flag];
    if (['refreshFavorites','includePreview','reviewed','purge','includeKeys'].includes(field)) {
      if (!['true','false'].includes(value)) throw new Error('布尔参数需为 true 或 false。');
      request[field]=value==='true';
    } else request[field]=['limit','timeout','wait','offset','favoriteOffset','maxMiB','maxAgeDays'].includes(field)?Number(value):value;
  }
  return request;
}
if (process.argv[1] && fileURLToPath(import.meta.url) === process.argv[1]) {
  try {
    const result = await invoke(parseArgs(process.argv.slice(2)));
    process.stdout.write(JSON.stringify(result,null,2)+'\n'); process.exitCode=result.ok?0:2;
  } catch(error) {process.stdout.write(JSON.stringify({ok:false,error:{code:'LAUNCH_ERROR',message:error.message}})+'\n');process.exitCode=2;}
}
