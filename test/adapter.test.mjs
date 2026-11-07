import test from 'node:test';
import assert from 'node:assert/strict';
import {createServer} from 'node:http';
import {createHash} from 'node:crypto';
import {mkdtemp, rm} from 'node:fs/promises';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import {AixedHookBackend, DirectWeChatAdapter, FileReservations} from '../src/adapter.mjs';

const sha = 'a'.repeat(64);
const fingerprint = createHash('sha256').update(JSON.stringify(['self', 'peer', 'hello'])).digest('hex');
const row = (serverId, overrides = {}) => ({accountId: 'self', recipientId: 'peer',
  direction: 'outgoing', text: 'hello', serverId, createdAtMs: 100,
  submissionFingerprint: fingerprint, ...overrides});
function fixture(options = {}) {
  let tick = 100, submitted = 0, reads = 0;
  const adapter = new DirectWeChatAdapter({
    binding: {nativeRuntimeValidated: true, transport: 'direct-only', accountMode: 'existing-personal-account',
      uiAutomationAllowed: false, coreSha256: sha, clientVersion: '4.1.15.50', accountId: 'self', ...options.binding},
    inspectCore: async () => ({sha256: options.sha || sha, version: '4.1.15.50'}),
    backend: {
      status: async () => ({IsLogin: 1}),
      profile: async () => ({wxid: options.account || 'self'}),
      submitText: async () => { submitted++; if (options.throwOnSubmit) throw new Error('network disconnected'); return {ret: 0}; },
    },
    receipts: {hasConversation: async () => options.conversation !== false,
      outgoing: async () => { reads++; return reads === 1 ? (options.baseline || []) : (options.after || []); }},
    reservations: {reserve: async () => {}},
    now: () => tick, pause: async ms => { tick += ms; }, receiptTimeoutMs: 200,
  });
  return {adapter, submitted: () => submitted};
}
const send = adapter => adapter.sendText({recipientId: 'peer', text: 'hello', operationKey: 'unique-operation'});

test('static report cannot authorize a native send', async () => {
  const f = fixture({binding: {nativeRuntimeValidated: false}});
  await assert.rejects(send(f.adapter), {code: 'NATIVE_PORT_UNVERIFIED'});
  assert.equal(f.submitted(), 0);
});
test('a core replacement and an account switch both prevent submission', async () => {
  for (const options of [{sha: 'b'.repeat(64)}, {account: 'different'}]) {
    const f = fixture(options);
    await assert.rejects(send(f.adapter), {code: options.sha ? 'CLIENT_VERSION_MISMATCH' : 'ACCOUNT_MISMATCH'});
    assert.equal(f.submitted(), 0);
  }
});
test('an unknown recipient prevents submission', async () => {
  const f = fixture({conversation: false});
  await assert.rejects(send(f.adapter), {code: 'CONVERSATION_NOT_FOUND'});
  assert.equal(f.submitted(), 0);
});
test('ret=0 without a new correlated record is an unknown outcome and locks retry', async () => {
  const f = fixture();
  await assert.rejects(send(f.adapter), {code: 'SEND_OUTCOME_UNKNOWN'});
  await assert.rejects(send(f.adapter), {code: 'SEND_LOCKED'});
  assert.equal(f.submitted(), 1);
});
test('an old server ID and an unrelated identical message cannot confirm this submission', async () => {
  for (const options of [
    {baseline: [row('123')], after: [row('123')]},
    {after: [row('124', {submissionFingerprint: 'unrelated'})]},
    {after: [row('124', {createdAtMs: 99})]},
  ]) {
    const f = fixture(options);
    await assert.rejects(send(f.adapter), {code: 'SEND_OUTCOME_UNKNOWN'});
    assert.equal(f.submitted(), 1);
  }
});
test('a new correlated decimal-string server ID confirms only a server record', async () => {
  const f = fixture({baseline: [row('9007199254740992')], after: [row('9007199254740993')]});
  const result = await send(f.adapter);
  assert.equal(result.serverId, '9007199254740993');
  assert.equal(result.status, 'server_record_observed');
  assert.equal(result.deliveredToRecipient, 'unknown');
  assert.equal(result.readByRecipient, 'unknown');
  assert.equal(f.submitted(), 1);
});
test('multiple correlated new records are ambiguous', async () => {
  const f = fixture({after: [row('123'), row('124')]});
  await assert.rejects(send(f.adapter), {code: 'AMBIGUOUS_RECEIPT'});
});
test('HTTP failure never causes a second submission', async () => {
  const f = fixture({throwOnSubmit: true});
  await assert.rejects(send(f.adapter), /network disconnected/);
  await assert.rejects(send(f.adapter), {code: 'SEND_LOCKED'});
  assert.equal(f.submitted(), 1);
});
test('durable reservation survives a new store instance', async () => {
  const directory = await mkdtemp(join(tmpdir(), 'wechat-adapter-test-'));
  try {
    await new FileReservations(directory).reserve('op', fingerprint, []);
    await assert.rejects(new FileReservations(directory).reserve('op', fingerprint, []),
      {code: 'OPERATION_ALREADY_RESERVED'});
  } finally { await rm(directory, {recursive: true, force: true}); }
});
test('the upstream wire adapter sends the documented paths and JSON fields', async () => {
  const seen = [];
  const server = createServer(async (req, res) => {
    let body = '';
    for await (const chunk of req) body += chunk;
    seen.push({path: req.url, method: req.method, body: body ? JSON.parse(body) : null});
    res.setHeader('Content-Type', 'application/json'); res.end(JSON.stringify({ret: 0}));
  });
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  try {
    const backend = new AixedHookBackend({baseUrl: `http://127.0.0.1:${server.address().port}`});
    await backend.status(); await backend.profile(); await backend.databases(); await backend.submitText('peer', 'hello');
    assert.deepEqual(seen, [
      {path: '/QueryDB/status', method: 'GET', body: null},
      {path: '/GetSelfProfile', method: 'POST', body: {}},
      {path: '/QueryDB/GetAllDBName', method: 'POST', body: {}},
      {path: '/SendTextMsg', method: 'POST', body: {wxidorgid: 'peer', msg: 'hello'}},
    ]);
  } finally { await new Promise(resolve => server.close(resolve)); }
});
test('non-loopback endpoints are rejected', () => {
  for (const baseUrl of ['http://example.com', 'http://0.0.0.0:30001', 'http://127.0.0.1@evil.test', 'http://127.0.0.1/path']) {
    assert.throws(() => new AixedHookBackend({baseUrl}), {code: 'INVALID_BACKEND_URL'});
  }
});
