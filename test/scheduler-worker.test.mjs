import test from 'node:test';
import assert from 'node:assert/strict';
import {mkdtempSync, mkdirSync, readFileSync, rmSync, writeFileSync} from 'node:fs';
import {join, resolve} from 'node:path';
import {randomUUID} from 'node:crypto';
import {spawn} from 'node:child_process';
import {parseWorkerArgs, runWorker, runManagedWorker, workerStatus, requestWorkerStop} from '../plugin/scripts/scheduler.mjs';

function temporary(t) {
  const root = resolve('work/test-temp');
  mkdirSync(root, {recursive: true});
  const dataDir = mkdtempSync(join(root, 'scheduler-worker-'));
  t.after(() => rmSync(dataDir, {recursive: true, force: true}));
  return dataDir;
}

function seedOwner(dataDir, changes = {}) {
  const lock = join(dataDir, 'scheduler-worker.lock');
  mkdirSync(lock, {recursive: true});
  const owner = {token: randomUUID(), pid: process.pid, startedAt: new Date().toISOString(),
    heartbeatAt: new Date().toISOString(), ...changes};
  writeFileSync(join(lock, 'owner.json'), JSON.stringify(owner));
  return owner;
}

const emptyResult = () => ({ok: true, result: {runs: []}});

test('worker parses bounded intervals and rejects unknown switches', () => {
  assert.deepEqual(parseWorkerArgs(['--once', '--interval', '30']), {once: true, interval: 30});
  for (const args of [['--interval', '0'], ['--interval'], ['--interval', 'Infinity'], ['--shell']]) {
    assert.throws(() => parseWorkerArgs(args));
  }
  assert.equal(parseWorkerArgs(['--status']).status, true);
  assert.equal(parseWorkerArgs(['--stop']).stop, true);
  for (const args of [['--status', '--stop'], ['--status', '--once'], ['--stop', '--interval', '15']]) {
    assert.throws(() => parseWorkerArgs(args));
  }
});

test('worker invokes only the due-task runner and omits private bodies from output', async () => {
  const requests = [], output = [];
  await runWorker({once: true, emit: value => output.push(value), call: async request => {
    requests.push(request);
    return {ok: true, result: {runs: [{taskId: 'task', operationKey: 'original-key', status: 'unknown',
      action: 'queried', result: {text: 'private text'}}]}};
  }});
  assert.deepEqual(requests, [{action: 'schedule-run'}]);
  assert.equal(output[0].runs[0].operationKey, 'original-key');
  assert.equal(JSON.stringify(output).includes('private text'), false);
});

test('worker handles an uncertain transport result without direct send or retry', async () => {
  let calls = 0;
  const output = [];
  await runWorker({once: true, emit: value => output.push(value), call: async () => {
    calls++; throw new Error('transport timeout');
  }});
  assert.equal(calls, 1);
  assert.equal(output[0].error, 'SCHEDULER_CALL_FAILED');
});

test('status and stop do not start a worker and absent state is stopped', t => {
  const dataDir = temporary(t);
  assert.equal(workerStatus({dataDir}).status, 'stopped');
  assert.equal(requestWorkerStop({dataDir}).requested, false);
});

test('managed once records check metadata and a sanitized recent error', async t => {
  const dataDir = temporary(t);
  await runManagedWorker({dataDir, once: true, emit() {}, call: async () => ({ok: false,
    error: {code: 'ACCOUNT_NOT_READY', message: 'private message body must not be saved'}})});
  const status = workerStatus({dataDir});
  assert.equal(status.status, 'stopped');
  assert.equal(status.lastCheckStatus, 'error');
  assert.equal(status.lastError.code, 'ACCOUNT_NOT_READY');
  assert.ok(status.lastError.message);
  assert.ok(status.lastCheckAt);
  assert.ok(status.lastCheckFinishedAt);
  const stored = readFileSync(join(dataDir, 'scheduler-worker.json'), 'utf8');
  assert.equal(stored.includes('private message body'), false);
  assert.equal(JSON.stringify(status).includes('token'), false);
  await runManagedWorker({dataDir, once: true, emit() {}, call: emptyResult});
  const recovered = workerStatus({dataDir});
  assert.equal(recovered.lastCheckStatus, 'ok');
  assert.equal(recovered.lastError.code, 'ACCOUNT_NOT_READY');
  assert.ok(recovered.lastSuccessAt);
});

test('managed worker keeps transport errors and send uncertainty as metadata only', async t => {
  const dataDir = temporary(t);
  await runManagedWorker({dataDir, once: true, emit() {}, call: async () => {
    throw new Error('private text from a backend exception');
  }});
  assert.equal(workerStatus({dataDir}).lastError.code, 'SCHEDULER_CALL_FAILED');
  await runManagedWorker({dataDir, once: true, emit() {}, call: async () => ({ok: true, result: {runs: [{
    status: 'unknown', operationKey: 'secret-operation', result: {text: 'private message'},
  }]}})});
  assert.equal(workerStatus({dataDir}).lastError.code, 'SEND_OUTCOME_UNKNOWN');
  const stored = readFileSync(join(dataDir, 'scheduler-worker.json'), 'utf8');
  assert.equal(stored.includes('private'), false);
  assert.equal(stored.includes('secret-operation'), false);
});

test('second instance is rejected and stop waits for the current call to finish', async t => {
  const dataDir = temporary(t);
  let started, release;
  const entered = new Promise(resolve => { started = resolve; });
  const held = new Promise(resolve => { release = resolve; });
  let calls = 0;
  const worker = runManagedWorker({dataDir, emit() {}, call: async () => {
    calls++;
    started();
    await held;
    return emptyResult();
  }});
  await entered;
  try {
    assert.equal(workerStatus({dataDir}).status, 'running');
    await assert.rejects(runManagedWorker({dataDir, once: true, call: emptyResult, emit() {}}), /已有执行器/);
    const stopped = requestWorkerStop({dataDir});
    assert.equal(stopped.requested, true);
    assert.equal(workerStatus({dataDir}).stopRequested, true);
    assert.equal(workerStatus({dataDir}).status, 'running');
  } finally { release(); }
  await worker;
  assert.equal(calls, 1);
  assert.equal(workerStatus({dataDir}).status, 'stopped');
});

test('a live PID with an expired heartbeat is not stolen or killed', async t => {
  const dataDir = temporary(t);
  const owner = seedOwner(dataDir, {heartbeatAt: new Date(Date.now() - 181000).toISOString()});
  assert.equal(workerStatus({dataDir}).status, 'stale');
  await assert.rejects(runManagedWorker({dataDir, once: true, call: emptyResult, emit() {}}), /活跃 PID/);
  assert.equal(requestWorkerStop({dataDir}).requested, true);
  const saved = JSON.parse(readFileSync(join(dataDir, 'scheduler-worker.lock', 'owner.json'), 'utf8'));
  assert.equal(saved.token, owner.token);
  assert.equal(saved.pid, process.pid);
});

test('a metadata-free startup lock is never reclaimed', async t => {
  const dataDir = temporary(t);
  mkdirSync(join(dataDir, 'scheduler-worker.lock'));
  assert.equal(workerStatus({dataDir}).status, 'starting');
  assert.equal(workerStatus({dataDir, now: Date.now() + 181000}).status, 'stale');
  await assert.rejects(runManagedWorker({dataDir, once: true, call: emptyResult, emit() {}}), /未完成初始化/);
  assert.equal(requestWorkerStop({dataDir}).requested, false);
});

test('dead owner can be recovered and an old token cannot stop its replacement', async t => {
  const dataDir = temporary(t);
  const child = spawn(process.execPath, ['-e', ''], {windowsHide: true, stdio: 'ignore'});
  await new Promise((resolve, reject) => { child.once('close', resolve); child.once('error', reject); });
  const old = seedOwner(dataDir, {pid: child.pid});
  assert.equal(workerStatus({dataDir}).reason, 'owner_exited');
  let calls = 0;
  await runManagedWorker({dataDir, once: true, emit() {}, call: async () => {
    calls++;
    const current = JSON.parse(readFileSync(join(dataDir, 'scheduler-worker.lock', 'owner.json'), 'utf8'));
    assert.notEqual(current.token, old.token);
    writeFileSync(join(dataDir, 'scheduler-worker.lock', 'stop-' + old.token + '.json'), '{}');
    assert.equal(workerStatus({dataDir}).stopRequested, false);
    return emptyResult();
  }});
  assert.equal(calls, 1);
  assert.equal(workerStatus({dataDir}).status, 'stopped');
});
