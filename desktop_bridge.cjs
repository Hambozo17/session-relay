'use strict';

// A version-pinned follower for Codex Desktop's local owner protocol.
// Full conversation states are held in memory and reduced before stdout.
const crypto = require('node:crypto');
const fs = require('node:fs');
const net = require('node:net');
const os = require('node:os');
const path = require('node:path');
const readline = require('node:readline');
const defaultInstructions = require('./desktop-settings-policy.json').default_instructions;

const V = Object.freeze({
  'thread-stream-state-changed': 11,
  'thread-stream-following-changed': 1,
  'thread-owner-discovery': 1,
  'thread-follower-start-turn': 2,
  'thread-follower-interrupt-turn': 4,
});
const PIPE = '\\\\.\\pipe\\codex-ipc';
const HOST = 'local';
const MAX_FRAME = 268435456;
const MAX_THREADS = 5000;
const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;
const SETTINGS_KEYS = [
  'approvalPolicy', 'approvalsReviewer', 'sandboxPolicy', 'permissions',
  'runtimeWorkspaceRoots', 'model', 'effort', 'collaborationMode', 'cwd',
  'serviceTier', 'multiAgentMode',
];
const FORBIDDEN = new Set(['__proto__', 'constructor', 'prototype']);
function recoveryError(kind) {
  return ['quota','login','usagelimitexceeded','ratelimitexceeded','unauthorized','authenticationerror'].includes(String(kind || '').toLowerCase().replace(/[^a-z0-9]/g,''));
}

function error(message, kind) {
  const value = new Error(message);
  value.kind = kind || 'unavailable';
  return value;
}

function rejectedKind(message) {
  const result = message && message.result && typeof message.result === 'object' ? message.result : null;
  const raw = message && message.error !== undefined ? message.error : result && result.error;
  const code = raw && typeof raw === 'object' ? raw.code : undefined;
  if (typeof code === 'number' && Number.isInteger(code)) return 'ipc_code_' + String(code).replace('-', 'minus_');
  if (typeof code === 'string' && /^[A-Z][A-Z0-9_]{0,48}$/.test(code)) return code.toLowerCase();
  // Inspect only for known protocol phrases; never return or log the raw error payload.
  const detail = typeof raw === 'string' ? raw.toLowerCase()
    : raw && typeof raw.message === 'string' ? raw.message.toLowerCase() : '';
  // This exact coordinator rejection was confirmed in the installed 26.1007.2314.0
  // bundle: no connected client claimed the requested conversation.
  if (/no-client-found|no client found|client-cannot-handle-request|client cannot handle request|no client can handle|no handler for (this )?request|no owner found/.test(detail)) return 'no_owner';
  if (/client-disconnected|client disconnected/.test(detail)) return 'client_disconnected';
  if (/client.?type|client type/i.test(detail)) return 'client_type_rejected';
  if (/version|unsupported method|method not found|unknown method|protocol/i.test(detail)) return 'protocol_rejected';
  if (/busy|rate.?limit|too many requests|overload/i.test(detail)) return 'overloaded';
  if (/owner|thread|conversation|handler|route|target/.test(detail)) return 'owner_route_rejected';
  if (/timeout|timed out|deadline/.test(detail)) return 'timeout';
  const resultType = typeof message.resultType === 'string' && /^[A-Za-z]{1,24}$/.test(message.resultType)
    ? message.resultType.toLowerCase() : 'unknown_result';
  const errorType = raw === undefined ? 'no_error_field' : Array.isArray(raw) ? 'array_error' : typeof raw;
  const allowedKeys = new Set(['code', 'kind', 'message', 'name', 'reason', 'data', 'error']);
  const keys = raw && typeof raw === 'object' && !Array.isArray(raw)
    ? Object.keys(raw).filter((key) => allowedKeys.has(key)).sort().join('_') || 'no_safe_fields'
    : 'no_safe_fields';
  return 'ipc_rejected_' + resultType + '_' + errorType + '_' + keys;
}

function validId(id) {
  if (typeof id !== 'string' || !UUID.test(id)) throw error('invalid conversation identifier', 'incompatible');
  return id;
}

function asarIndex(file) {
  const fd = fs.openSync(file, 'r');
  try {
    const header = Buffer.alloc(16);
    if (fs.readSync(fd, header, 0, 16, 0) !== 16) throw new Error('short header');
    const format = header.readUInt32LE(0);
    const headerSize = header.readUInt32LE(4);
    const jsonSize = header.readUInt32LE(12);
    if (format !== 4 || headerSize < 16 || headerSize > 64 * 1024 * 1024 ||
        jsonSize < 2 || jsonSize > headerSize) throw new Error('unknown asar format');
    const json = Buffer.alloc(jsonSize);
    if (fs.readSync(fd, json, 0, jsonSize, 16) !== jsonSize) throw new Error('short index');
    return { fd, tree: JSON.parse(json.toString('utf8')), base: 8 + headerSize };
  } catch (cause) {
    fs.closeSync(fd);
    throw cause;
  }
}

function installedProtocolSource() {
  if (process.platform !== 'win32') throw error('Windows Desktop is required', 'incompatible');
  const suppliedArchive = process.env.CODEX_APP_ASAR;
  let archives;
  if (suppliedArchive) {
    const normalized = path.resolve(suppliedArchive);
    if (!/(?:^|[\\/])OpenAI\.Codex_[0-9].*_x64__[^/\\]+[\\/]app[\\/]resources[\\/]app\.asar$/i.test(normalized)) {
      throw error('installed Desktop protocol source was not found', 'incompatible');
    }
    archives = [normalized];
  } else {
    const root = path.join(process.env.ProgramFiles || 'C:\\Program Files', 'WindowsApps');
    const packages = fs.readdirSync(root)
      .filter((name) => /^OpenAI\.Codex_[0-9].*_x64__[^/\\]+$/i.test(name))
      .sort((a, b) => b.localeCompare(a, undefined, { numeric: true }));
    archives = packages.map((name) => path.join(root, name, 'app', 'resources', 'app.asar'));
  }
  for (const archivePath of archives) {
    if (!fs.existsSync(archivePath)) continue;
    const archive = asarIndex(archivePath);
    try {
      const entries = [];
      function walk(node, prefix) {
        for (const [key, entry] of Object.entries(node.files || {})) {
          const relative = prefix ? prefix + '/' + key : key;
          if (entry.files) walk(entry, relative);
          else if (!entry.unpacked && /\.vite\/build\/src-[^/\\]+\.js$/i.test(relative)) entries.push(entry);
        }
      }
      walk(archive.tree, '');
      for (const entry of entries) {
        const size = Number(entry.size);
        const offset = Number(entry.offset);
        if (!Number.isSafeInteger(size) || size <= 0 || size > 16 * 1024 * 1024 ||
            !Number.isSafeInteger(offset) || offset < 0) continue;
        const bytes = Buffer.alloc(size);
        if (fs.readSync(archive.fd, bytes, 0, size, archive.base + offset) !== size) continue;
        const source = bytes.toString('utf8');
        const at = source.indexOf('"thread-stream-state-changed":');
        if (at < 0) continue;
        const start = source.lastIndexOf('={', at);
        const end = source.indexOf('};', at);
        if (start >= 0 && end >= start && end - start < 8192) return source.slice(start, end + 2);
      }
    } finally {
      fs.closeSync(archive.fd);
    }
  }
  throw error('installed Desktop protocol source was not found', 'incompatible');
}

function verifyInstalledProtocol() {
  const source = installedProtocolSource();
  for (const [method, version] of Object.entries(V)) {
    if (!source.includes('"' + method + '":' + version)) {
      throw error('installed Desktop IPC protocol is incompatible', 'incompatible');
    }
  }
  return true;
}

function clone(value) {
  return value === undefined ? undefined : JSON.parse(JSON.stringify(value));
}

function canonical(value) {
  if (Array.isArray(value)) return value.map(canonical);
  if (value && typeof value === 'object') return Object.fromEntries(
    Object.keys(value).sort().map(key=>[key,canonical(value[key])]));
  return value;
}

function applyPatches(root, patches) {
  if (!Array.isArray(patches) || patches.length > 100000) throw new Error('invalid patch set');
  for (const patch of patches) {
    if (!patch || !Array.isArray(patch.path) ||
        patch.path.some((part) => FORBIDDEN.has(String(part)))) throw new Error('unsupported patch');
    if (patch.path.length === 0) {
      if (patch.op !== 'replace' && patch.op !== 'add') throw new Error('unsupported root patch');
      root = patch.value;
      continue;
    }
    let parent = root;
    for (const part of patch.path.slice(0, -1)) {
      if (parent == null || typeof parent !== 'object' || !(part in parent)) throw new Error('patch gap');
      parent = parent[part];
    }
    const key = patch.path[patch.path.length - 1];
    if (parent == null || typeof parent !== 'object') throw new Error('invalid patch target');
    if (patch.op === 'remove') {
      if (Array.isArray(parent)) {
        const index = Number(key);
        if (!Number.isInteger(index) || index < 0 || index >= parent.length) throw new Error('invalid array removal');
        parent.splice(index, 1);
      } else {
        if (!Object.prototype.hasOwnProperty.call(parent, key)) throw new Error('missing patch key');
        delete parent[key];
      }
    } else if (patch.op === 'add' || patch.op === 'replace') {
      if (Array.isArray(parent)) {
        const index = Number(key);
        if (!Number.isInteger(index) || index < 0 || index > parent.length ||
            (patch.op === 'replace' && index >= parent.length)) throw new Error('invalid array patch');
        if (patch.op === 'add') parent.splice(index, 0, clone(patch.value));
        else parent[index] = clone(patch.value);
      } else {
        if (patch.op === 'replace' && !Object.prototype.hasOwnProperty.call(parent, key)) throw new Error('missing patch key');
        parent[key] = clone(patch.value);
      }
    } else throw new Error('unsupported patch operation');
  }
  return root;
}

function turnsOf(state) {
  if (state.turnHistory && state.turnHistory.kind === 'canonical') {
    const history = state.turnHistory.history || {};
    const entities = history.entitiesByKey || {};
    const turns = (history.islands || []).flatMap((island) =>
      (island.entries || []).map((entry) => entities[entry.value]).filter(Boolean));
    if (turns.length) return turns;
    return Object.values(entities).sort((a, b) => (a.turnStartedAtMs || 0) - (b.turnStartedAtMs || 0));
  }
  return Array.isArray(state.turns) ? state.turns : [];
}

function first(...values) {
  return values.find((value) => value !== undefined && value !== null);
}

function settingsOf(state) {
  const t = state.threadSettings || {};
  const permissions = first(state.currentPermissions, state.permissions, t.permissions, state.permissionSelection);
  const values = {
    approvalPolicy: first(state.approvalPolicy, state.latestApprovalPolicy, t.approvalPolicy),
    approvalsReviewer: first(state.approvalsReviewer, t.approvalsReviewer),
    sandboxPolicy: first(state.sandboxPolicy, t.sandboxPolicy),
    permissions,
    runtimeWorkspaceRoots: first(state.runtimeWorkspaceRoots, t.runtimeWorkspaceRoots),
    model: first(state.latestModel, state.model, t.model),
    effort: first(state.latestModelReasoningEffort, state.latestReasoningEffort, state.reasoningEffort, state.effort, t.effort),
    collaborationMode: first(state.latestCollaborationMode, state.collaborationMode, t.collaborationMode),
    cwd: first(state.cwd, t.cwd),
    serviceTier: first(state.serviceTier, t.serviceTier),
    multiAgentMode: first(state.multiAgentMode, t.multiAgentMode),
  };
  const output = {};
  for (const key of SETTINGS_KEYS) if (values[key] !== undefined) output[key] = clone(values[key]);
  return output;
}

function gitWorkspaceIdentity(root) {
  if (typeof root !== 'string' || !path.isAbsolute(root)) return null;
  try {
    const marker=path.join(root,'.git');
    const info=fs.statSync(marker);
    let common;
    if (info.isDirectory()) common=fs.realpathSync(marker);
    else if (info.isFile() && info.size<=4096) {
      const text=fs.readFileSync(marker,'utf8').trim();
      if (!text.startsWith('gitdir: ')) return null;
      const admin=fs.realpathSync(path.resolve(path.dirname(marker),text.slice(8)));
      const link=path.join(admin,'commondir'),back=path.join(admin,'gitdir');
      if (!fs.statSync(link).isFile() || !fs.statSync(back).isFile() ||
          Math.max(fs.statSync(link).size,fs.statSync(back).size)>4096) return null;
      common=fs.realpathSync(path.resolve(admin,fs.readFileSync(link,'utf8').trim()));
      if (path.dirname(admin)!==path.join(common,'worktrees') ||
          fs.realpathSync(fs.readFileSync(back,'utf8').trim())!==fs.realpathSync(marker)) return null;
    } else return null;
    if (!fs.statSync(common).isDirectory() || !fs.statSync(path.join(common,'HEAD')).isFile() ||
        !fs.statSync(path.join(common,'config')).isFile()) return null;
    return common.replace(/\\/g,'/').toLowerCase();
  } catch (_) { return null; }
}

function summarize(state, owner) {
  const turns = turnsOf(state);
  const last = turns.length ? turns[turns.length - 1] : {};
  const runtime = state.threadRuntimeStatus && state.threadRuntimeStatus.type || 'unknown';
  const requests = Array.isArray(state.requests) ? state.requests : [];
  const activeFlags = state.threadRuntimeStatus && state.threadRuntimeStatus.activeFlags;
  const rawStatus = last.status;
  let status = 'unknown';
  if (rawStatus === 'inProgress' && runtime === 'active') status = 'running';
  else if (rawStatus === 'interrupted') status = 'interrupted';
  else if (rawStatus === 'failed') status = 'failed';
  else if (rawStatus === 'completed') status = 'idle';
  const settings = settingsOf(state);
  const fingerprintSettings = clone(settings);
  const mode = fingerprintSettings.collaborationMode;
  const modeSettings = mode && mode.settings;
  if (fingerprintSettings.effort == null && modeSettings && modeSettings.model === fingerprintSettings.model &&
      typeof modeSettings.reasoning_effort === 'string') fingerprintSettings.effort = modeSettings.reasoning_effort;
  const instructions = mode && mode.settings && mode.settings.developer_instructions;
  // Ignore only a verbatim built-in template verified in the installed binary.
  // Preserve the raw snapshot, all custom instructions, model, effort and mode.
  if (mode && mode.mode === 'default' && typeof instructions === 'string' &&
      defaultInstructions.includes(instructions.replace(/\r\n/g,'\n'))) {
    mode.settings.developer_instructions = null;
  }
  // Desktop adds a per-thread visualization root when starting a turn. Under
  // full filesystem access this grants no new permission. Ignore only that
  // exact generated root; preserve every project root and restricted policy.
  const permissions = fingerprintSettings.permissions;
  const sandbox = permissions && permissions.sandboxPolicy || fingerprintSettings.sandboxPolicy;
  if (sandbox && ['dangerFullAccess','danger-full-access'].includes(sandbox.type) && UUID.test(String(state.id))) {
    const normalize = value => String(value).replace(/\\/g,'/').replace(/\/+/g,'/').toLowerCase();
    const prefix = normalize(path.join(os.homedir(),'.codex','visualizations')) + '/';
    const generated = root => {
      if (typeof root !== 'string') return false;
      const value=normalize(root);
      if (!value.startsWith(prefix)) return false;
      const tail=value.slice(prefix.length).replace(/\/$/,'');
      return /^\d{4}\/\d{2}\/\d{2}\//.test(tail) && tail.slice(11)===state.id.toLowerCase();
    };
    for (const holder of [fingerprintSettings,permissions]) {
      if (holder && Array.isArray(holder.runtimeWorkspaceRoots)) {
        holder.runtimeWorkspaceRoots=holder.runtimeWorkspaceRoots.filter(root=>!generated(root));
        // A child already contained in another full-access workspace root
        // adds no scope. Preserve raw roots and compare only their coverage.
        const absolute = root => {
          if (typeof root !== 'string') return null;
          const value=path.win32.normalize(root).replace(/\\/g,'/').toLowerCase().replace(/\/+$/,'');
          return /^[a-z]:\//.test(value) || /^[a-z]:$/.test(value) && root.length>2 ? value : null;
        };
        const roots=holder.runtimeWorkspaceRoots, keys=roots.map(absolute);
        holder.runtimeWorkspaceRoots=roots.filter((root,i)=>!keys[i] ||
          !keys.some(parent=>parent && keys[i]!==parent && keys[i].startsWith(parent+'/')));
        holder.runtimeWorkspaceRoots=holder.runtimeWorkspaceRoots.map(root=>{
          const identity=gitWorkspaceIdentity(root);
          return identity ? 'git-common:'+identity : root;
        }).filter((root,i,all)=>typeof root!=='string' || !root.startsWith('git-common:') || all.indexOf(root)===i);
      }
    }
  }
  const settingsFingerprint = crypto.createHash('sha256').update(JSON.stringify(canonical(fingerprintSettings))).digest('hex');
  const nativeError = last.error && last.error.codexErrorInfo;
  const rawError = last.error && (last.error.code || last.error.kind ||
    (typeof nativeError === 'string' ? nativeError : nativeError && (nativeError.type || nativeError.kind)));
  const errorKind = typeof rawError === 'string' && /^[A-Za-z0-9_.:-]{1,80}$/.test(rawError) ? rawError : null;
  const source = state.threadSource;
  return {
    id: state.id,
    owner,
    title: typeof state.title === 'string' ? state.title : state.id,
    cwd: typeof state.cwd === 'string' ? state.cwd : null,
    turn_id: last.turnId || null,
    turn_started_at_ms: Number.isFinite(last.turnStartedAtMs) ? last.turnStartedAtMs : null,
    status,
    runtime_status: runtime,
    waiting: requests.length > 0 || (Array.isArray(activeFlags) && activeFlags.length > 0) ||
      (Array.isArray(state.unconfirmedTurnSubmissions) && state.unconfirmedTurnSubmissions.length > 0),
    waiting_reasons: requests.map((item) => typeof item.kind === 'string' ? item.kind : 'request'),
    is_child: !!state.agentNickname || source === 'subagent' ||
      (!!source && typeof source === 'object' && !!source.subAgent) ||
      String(source || '').toLowerCase().includes('subagent'),
    ephemeral: !!state.ephemeral || !!state.sideConversation,
    model: settings.model || null,
    model_provider: state.modelProvider || state.latestModelProvider || null,
    effort: settings.effort || null,
    settings,
    settings_fingerprint: settingsFingerprint,
    goal_status: state.threadGoal && state.threadGoal.status && String(state.threadGoal.status).replace(/[A-Z]/g, c => '_' + c.toLowerCase()) || null,
    client_user_message_id: last.params && last.params.clientUserMessageId || null,
    error_kind: errorKind,
  };
}

function delay(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

async function mapLimit(items, limit, fn) {
  const results = new Array(items.length);
  let index = 0;
  async function worker() {
    while (true) {
      const current = index++;
      if (current >= items.length) return;
      try { results[current] = await fn(items[current]); }
      catch (cause) { results[current] = { error: cause.kind || 'unavailable' }; }
    }
  }
  await Promise.all(Array.from({ length: Math.min(limit, items.length) }, worker));
  return results;
}

class Bridge {
  constructor(emit) {
    this.emit = emit;
    this.buffer = Buffer.alloc(0);
    this.client = null;
    this.socket = null;
    this.pending = new Map();
    this.states = new Map();
    this.followed = new Set();
    this.attempts = new Set();
    this.compatible = false;
    this.ready = false;
    this.closing = false;
  }

  connect() {
    verifyInstalledProtocol();
    this.compatible = true;
    this.socket = net.connect(PIPE);
    this.socket.on('connect', async () => {
      try {
        const response = await this.request('initialize', { clientType: 'session-relay' }, null, 8000, false);
        const id = response && response.result && response.result.clientId;
        if (typeof id !== 'string' || !id) throw error('Desktop coordinator identity is unavailable', 'incompatible');
        this.client = id;
        this.ready = true;
        this.emit({ event: 'connected', compatible: true });
      } catch (cause) {
        this.emit({ event: 'error', kind: cause.kind || 'incompatible', message: cause.message });
        this.socket.destroy();
      }
    });
    this.socket.on('data', (chunk) => this.onData(chunk));
    this.socket.on('error', () => {
      this.rejectAll(error('Desktop coordinator pipe is unavailable'));
      this.emit({ event: 'error', kind: 'unavailable', message: 'Desktop coordinator pipe is unavailable' });
    });
    this.socket.on('close', () => {
      this.ready = false;
      this.rejectAll(error('Desktop coordinator disconnected', 'uncertain'));
      if (!this.closing) this.emit({ event: 'disconnected' });
      if (this.closing && require.main === module) setTimeout(() => process.exit(0), 10);
    });
  }

  rejectAll(cause) {
    for (const item of this.pending.values()) {
      clearTimeout(item.timer);
      item.reject(item.mutating ? error('Desktop mutation outcome is uncertain; do not retry', 'uncertain') : cause);
    }
    this.pending.clear();
  }

  write(message) {
    if (!this.socket || this.socket.destroyed) throw error('Desktop coordinator is disconnected');
    const payload = Buffer.from(JSON.stringify(message), 'utf8');
    if (!payload.length || payload.length > MAX_FRAME) throw error('Desktop IPC frame is outside supported bounds', 'incompatible');
    const header = Buffer.alloc(4);
    header.writeUInt32LE(payload.length, 0);
    this.socket.write(Buffer.concat([header, payload]));
  }

  request(method, params, target, timeoutMs, mutating) {
    if (method !== 'initialize' && !Object.prototype.hasOwnProperty.call(V, method)) {
      return Promise.reject(error('unsupported Desktop IPC method', 'incompatible'));
    }
    return new Promise((resolve, reject) => {
      const requestId = crypto.randomUUID();
      const timer = setTimeout(() => {
        this.pending.delete(requestId);
        reject(error(mutating
          ? 'Desktop mutation timed out; outcome is uncertain and must not be retried'
          : 'Desktop read request timed out', mutating ? 'uncertain' : 'timeout'));
      }, timeoutMs);
      this.pending.set(requestId, { resolve, reject, timer, mutating: !!mutating });
      const message = {
        type: 'request',
        requestId,
        sourceClientId: this.client || 'session-relay-initializing',
        method,
        params,
        version: method === 'initialize' ? 0 : V[method],
        timeoutMs,
      };
      if (target) message.targetClientId = target;
      try { this.write(message); }
      catch (cause) {
        clearTimeout(timer);
        this.pending.delete(requestId);
        reject(mutating ? error('Desktop mutation write outcome is uncertain; do not retry', 'uncertain') : cause);
      }
    });
  }

  onData(chunk) {
    try {
      this.buffer = Buffer.concat([this.buffer, chunk]);
      while (this.buffer.length >= 4) {
        const length = this.buffer.readUInt32LE(0);
        if (!length || length > MAX_FRAME) throw new Error('invalid frame length');
        if (this.buffer.length < length + 4) break;
        const message = JSON.parse(this.buffer.subarray(4, length + 4).toString('utf8'));
        this.buffer = this.buffer.subarray(length + 4);
        this.receive(message);
      }
    } catch (_cause) {
      this.compatible = false;
      this.states.clear();
      this.rejectAll(error('Desktop IPC framing is incompatible', 'incompatible'));
      this.emit({ event: 'error', kind: 'incompatible', message: 'Desktop IPC framing is incompatible' });
      this.socket.destroy();
    }
  }

  receive(message) {
    if (!message || typeof message !== 'object') return;
    if (message.type === 'response') {
      const pending = this.pending.get(message.requestId);
      if (!pending) return;
      clearTimeout(pending.timer);
      this.pending.delete(message.requestId);
      if (message.resultType === 'success') pending.resolve(message);
      else pending.reject(error('Desktop IPC request was rejected', pending.mutating ? 'rejected' : rejectedKind(message)));
      return;
    }
    if (message.type === 'client-discovery-request') {
      try {
        this.write({ type: 'client-discovery-response', requestId: message.requestId, response: { canHandle: false } });
      } catch (_cause) { /* disconnect is surfaced by the socket */ }
      return;
    }
    if (message.type !== 'broadcast') return;
    if (message.method === 'client-status-changed' &&
        message.params && message.params.status === 'disconnected') {
      for (const [id, state] of this.states) if (state.owner === message.params.clientId) this.states.delete(id);
      return;
    }
    if (message.method !== 'thread-stream-state-changed' ||
        !message.params || message.params.hostId !== HOST) return;
    if (message.version !== V['thread-stream-state-changed']) {
      this.compatible = false;
      this.states.clear();
      this.emit({ event: 'incompatible', observed_version: message.version });
      return;
    }
    if (!this.compatible) return;
    const id = message.params.conversationId;
    if (typeof id !== 'string' || !this.followed.has(id)) return;
    const change = message.params.change || {};
    try {
      if (change.type === 'snapshot') {
        if (!change.conversationState || typeof message.sourceClientId !== 'string') throw new Error('invalid snapshot');
        this.states.set(id, { state: change.conversationState, revision: change.revision, owner: message.sourceClientId });
      } else if (change.type === 'patches') {
        const current = this.states.get(id);
        if (!current || current.owner !== message.sourceClientId ||
            current.revision !== change.baseRevision || !Array.isArray(change.patches)) {
          this.states.delete(id);
          return;
        }
        current.state = applyPatches(current.state, change.patches);
        current.revision = change.revision;
      }
    } catch (_cause) {
      this.states.delete(id);
    }
  }

  follow(id, refresh = false) {
    if (this.followed.has(id) && !refresh) return;
    this.followed.add(id);
    this.write({
      type: 'broadcast',
      method: 'thread-stream-following-changed',
      sourceClientId: this.client,
      version: V['thread-stream-following-changed'],
      params: { conversationId: id, hostId: HOST, following: true },
    });
  }

  async ownerFor(id, timeoutMs = 1800) {
    let response;
    try {
      response = await this.request(
        'thread-owner-discovery', { hostId: HOST, conversationId: id }, null, timeoutMs, false);
    } catch (cause) {
      if (cause.kind === 'no_owner') return null;
      throw cause;
    }
    const owner = response && (response.handledByClientId || response.result && response.result.handledByClientId);
    if (owner === null || owner === undefined || owner === '') return null;
    if (typeof owner !== 'string') throw error('malformed owner response', 'incompatible');
    return owner;
  }

  async scan(ids, waitMs, options = {}) {
    if (!Array.isArray(ids) || ids.length > MAX_THREADS) throw error('local conversation inventory is incomplete', 'incompatible');
    const unique = Array.from(new Set(ids.map(validId)));
    for (const id of unique) this.states.delete(id);
    const ownerLimit = Number.isInteger(options.owner_limit) ? Math.max(1, Math.min(48, options.owner_limit)) : 16;
    const ownerTimeout = Number.isInteger(options.owner_timeout_ms) ? Math.max(250, Math.min(10000, options.owner_timeout_ms)) : 1800;
    const ownerDeadline = Date.now() + 45000;
    const readOwner = async (id, timeout) => {
      const remaining = ownerDeadline - Date.now();
      if (remaining <= 0) throw error('owner inventory read deadline reached', 'timeout');
      return this.ownerFor(id, Math.min(remaining, timeout));
    };
    const rows = await mapLimit(unique, ownerLimit, async (id) => {
      try { return { id, owner: await readOwner(id, ownerTimeout) }; }
      catch (cause) { return { id, error: cause.kind || 'unavailable' }; }
    });
    // Large saved histories can briefly overload owner discovery. Retry only
    // failed READS at lower concurrency; never treat a timeout as no owner.
    const retry = rows.filter(row => ['timeout', 'overloaded'].includes(row.error));
    if (retry.length <= 64) {
      await mapLimit(retry, 4, async (row) => {
        try {
          row.owner = await readOwner(row.id, Math.max(ownerTimeout, 5000));
          delete row.error;
        } catch (cause) { row.error = cause.kind || 'unavailable'; }
      });
    }
    if (!this.compatible) throw error('installed Desktop IPC protocol is incompatible', 'incompatible');
    const owned = rows.filter((row) => row.owner);
    for (const row of owned) this.follow(row.id, true);
    const until = Date.now() + Math.max(0, Math.min(Number(waitMs) || 0, 10000));
    while (Date.now() < until) {
      if (!owned.some((row) => {
        const state = this.states.get(row.id);
        return !state || state.owner !== row.owner;
      })) break;
      await delay(75);
    }
    // Refresh only missing snapshots. A busy owner may need longer than the
    // short post-interrupt scan, even after ownership itself was confirmed.
    const missing = owned.filter(row => {
      const state = this.states.get(row.id);
      return !state || state.owner !== row.owner;
    });
    for (const row of missing) this.follow(row.id, true);
    const retryUntil = Math.min(ownerDeadline + 10000, Date.now() + 5000);
    while (missing.length && Date.now() < retryUntil) {
      if (missing.every(row => this.states.get(row.id)?.owner === row.owner)) break;
      await delay(75);
    }
    const sessions = [];
    const unresolved = [];
    for (const row of rows) {
      if (row.error) {
        unresolved.push({ id: row.id, reason: row.error });
      } else if (row.owner) {
        const state = this.states.get(row.id);
        if (!state || state.owner !== row.owner) unresolved.push({ id: row.id, reason: 'owner_state_unavailable' });
        else sessions.push(summarize(state.state, state.owner));
      }
    }
    const errors = {};
    for (const row of unresolved) errors[row.reason] = (errors[row.reason] || 0) + 1;
    return {
      compatible: this.compatible,
      sessions,
      unresolved,
      diagnostics: { owner_found: owned.length, owner_null: rows.filter((row) => !row.error && !row.owner).length, errors },
    };
  }

  async fresh(id, expectedOwner) {
    const owner = await this.ownerFor(id);
    if (!owner || (expectedOwner && owner !== expectedOwner)) throw error('conversation owner changed or is unavailable', 'no_owner');
    const state = this.states.get(id);
    if (!state || state.owner !== owner) throw error('conversation state is unavailable', 'no_owner');
    return { owner, summary: summarize(state.state, owner) };
  }

  async waitFor(id, predicate, timeout, message) {
    const deadline = Date.now() + timeout;
    while (Date.now() < deadline) {
      if (!this.compatible) throw error('installed Desktop IPC protocol is incompatible', 'incompatible');
      const state = this.states.get(id);
      if (state) {
        const summary = summarize(state.state, state.owner);
        if (predicate(summary)) return summary;
      }
      await delay(100);
    }
    throw error(message, 'uncertain');
  }

  async interrupt(command) {
    const id = validId(command.thread);
    if (!UUID.test(String(command.operation_id || '')) || !command.expected_turn) {
      throw error('invalid pause operation identity', 'incompatible');
    }
    const current = await this.fresh(id, command.expected_owner);
    const state = current.summary;
    if (state.turn_id !== command.expected_turn || state.status !== 'running' ||
        state.runtime_status !== 'active' || state.waiting || state.is_child || state.ephemeral ||
        state.settings_fingerprint !== command.settings_fingerprint) {
      throw error('conversation changed before pause; no request was sent', 'state_changed');
    }
    const key = 'interrupt:' + command.operation_id + ':' + id;
    if (this.attempts.has(key)) throw error('pause was already attempted; reconcile before retry', 'uncertain');
    this.attempts.add(key);
    let response, confirmation = 'native-acknowledgement';
    try {
      response = await this.request(
        'thread-follower-interrupt-turn',
        { conversationId: id, mode: 'user-stop', expectedTurnId: command.expected_turn },
        current.owner, 20000, true);
    } catch (cause) {
      if (!['rejected', 'uncertain'].includes(cause.kind)) throw cause;
      // Native interrupt can succeed before a later handler/transport failure.
      // Reconcile its exact postcondition; never submit the interrupt again.
      confirmation = 'verified-postcondition-after-request-failure';
    }
    if (response) {
      const stopped = response.result && (response.result.interruptedTurnId || response.result.result && response.result.result.interruptedTurnId);
      if (stopped !== command.expected_turn) throw error('pause acknowledgement did not match the selected turn', 'uncertain');
    }
    const confirmed = await this.waitFor(id, (item) =>
      item.turn_id === command.expected_turn && item.status === 'interrupted' &&
      ['idle', 'notLoaded'].includes(item.runtime_status) && !item.waiting &&
      item.settings_fingerprint === command.settings_fingerprint && item.goal_status === state.goal_status &&
      !item.is_child && !item.ephemeral,
    12000, 'pause result could not be confirmed; do not retry');
    return { session: confirmed, owner: current.owner, confirmation };
  }

  async resume(command) {
    const id = validId(command.thread);
    const operation = command.operation_id;
    if (!UUID.test(String(operation || '')) || !command.expected_turn ||
        !command.request || command.request.threadId !== id ||
        command.request.clientUserMessageId !== operation ||
        !Array.isArray(command.request.input) || command.request.input.length !== 1) {
      throw error('invalid continuation request', 'incompatible');
    }
    const current = await this.fresh(id, command.expected_owner);
    const state = current.summary;
    const allowed = state.status === 'interrupted' ||
      (state.status === 'failed' && (recoveryError(state.error_kind) || state.goal_status === 'active')) ||
      (state.status === 'idle' && state.goal_status === 'active');
    if (state.turn_id !== command.expected_turn || !allowed ||
        !['idle', 'notLoaded'].includes(state.runtime_status) || state.waiting ||
        state.is_child || state.ephemeral || ![null, 'active'].includes(state.goal_status) ||
        state.settings_fingerprint !== command.settings_fingerprint) {
      throw error('conversation changed or is ineligible for automatic continuation', 'state_changed');
    }
    const key = 'resume:' + operation + ':' + id;
    if (this.attempts.has(key)) throw error('continuation was already attempted; reconcile before retry', 'uncertain');
    this.attempts.add(key);
    await this.request('thread-follower-start-turn', {
      conversationId: id,
      turnStart: {
        request: command.request,
        context: {
          inheritThreadSettings: true,
          usePermissionSelection: false,
          useAppServerPermissionDefault: false,
        },
      },
    }, current.owner, 30000, true);
    const confirmed = await this.waitFor(id, (item) =>
      item.turn_id && item.turn_id !== command.expected_turn &&
      item.client_user_message_id === operation && ['running', 'idle'].includes(item.status) &&
      item.settings_fingerprint === command.settings_fingerprint &&
      [null, 'active', 'completed'].includes(item.goal_status),
    15000, 'continuation result could not be confirmed; do not retry');
    return { session: confirmed, owner: current.owner };
  }

  async command(command) {
    if (!this.ready || !this.compatible) throw error('Desktop coordinator is not compatible or ready', 'incompatible');
    if (command.op === 'scan') return this.scan(command.threads, command.wait_ms, command);
    if (command.op === 'snapshot') {
      const result = await this.fresh(validId(command.thread));
      return { session: result.summary, owner: result.owner };
    }
    if (command.op === 'interrupt') return this.interrupt(command);
    if (command.op === 'resume') return this.resume(command);
    if (command.op === 'close') {
      this.closing = true;
      if (this.socket && !this.socket.destroyed) this.socket.end();
      return { closed: true };
    }
    throw error('unknown bridge command', 'incompatible');
  }
}

if (require.main === module) {
  const emit = (message) => process.stdout.write(JSON.stringify(message) + '\n');
  let bridge;
  try {
    bridge = new Bridge(emit);
    bridge.connect();
  } catch (cause) {
    emit({
      event: 'error',
      kind: cause.kind || 'incompatible',
      message: cause.kind === 'unavailable'
        ? 'Codex Desktop coordinator is unavailable'
        : 'installed Desktop IPC protocol is incompatible',
    });
    process.exitCode = 2;
  }
  if (bridge) {
    readline.createInterface({ input: process.stdin }).on('line', async (line) => {
      let command;
      try {
        command = JSON.parse(line);
        const result = await bridge.command(command);
        emit({ id: command.id, result });
        if (command.op === 'close') process.exit(0);
      } catch (cause) {
        emit({
          id: command && command.id,
          kind: cause.kind || 'unavailable',
          error: cause.message || 'Desktop bridge request failed',
        });
      }
    }).on('close', () => {
      bridge.closing = true;
      if (bridge.socket && !bridge.socket.destroyed) bridge.socket.end();
    });
  }
}

module.exports = { Bridge, applyPatches, summarize, verifyInstalledProtocol, METHOD_VERSIONS: V };
