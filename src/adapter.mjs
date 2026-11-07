import {createHash} from 'node:crypto';
import {open, readFile, mkdir} from 'node:fs/promises';
import {join} from 'node:path';

export class AdapterError extends Error {
  constructor(code, message) { super(message); this.code = code; }
}
const fail = (code, message) => { throw new AdapterError(code, message); };
const required = (value, name) => {
  if (typeof value !== 'string' || !value.trim() || value.includes('\0')) fail('INVALID_ARGUMENT', name);
  return value;
};

/** The documented upstream wire format, not a claim of native-version support. */
export class AixedHookBackend {
  constructor({baseUrl = 'http://127.0.0.1:30001', timeoutMs = 3000} = {}) {
    const url = new URL(baseUrl);
    if (url.protocol !== 'http:' || url.hostname !== '127.0.0.1' || url.username ||
        url.password || url.pathname !== '/' || url.search || url.hash) {
      fail('INVALID_BACKEND_URL', 'Use an IPv4 loopback HTTP origin');
    }
    this.origin = url.origin;
    this.timeoutMs = timeoutMs;
  }
  async request(path, body) {
    const response = await fetch(this.origin + path, {
      method: body === undefined ? 'GET' : 'POST', redirect: 'error',
      headers: {'Content-Type': 'application/json'},
      body: body === undefined ? undefined : JSON.stringify(body),
      signal: AbortSignal.timeout(this.timeoutMs),
    });
    if (!response.ok) fail('BACKEND_HTTP_ERROR', `HTTP ${response.status}`);
    // Stream with a hard cap, rather than trusting a Content-Length header.
    let size = 0;
    const chunks = [];
    for await (const chunk of response.body) {
      size += chunk.length;
      if (size > 4 * 1024 * 1024) fail('BACKEND_RESPONSE_TOO_LARGE', 'Response exceeds 4 MiB');
      chunks.push(chunk);
    }
    return JSON.parse(Buffer.concat(chunks).toString('utf8'));
  }
  status() { return this.request('/QueryDB/status'); }
  profile() { return this.request('/GetSelfProfile', {}); }
  databases() { return this.request('/QueryDB/GetAllDBName', {}); }
  submitText(recipient, text) { return this.request('/SendTextMsg', {wxidorgid: recipient, msg: text}); }
}

/**
 * Durable reservation before submission. A crash/timeout leaves a reservation,
 * so repeating the operation never silently resends. It stores hashes, not text.
 */
export class FileReservations {
  constructor(directory) { this.directory = directory; }
  path(key) { return join(this.directory, createHash('sha256').update(key).digest('hex') + '.json'); }
  async reserve(key, fingerprint, snapshot) {
    await mkdir(this.directory, {recursive: true});
    let handle;
    try { handle = await open(this.path(key), 'wx'); }
    catch (error) {
      if (error.code === 'EEXIST') fail('OPERATION_ALREADY_RESERVED', 'Inspect the previous outcome before using a new operation key');
      throw error;
    }
    try {
      await handle.writeFile(JSON.stringify({fingerprint, reservedAt: new Date().toISOString(),
        baselineServerIds: snapshot.map(row => String(row.serverId))}));
      await handle.sync();
    } finally { await handle.close(); }
  }
}

const positiveServerId = id => typeof id === 'string' && /^[1-9][0-9]*$/.test(id);
function validateRows(rows, account, recipient) {
  if (!Array.isArray(rows)) fail('INVALID_MESSAGE_EVIDENCE', 'Message source did not return an array');
  for (const row of rows) {
    if (!row || row.accountId !== account || row.recipientId !== recipient ||
        row.direction !== 'outgoing' || typeof row.text !== 'string' ||
        !positiveServerId(row.serverId) || !Number.isSafeInteger(row.createdAtMs)) {
      fail('INVALID_MESSAGE_EVIDENCE', 'Message evidence has invalid identity, timestamp or server ID');
    }
  }
  return rows;
}

/**
 * bridgeBinding must come from actual native validation, never from this static
 * scanner. receipts must read new outgoing records from the same native account.
 * Neither dependency is available for 4.1.15.50 yet. No UI fallback exists.
 */
export class DirectWeChatAdapter {
  constructor({backend, binding, inspectCore, receipts, reservations, now = Date.now,
    pause = ms => new Promise(resolve => setTimeout(resolve, ms)), receiptTimeoutMs = 5000}) {
    Object.assign(this, {backend, binding, inspectCore, receipts, reservations, now, pause, receiptTimeoutMs});
    this.busy = false;
    this.uncertain = false;
  }
  async check() {
    const b = this.binding;
    if (!b || b.nativeRuntimeValidated !== true || b.transport !== 'direct-only' ||
        b.accountMode !== 'existing-personal-account' || b.uiAutomationAllowed !== false) {
      fail('NATIVE_PORT_UNVERIFIED', 'A runtime-validated native version binding is required');
    }
    if (!/^[a-f0-9]{64}$/.test(b.coreSha256 ?? '')) fail('INVALID_BINDING', 'Missing pinned core digest');
    required(b.accountId, 'Missing pinned account ID');
    const core = await this.inspectCore();
    if (core.sha256 !== b.coreSha256 || core.version !== b.clientVersion) {
      fail('CLIENT_VERSION_MISMATCH', 'Installed core differs from the validated native binding');
    }
    const status = await this.backend.status();
    if (status?.IsLogin !== 1) fail('ACCOUNT_NOT_READY', 'Native backend does not report a logged-in account');
    const profile = await this.backend.profile();
    if (profile?.wxid !== b.accountId) fail('ACCOUNT_MISMATCH', 'Backend account differs from the validated account');
    return {core, accountId: b.accountId};
  }
  async profile() { await this.check(); return this.backend.profile(); }
  async messages(recipientId) {
    required(recipientId, 'recipientId');
    const {accountId} = await this.check();
    if (!this.receipts) fail('MESSAGE_SOURCE_UNAVAILABLE', 'No validated message reader is connected');
    return validateRows(await this.receipts.outgoing(accountId, recipientId), accountId, recipientId);
  }
  async sendText({recipientId, text, operationKey}) {
    required(recipientId, 'recipientId'); required(text, 'text'); required(operationKey, 'operationKey');
    if (Buffer.byteLength(text, 'utf8') > 16000) fail('TEXT_TOO_LONG', 'Text exceeds 16000 UTF-8 bytes');
    if (this.busy || this.uncertain) fail('SEND_LOCKED', 'Another operation is active or has an unresolved outcome');
    this.busy = true;
    let reserved = false;
    try {
      const {accountId} = await this.check();
      if (!this.receipts || !this.reservations) fail('MESSAGE_SOURCE_UNAVAILABLE', 'A validated reader and durable reservations are required');
      if (!await this.receipts.hasConversation(accountId, recipientId)) fail('CONVERSATION_NOT_FOUND', 'Recipient must map to an existing conversation');
      const baseline = validateRows(await this.receipts.outgoing(accountId, recipientId), accountId, recipientId);
      const ids = new Set(baseline.map(row => row.serverId));
      const fingerprint = createHash('sha256').update(JSON.stringify([accountId, recipientId, text])).digest('hex');
      await this.reservations.reserve(operationKey, fingerprint, baseline);
      reserved = true;
      // Recheck identity after the reservation and immediately before submission.
      await this.check();
      const submittedAt = this.now();
      const response = await this.backend.submitText(recipientId, text);
      if (response?.ret !== 0) fail('SUBMISSION_NOT_CONFIRMED', 'Native submission was not accepted; inspect records before retrying');
      const deadline = this.now() + this.receiptTimeoutMs;
      for (;;) {
        await this.check();
        const rows = validateRows(await this.receipts.outgoing(accountId, recipientId), accountId, recipientId);
        const found = rows.filter(row => !ids.has(row.serverId) && row.text === text &&
          row.createdAtMs >= submittedAt && row.submissionFingerprint === fingerprint);
        if (found.length > 1) fail('AMBIGUOUS_RECEIPT', 'Multiple matching records; do not resend automatically');
        if (found.length === 1) return {status: 'server_record_observed', serverId: found[0].serverId,
          recipientId, deliveredToRecipient: 'unknown', readByRecipient: 'unknown'};
        if (this.now() >= deadline) fail('SEND_OUTCOME_UNKNOWN', 'No new correlated server record; do not resend automatically');
        await this.pause(Math.min(100, Math.max(1, deadline - this.now())));
      }
    } catch (error) {
      // Once reserved, even an HTTP failure is ambiguous. Never retry submission.
      if (reserved) this.uncertain = true;
      throw error;
    } finally { this.busy = false; }
  }
}

export async function coreDigest(path) {
  return createHash('sha256').update(await readFile(path)).digest('hex');
}
