import test from 'node:test';
import assert from 'node:assert/strict';
import {spawnSync} from 'node:child_process';
import {randomUUID} from 'node:crypto';
import {existsSync, mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync} from 'node:fs';
import {join, resolve, sep} from 'node:path';
import {fileURLToPath} from 'node:url';
import {invoke, parseArgs} from '../plugin/scripts/wechat.mjs';

const entry = fileURLToPath(new URL('../plugin/scripts/wechat.mjs', import.meta.url));

function temporary(t) {
  const root = resolve('work/test-temp');
  mkdirSync(root, {recursive: true});
  const directory = mkdtempSync(join(root, 'maintenance-cli-'));
  assert.ok(directory.startsWith(root + sep));
  t.after(() => rmSync(directory, {recursive: true, force: true}));
  return directory;
}

function cli(dataDir, args, exitCode = 0) {
  const child = spawnSync(process.execPath, [entry, ...args], {encoding: 'utf8',
    windowsHide: true, timeout: 30000,
    env: {...process.env, WECHAT_DIRECT_DATA: dataDir, WECHAT_DIRECT_DEBUG: '1'}});
  assert.ifError(child.error);
  assert.equal(child.status, exitCode, child.stdout + child.stderr);
  return JSON.parse(child.stdout);
}

test('invoke dynamically routes worker status and stop without starting a worker', async t => {
  const dataDir = temporary(t);
  const previous = process.env.WECHAT_DIRECT_DATA;
  process.env.WECHAT_DIRECT_DATA = dataDir;
  t.after(() => {
    if (previous === undefined) delete process.env.WECHAT_DIRECT_DATA;
    else process.env.WECHAT_DIRECT_DATA = previous;
  });

  const absent = await invoke(parseArgs(['worker-status']));
  assert.equal(absent.ok, true);
  assert.equal(absent.result.status, 'stopped');
  const idleStop = await invoke(parseArgs(['worker-stop']));
  assert.equal(idleStop.ok, true);
  assert.equal(idleStop.result.requested, false);
  assert.equal(existsSync(join(dataDir, 'scheduler-worker.lock')), false);

  const lock = join(dataDir, 'scheduler-worker.lock');
  mkdirSync(lock);
  const owner = {token: randomUUID(), pid: process.pid, heartbeatAt: new Date().toISOString()};
  writeFileSync(join(lock, 'owner.json'), JSON.stringify(owner));
  const active = await invoke(parseArgs(['worker-status']));
  assert.equal(active.ok, true);
  assert.equal(active.result.status, 'running');
  const stopped = await invoke(parseArgs(['worker-stop']));
  assert.equal(stopped.ok, true);
  assert.equal(stopped.result.requested, true);
  assert.equal(stopped.result.status, 'stop_requested');
  assert.equal(JSON.parse(readFileSync(join(lock, 'stop-' + owner.token + '.json'), 'utf8')).token, owner.token);
  assert.deepEqual(JSON.parse(readFileSync(join(lock, 'owner.json'), 'utf8')), owner);
  assert.equal((await invoke(parseArgs(['worker-status']))).result.stopRequested, true);
});

test('cache CLI crosses Node and Python using only its temporary data directory',
  {skip: process.platform !== 'win32'}, t => {
    const dataDir = temporary(t);
    const initial = cli(dataDir, ['cache-status']);
    assert.equal(initial.ok, true);
    assert.deepEqual(initial.result.policy, {maxMiB: 1024, maxAgeDays: 7});
    assert.equal(initial.result.bytes, 0);

    const database = join(dataDir, 'db', 'synthetic.db');
    const stamp = database + '.stamp';
    const keys = join(dataDir, 'db', 'keys.json');
    mkdirSync(join(dataDir, 'db'));
    writeFileSync(database, Buffer.alloc(2 * 1024 * 1024));
    writeFileSync(stamp, 'synthetic stamp');
    writeFileSync(keys, '{}');
    writeFileSync(join(dataDir, 'original.txt'), 'original stays intact');
    writeFileSync(join(dataDir, 'account.json'), JSON.stringify({accountId: 'synthetic-account'}));

    const configured = cli(dataDir, ['cache-configure', '--max-mib', '1', '--max-age-days', '2']);
    assert.equal(configured.ok, true);
    assert.deepEqual(configured.result.policy, {maxMiB: 1, maxAgeDays: 2});
    assert.equal(configured.result.overLimit, true);
    assert.equal(existsSync(database), true); // Configuration does not clean immediately.
    const status = cli(dataDir, ['cache-status']);
    assert.equal(status.ok, true);
    assert.deepEqual(status.result.policy, configured.result.policy);
    assert.equal(status.result.bytes, 2 * 1024 * 1024 + Buffer.byteLength('synthetic stamp'));

    const cleaned = cli(dataDir, ['cache-clean', '--purge', 'false', '--include-keys', 'false']);
    assert.equal(cleaned.ok, true);
    assert.equal(cleaned.result.removedFiles, 2);
    assert.equal(cleaned.result.remainingBytes, 0);
    assert.equal(cleaned.result.overLimit, false);
    assert.equal(existsSync(database), false);
    assert.equal(existsSync(stamp), false);
    assert.equal(existsSync(keys), true);
    const withKeys = cli(dataDir, ['cache-clean', '--purge', 'true', '--include-keys', 'true']);
    assert.equal(withKeys.ok, true);
    assert.equal(withKeys.result.removedFiles, 1);
    assert.equal(existsSync(keys), false);
    assert.equal(readFileSync(join(dataDir, 'original.txt'), 'utf8'), 'original stays intact');
    assert.deepEqual(JSON.parse(readFileSync(join(dataDir, 'account.json'), 'utf8')), {accountId: 'synthetic-account'});

    const invalid = cli(dataDir, ['cache-configure', '--max-mib', '0', '--max-age-days', '2'], 2);
    assert.equal(invalid.ok, false);
    assert.equal(invalid.error.code, 'CACHE_POLICY_INVALID');
    assert.deepEqual(JSON.parse(readFileSync(join(dataDir, 'cache-policy.json'), 'utf8')), configured.result.policy);
  });
