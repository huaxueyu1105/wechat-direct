import {setTimeout as delay} from 'node:timers/promises';
import {fileURLToPath} from 'node:url';
import {randomUUID} from 'node:crypto';
import {existsSync, mkdirSync, readFileSync, renameSync, rmSync, statSync, writeFileSync} from 'node:fs';
import {join, resolve} from 'node:path';
import {invoke} from './wechat.mjs';

const STALE_MS = 180000;
const HEARTBEAT_MS = 5000;
const print = value => process.stdout.write(JSON.stringify(value) + '\n');

export function parseWorkerArgs(args) {
  const options = {once: false, interval: 15};
  for (let i = 0; i < args.length; i++) {
    if (args[i] === '--once') options.once = true;
    else if (args[i] === '--status') options.status = true;
    else if (args[i] === '--stop') options.stop = true;
    else if (args[i] === '--interval') {
      const value = Number(args[++i]);
      if (!Number.isInteger(value) || value < 5 || value > 3600) throw new Error('检查间隔需为 5–3600 秒。');
      options.interval = value;
    } else throw new Error('用法：scheduler.mjs [--once] [--interval 秒] | --status | --stop');
  }
  if ((options.status && options.stop) || ((options.status || options.stop) && (options.once || args.includes('--interval')))) {
    throw new Error('--status / --stop 需单独使用。');
  }
  return options;
}

function files(dataDir) {
  if (!dataDir && !process.env.WECHAT_DIRECT_DATA && !process.env.LOCALAPPDATA) throw new Error('未找到 LOCALAPPDATA；请设置 WECHAT_DIRECT_DATA。');
  const root = resolve(dataDir || process.env.WECHAT_DIRECT_DATA || join(process.env.LOCALAPPDATA, 'CodexWeChatDirect'));
  const lock = join(root, 'scheduler-worker.lock');
  return {root, lock, owner: join(lock, 'owner.json'), state: join(root, 'scheduler-worker.json')};
}

function readJson(path) {
  try { return JSON.parse(readFileSync(path, 'utf8')); }
  catch (error) { if (error.code === 'ENOENT' || error instanceof SyntaxError) return null; throw error; }
}

function writeJson(path, value) {
  const temporary = path + '.' + randomUUID() + '.tmp';
  try {
    writeFileSync(temporary, JSON.stringify(value) + '\n', {encoding: 'utf8', mode: 0o600});
    renameSync(temporary, path);
  } finally { if (existsSync(temporary)) rmSync(temporary); }
}

function alive(pid) {
  try { process.kill(pid, 0); return true; }
  catch (error) { return error.code !== 'ESRCH'; }
}

function ownerValid(owner) {
  return owner && typeof owner.token === 'string' && /^[0-9a-f-]{36}$/.test(owner.token) &&
    Number.isInteger(owner.pid) && owner.pid > 0 && Number.isFinite(Date.parse(owner.heartbeatAt));
}

function errorInfo(value, at = new Date().toISOString()) {
  const code = typeof value === 'string' && /^[A-Za-z][A-Za-z0-9_]{0,79}$/.test(value) ? value : 'SCHEDULER_ERROR';
  const messages = {
    ACCOUNT_NOT_READY: '微信未登录或绑定账号暂不可用。',
    ACCOUNT_MISMATCH: '当前微信账号与任务绑定不一致。',
    CLIENT_UNSUPPORTED: '当前微信版本尚未适配。',
    SEND_NOT_SUBMITTED: '本次定时消息未提交。',
    SEND_OUTCOME_UNKNOWN: '发送结果尚未确认；只查询原操作，不重新发送。',
    SCHEDULER_CALL_FAILED: '本机后端调用失败；已开始的操作仍按原编号查询。',
  };
  return {at, code, message: messages[code] || '执行器检查失败，错误代码：' + code + '。'};
}

function metadata(state) {
  const result = {};
  for (const key of ['pid', 'startedAt', 'heartbeatAt', 'lastCheckAt', 'lastCheckFinishedAt', 'lastCheckStatus', 'lastSuccessAt', 'stoppedAt']) {
    result[key] = state?.[key] ?? null;
  }
  result.lastError = state?.lastError ? errorInfo(state.lastError.code, state.lastError.at) : null;
  return result;
}

export function workerStatus({dataDir, now = Date.now()} = {}) {
  const paths = files(dataDir);
  if (!existsSync(paths.lock)) return {status: 'stopped', ...metadata(readJson(paths.state)), stopRequested: false};
  const owner = readJson(paths.owner);
  if (!ownerValid(owner)) {
    // mkdir and writing metadata are separate operations. Never take this lock.
    let modified;
    try { modified = statSync(paths.lock).mtimeMs; }
    catch (error) { if (error.code === 'ENOENT') return {status: 'stopped', ...metadata(readJson(paths.state)), stopRequested: false}; throw error; }
    const stale = now - modified > STALE_MS;
    return {status: stale ? 'stale' : 'starting', ...metadata(null), stopRequested: false, reason: 'metadata_unavailable'};
  }
  const isAlive = alive(owner.pid);
  const stale = !isAlive || now - Date.parse(owner.heartbeatAt) > STALE_MS;
  return {status: stale ? 'stale' : 'running', ...metadata(owner),
    stopRequested: existsSync(join(paths.lock, 'stop-' + owner.token + '.json')),
    ...(stale ? {reason: isAlive ? 'heartbeat_expired' : 'owner_exited'} : {})};
}

export function requestWorkerStop({dataDir} = {}) {
  const paths = files(dataDir);
  const worker = workerStatus({dataDir});
  const owner = readJson(paths.owner);
  if (!ownerValid(owner) || !alive(owner.pid)) return {requested: false, status: worker.status, worker};
  // Token-scoped requests cannot stop a replacement instance, even if its PID
  // was reused. No process signal is sent by this control operation.
  try { writeJson(join(paths.lock, 'stop-' + owner.token + '.json'), {token: owner.token, requestedAt: new Date().toISOString()}); }
  catch (error) { if (error.code === 'ENOENT') return {requested: false, status: 'stopped', worker: workerStatus({dataDir})}; throw error; }
  return {requested: true, status: 'stop_requested', worker: {...worker, stopRequested: true}};
}

function claim(paths) {
  mkdirSync(paths.root, {recursive: true});
  try { mkdirSync(paths.lock); }
  catch (error) {
    if (error.code !== 'EEXIST') throw error;
    const previous = readJson(paths.owner);
    if (!ownerValid(previous)) throw new Error('执行器锁尚未完成初始化或已损坏；未抢占该锁，请先查询 --status。');
    if (alive(previous.pid)) throw new Error('已有执行器进程；请用 --status 查询或 --stop 请求停止，未抢占活跃 PID。');
    const reclaim = join(paths.lock, 'reclaim');
    // Serialize stale-lock recovery inside that exact lock directory. A second
    // starter must recheck its token before it can rename anything.
    try { mkdirSync(reclaim); }
    catch (error) { if (['EEXIST', 'ENOENT'].includes(error.code)) throw new Error('其他实例正在回收执行器状态，请稍后重试。'); throw error; }
    let renamed = false;
    try {
      const current = readJson(paths.owner);
      if (current?.token !== previous.token || alive(current.pid)) throw new Error('执行器状态已变化，未抢占锁。');
      const abandoned = paths.lock + '.stale-' + randomUUID();
      renameSync(paths.lock, abandoned);
      renamed = true;
      rmSync(abandoned, {recursive: true, force: true});
      mkdirSync(paths.lock);
    } finally { if (!renamed) rmSync(reclaim, {recursive: true, force: true}); }
  }
  const now = new Date().toISOString();
  const previous = metadata(readJson(paths.state));
  const state = {token: randomUUID(), pid: process.pid, startedAt: now, heartbeatAt: now,
    lastCheckAt: previous.lastCheckAt, lastCheckFinishedAt: previous.lastCheckFinishedAt,
    lastCheckStatus: previous.lastCheckStatus, lastSuccessAt: previous.lastSuccessAt,
    lastError: previous.lastError, stoppedAt: null};
  try { writeJson(paths.owner, state); writeJson(paths.state, state); }
  catch (error) { rmSync(paths.lock, {recursive: true, force: true}); throw error; }
  return state;
}

// A worker must be explicitly started. Creating a task does not start a daemon.
export async function runWorker({once = false, interval = 15, signal, call = invoke,
  emit = print} = {}) {
  do {
    if (signal?.aborted) break;
    try {
      const response = await call({action: 'schedule-run'});
      if (!response.ok) {
        const error = errorInfo(response.error?.code);
        emit({ok: false, error: error.code, message: error.message});
      }
      else if (once || response.result.runs.length) emit({ok: true, runs: response.result.runs.map(run => ({
        taskId: run.taskId, operationKey: run.operationKey, status: run.status, action: run.action,
      }))});
    } catch (error) {
      // Do not resubmit here: the next tick queries the durable original operation.
      const failure = errorInfo('SCHEDULER_CALL_FAILED');
      emit({ok: false, error: failure.code, message: failure.message});
    }
    if (once || signal?.aborted) break;
    try { await delay(interval * 1000, undefined, {signal}); }
    catch (error) { if (error.name !== 'AbortError') throw error; }
  } while (!signal?.aborted);
}

export async function runManagedWorker({dataDir, once = false, interval = 15, signal, call = invoke, emit = print} = {}) {
  const paths = files(dataDir);
  const state = claim(paths);
  const controller = new AbortController();
  const owns = () => readJson(paths.owner)?.token === state.token;
  const stopFile = join(paths.lock, 'stop-' + state.token + '.json');
  let storageError;
  const publish = () => {
    if (!owns()) { controller.abort(); throw new Error('执行器锁所有权已变化，已停止后续检查。'); }
    try { writeJson(paths.owner, state); writeJson(paths.state, state); }
    catch (error) { storageError = error; controller.abort(); throw error; }
  };
  const abort = () => controller.abort();
  signal?.addEventListener('abort', abort, {once: true});
  if (signal?.aborted) controller.abort();
  const control = setInterval(() => {
    try {
      if (!owns() || existsSync(stopFile)) controller.abort();
      if (owns() && Date.now() - Date.parse(state.heartbeatAt) >= HEARTBEAT_MS) {
        state.heartbeatAt = new Date().toISOString();
        publish();
      }
    } catch (error) { storageError = error; controller.abort(); }
  }, 1000);
  try {
    await runWorker({once, interval, signal: controller.signal, emit, call: async request => {
      if (!owns() || existsSync(stopFile) || controller.signal.aborted) {
        controller.abort();
        return {ok: true, result: {runs: []}};
      }
      state.lastCheckAt = state.heartbeatAt = new Date().toISOString();
      state.lastCheckStatus = 'checking';
      publish();
      try {
        const response = await call(request);
        const failed = response.ok && response.result?.runs?.find(run => ['unknown', 'submitting', 'submitted', 'not_submitted'].includes(run.status));
        const code = !response.ok ? response.error?.code || 'SCHEDULER_ERROR' : failed ?
          (failed.result?.error?.code || failed.result?.error || (failed.status === 'not_submitted' ? 'SEND_NOT_SUBMITTED' : 'SEND_OUTCOME_UNKNOWN')) : null;
        state.lastCheckFinishedAt = state.heartbeatAt = new Date().toISOString();
        state.lastCheckStatus = code ? 'error' : 'ok';
        if (code) state.lastError = errorInfo(code, state.lastCheckFinishedAt);
        else state.lastSuccessAt = state.lastCheckFinishedAt;
        publish();
        return response;
      } catch (error) {
        state.lastCheckFinishedAt = state.heartbeatAt = new Date().toISOString();
        state.lastCheckStatus = 'error';
        state.lastError = errorInfo('SCHEDULER_CALL_FAILED', state.lastCheckFinishedAt);
        publish();
        throw error;
      }
    }});
    if (storageError) throw storageError;
  } finally {
    clearInterval(control);
    signal?.removeEventListener('abort', abort);
    if (owns()) {
      state.stoppedAt = new Date().toISOString();
      try { writeJson(paths.state, state); }
      finally { rmSync(paths.lock, {recursive: true, force: true}); }
    }
  }
}

if (process.argv[1] && fileURLToPath(import.meta.url) === process.argv[1]) {
  try {
    const options = parseWorkerArgs(process.argv.slice(2));
    if (options.status) print(workerStatus());
    else if (options.stop) print(requestWorkerStop());
    else {
      const controller = new AbortController();
      process.once('SIGINT', () => controller.abort());
      process.once('SIGTERM', () => controller.abort());
      await runManagedWorker({...options, signal: controller.signal});
    }
  } catch (error) {
    process.stderr.write(error.message + '\n');
    process.exitCode = 2;
  }
}
