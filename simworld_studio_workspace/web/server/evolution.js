'use strict';

const fs = require('fs');
const path = require('path');
const crypto = require('crypto');
const { spawn } = require('child_process');

const {
  ALLOWED_PRIMITIVES,
  buildDynamicToolDef,
  dryRunExpansion,
  validateParamsSchemaShape,
  validateToolShape,
} = require('./learned-tool-runtime');

const NL = String.fromCharCode(10);
const SESSION_PENDING = 'pending';
const SESSION_ARCHIVED = 'archived';
const SESSION_FAILED_UNRESOLVED = 'failed_unresolved';

function ensureDir(dirPath) {
  fs.mkdirSync(dirPath, { recursive: true });
}

function safeJsonParse(text) {
  try {
    return JSON.parse(text);
  } catch {
    return null;
  }
}

function nowIso() {
  return new Date().toISOString();
}

function readJsonFileSafe(filePath, fallback) {
  try {
    const raw = fs.readFileSync(filePath, 'utf-8');
    const parsed = safeJsonParse(raw);
    return parsed && typeof parsed === 'object' ? parsed : fallback;
  } catch {
    return fallback;
  }
}

function writeJsonFileSafe(filePath, value) {
  try {
    ensureDir(path.dirname(filePath));
    fs.writeFileSync(filePath, `${JSON.stringify(value, null, 2)}${NL}`, 'utf-8');
  } catch {
    // best effort only
  }
}

function resolveEvolutionConfigPath(arenaDataDir) {
  const base = arenaDataDir || path.resolve(__dirname, '../../arena_data');
  return path.join(base, 'evolution', 'config.json');
}

function readEvolutionConfig(arenaDataDir) {
  const filePath = resolveEvolutionConfigPath(arenaDataDir);
  const parsed = readJsonFileSafe(filePath, {});
  return {
    enabled: typeof parsed.enabled === 'boolean' ? parsed.enabled : true,
    updatedAt: parsed.updatedAt || null,
    source: parsed.source || null,
    configPath: filePath,
  };
}

function writeEvolutionConfig(arenaDataDir, enabled, source) {
  const filePath = resolveEvolutionConfigPath(arenaDataDir);
  const payload = {
    enabled: Boolean(enabled),
    updatedAt: nowIso(),
    source: String(source || 'api'),
  };
  writeJsonFileSafe(filePath, payload);
  return {
    enabled: payload.enabled,
    updatedAt: payload.updatedAt,
    source: payload.source,
    configPath: filePath,
  };
}

function slugify(input) {
  const src = String(input || '').toLowerCase();
  let out = '';
  let prevUnderscore = false;

  for (const ch of src) {
    const code = ch.charCodeAt(0);
    const isLower = code >= 97 && code <= 122;
    const isDigit = code >= 48 && code <= 57;
    const isUnderscore = ch === '_';

    if (isLower || isDigit || isUnderscore) {
      out += ch;
      prevUnderscore = false;
    } else if (!prevUnderscore) {
      out += '_';
      prevUnderscore = true;
    }
  }

  while (out.startsWith('_')) out = out.slice(1);
  while (out.endsWith('_')) out = out.slice(0, -1);

  return out.slice(0, 80);
}

function tokenize(text) {
  const src = String(text || '').toLowerCase();
  const out = [];
  let cur = '';

  for (const ch of src) {
    const code = ch.charCodeAt(0);
    const isLower = code >= 97 && code <= 122;
    const isDigit = code >= 48 && code <= 57;
    const keep = isLower || isDigit || ch === '_';
    if (keep) {
      cur += ch;
    } else if (cur) {
      out.push(cur);
      cur = '';
    }
  }

  if (cur) out.push(cur);
  return out;
}

function uniqueStrings(values) {
  const out = [];
  const seen = new Set();
  for (const value of values || []) {
    const text = String(value || '').trim();
    if (!text || seen.has(text)) continue;
    seen.add(text);
    out.push(text);
  }
  return out;
}

function trimText(text, maxLen) {
  const t = String(text || '');
  if (t.length <= maxLen) return t;
  return `${t.slice(0, maxLen)}${NL}...`;
}

function stripMcpPrefix(name) {
  let n = String(name || 'unknown');
  if (n.startsWith('mcp__')) {
    const idx = n.indexOf('__', 5);
    if (idx >= 0 && idx + 2 < n.length) {
      n = n.slice(idx + 2);
    }
  }
  return n;
}

function parseFrontmatter(rawText) {
  const raw = String(rawText || '');
  const match = raw.match(/^---\n([\s\S]*?)\n---\n([\s\S]*)$/);
  if (!match) return { meta: {}, content: raw };

  const lines = match[1].split('\n');
  const out = {};

  for (const line of lines) {
    const m = line.match(/^(\w+):\s*(.*)$/);
    if (!m) continue;

    const key = m[1];
    const value = m[2].trim();

    if (value.startsWith('[') && value.endsWith(']')) {
      out[key] = value
        .slice(1, -1)
        .split(',')
        .map((v) => v.trim())
        .filter(Boolean)
        .map((v) => v.replace(/^['"]|['"]$/g, ''));
      continue;
    }

    out[key] = value.replace(/^['"]|['"]$/g, '');
  }

  return { meta: out, content: match[2].trim() };
}

function yamlScalar(value) {
  if (value == null) return '""';
  const v = String(value);
  return JSON.stringify(v);
}

function yamlArray(values) {
  const arr = Array.isArray(values) ? values : [];
  return `[${arr.map((v) => JSON.stringify(String(v))).join(', ')}]`;
}

function humanizeId(id) {
  return String(id || '')
    .split('_')
    .filter(Boolean)
    .map((w) => (w ? `${w.slice(0, 1).toUpperCase()}${w.slice(1)}` : ''))
    .join(' ')
    .trim();
}

function mentionsLearnedTool(content, toolId) {
  const id = slugify(toolId || '');
  if (!id) return false;
  const text = String(content || '').toLowerCase();
  const mcpName = `learned__${id}`.toLowerCase();
  if (text.includes(mcpName)) return true;

  const escapedId = id.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
  const idRegex = new RegExp(`\\b${escapedId}\\b`, 'i');
  return idRegex.test(text);
}

function extractLearnedToolRefs(content) {
  const refs = new Set();
  const src = String(content || '');
  const re = /learned__([a-z0-9_]+)/gi;
  let m;
  while ((m = re.exec(src)) !== null) {
    const id = slugify(m[1] || '');
    if (id) refs.add(id);
  }
  return refs;
}

function buildToolUsageSkillContent(tool, subpartText) {
  const id = slugify(tool && (tool.id || tool.name) ? (tool.id || tool.name) : '');
  const mcpName = `learned__${id}`;
  const displayName = String((tool && tool.name) || humanizeId(id) || id || 'Learned Tool');

  const subpartLine = subpartText
    ? `This skill was linked from subpart: "${String(subpartText).trim()}".`
    : 'This skill was linked from a reusable prompt subpart.';

  return [
    `## ${displayName}`,
    '',
    '### When To Use',
    subpartLine,
    `Use \`${mcpName}\` for requests that match this reusable operation.`,
    '',
    '### Execution Policy',
    `Call \`${mcpName}\` first when its parameters can represent the request.`,
    'Fallback to primitive calls only for behavior that cannot be represented by this tool.',
    '',
    '### Parameterization',
    'Keep requests parameterized so the tool can generalize across scenes.',
  ].join(NL);
}

function extractToolTraceFromStreamJson(rawText) {
  const lines = String(rawText || '')
    .split(NL)
    .map((l) => l.trim())
    .filter(Boolean);

  const byId = new Map();
  const startOrder = [];
  let seq = 0;
  let lastToolId = null;

  function ensureEntry(id, name) {
    if (!id) return null;
    if (!byId.has(id)) {
      byId.set(id, {
        id,
        name: name || 'unknown',
        inputBuffer: '',
        input: null,
        result: '',
        ok: false,
        seq: seq += 1,
      });
      startOrder.push(id);
    }
    const entry = byId.get(id);
    if (name && entry.name === 'unknown') entry.name = name;
    return entry;
  }

  for (const line of lines) {
    const event = safeJsonParse(line);
    if (!event || typeof event !== 'object') continue;

    if (event.type === 'stream_event') {
      const ev = event.event || {};
      if (ev.type === 'content_block_start' && ev.content_block && ev.content_block.type === 'tool_use') {
        const entry = ensureEntry(ev.content_block.id, ev.content_block.name);
        if (entry) {
          if (ev.content_block.input && typeof ev.content_block.input === 'object') {
            entry.input = ev.content_block.input;
          }
          lastToolId = entry.id;
        }
      }
      if (ev.type === 'content_block_delta' && ev.delta && ev.delta.type === 'input_json_delta') {
        const entry = lastToolId ? ensureEntry(lastToolId) : null;
        if (entry) entry.inputBuffer += String(ev.delta.partial_json || '');
      }
      continue;
    }

    if (event.type === 'assistant') {
      const content = event.message && Array.isArray(event.message.content) ? event.message.content : [];
      for (const block of content) {
        if (block.type === 'tool_use') {
          const entry = ensureEntry(block.id, block.name);
          if (entry && block.input && typeof block.input === 'object') {
            entry.input = block.input;
          }
          if (entry) lastToolId = entry.id;
        }
      }
      continue;
    }

    if (event.type === 'user') {
      const content = event.message && Array.isArray(event.message.content) ? event.message.content : [];
      for (const block of content) {
        if (block.type !== 'tool_result') continue;
        const entry = ensureEntry(block.tool_use_id);
        if (!entry) continue;

        let result = '';
        if (Array.isArray(block.content)) {
          result = block.content.map((c) => (c && c.text ? c.text : '')).join('');
        } else {
          result = String(block.content || '');
        }

        entry.result = result;
        entry.ok = !block.is_error;
      }
    }
  }

  const out = [];
  for (const id of startOrder) {
    const entry = byId.get(id);
    if (!entry) continue;

    if (!entry.input && entry.inputBuffer) {
      const parsed = safeJsonParse(entry.inputBuffer);
      if (parsed && typeof parsed === 'object') entry.input = parsed;
    }

    out.push({
      name: stripMcpPrefix(entry.name),
      input: entry.input || {},
      result: entry.result || '',
      ok: Boolean(entry.ok),
      seq: entry.seq,
    });
  }

  out.sort((a, b) => a.seq - b.seq);
  return out.map(({ seq, ...rest }) => rest);
}

function extractSessionModelFromStreamJson(rawText) {
  const lines = String(rawText || '')
    .split(NL)
    .map((l) => l.trim())
    .filter(Boolean);

  for (const line of lines) {
    const event = safeJsonParse(line);
    if (!event || typeof event !== 'object') continue;

    if (event.type === 'system' && event.subtype === 'init' && event.model) {
      return String(event.model);
    }

    if (event.type === 'assistant' && event.message && event.message.model && event.message.model !== '<synthetic>') {
      return String(event.message.model);
    }
  }

  return null;
}

class EvolutionManager {
  constructor(opts) {
    const options = opts || {};

    this.skillRegistry = options.skillRegistry;
    this.learnedToolStore = options.learnedToolStore;

    this.claudeBin = options.claudeBin || process.env.CLAUDE_BIN || 'claude';
    this.model = process.env.EVOLUTION_MODEL || options.model || null;

    this.arenaDataDir = options.arenaDataDir || path.resolve(__dirname, '../../arena_data');
    this.skillsDir = options.skillsDir || path.resolve(__dirname, '../../skills');
    this.logsDir = options.logsDir || path.resolve(__dirname, '../../logs');

    this.evolutionDir = path.join(this.arenaDataDir, 'evolution');
    this.sessionsDir = path.join(this.evolutionDir, 'sessions');
    this.batchesDir = path.join(this.evolutionDir, 'batches');
    this.runsPath = path.join(this.evolutionDir, 'runs.jsonl');
    this.configPath = path.join(this.evolutionDir, 'config.json');

    const minPending = Number(process.env.EVOLUTION_BATCH_MIN_PENDING || options.batchMinPending || 1);
    this.batchMinPending = Math.max(1, Number.isFinite(minPending) ? minPending : 1);

    const repairBudget = Number(process.env.EVOLUTION_REPAIR_BUDGET || options.repairBudget || 2);
    this.repairBudget = Math.max(0, Number.isFinite(repairBudget) ? repairBudget : 2);

    const autoSweepEnv = String(process.env.EVOLUTION_AUTO_SWEEP_ENABLED || '')
      .trim()
      .toLowerCase();
    const envAutoSweepEnabled = autoSweepEnv === '1' || autoSweepEnv === 'true';
    this.autoSweepEnabled = typeof options.autoSweepEnabled === 'boolean'
      ? options.autoSweepEnabled
      : envAutoSweepEnabled;
    this.autoSweepIntervalMs = Math.max(2000, Number(options.autoSweepIntervalMs || 15000));

    ensureDir(this.arenaDataDir);
    ensureDir(this.evolutionDir);
    ensureDir(this.sessionsDir);
    ensureDir(this.batchesDir);
    ensureDir(this.skillsDir);

    if (!fs.existsSync(this.runsPath)) fs.writeFileSync(this.runsPath, '', 'utf-8');

    const envEnabled = String(process.env.EVOLUTION_ENABLED || '').trim().toLowerCase();
    const initialEnabled = envEnabled === 'false' || envEnabled === '0' ? false : true;
    const diskCfg = readJsonFileSafe(this.configPath, null);
    this.evolutionEnabled =
      diskCfg && typeof diskCfg.enabled === 'boolean' ? diskCfg.enabled : initialEnabled;
    this._persistConfig('init');

    this.queue = [];
    this.processing = false;
    this.cleanupReport = {
      lastRunAt: null,
      checkedCount: 0,
      removedCount: 0,
      removedToolIds: [],
      removedToolReasons: [],
      removedSkillCount: 0,
      removedSkillIds: [],
    };

    this._cleanupLegacyLearnedTools();

    if (this.autoSweepEnabled) {
      setTimeout(() => {
        this._scheduleAutoSweep('startup');
      }, 1200);

      this.autoSweepTimer = setInterval(() => {
        this._scheduleAutoSweep('interval');
      }, this.autoSweepIntervalMs);

      if (this.autoSweepTimer && typeof this.autoSweepTimer.unref === 'function') {
        this.autoSweepTimer.unref();
      }
    }
  }

  _persistConfig(source) {
    const payload = {
      enabled: Boolean(this.evolutionEnabled),
      updatedAt: nowIso(),
      source: String(source || 'runtime'),
    };
    writeJsonFileSafe(this.configPath, payload);
  }

  _refreshEnabledFromDisk() {
    const diskCfg = readJsonFileSafe(this.configPath, null);
    if (diskCfg && typeof diskCfg.enabled === 'boolean') {
      this.evolutionEnabled = Boolean(diskCfg.enabled);
    }
    return this.evolutionEnabled;
  }

  setEnabled(enabled, source) {
    this.evolutionEnabled = Boolean(enabled);
    this._persistConfig(source || 'api');
    if (!this.evolutionEnabled) {
      this.queue = [];
    }
    return this.getConfig();
  }

  getConfig() {
    this._refreshEnabledFromDisk();
    return {
      enabled: Boolean(this.evolutionEnabled),
      configPath: this.configPath,
      updatedAt: nowIso(),
    };
  }

  _hasQueuedBatchCheck() {
    return this.queue.some((item) => item && (item.type === 'batch_check' || item.type === 'reprocess'));
  }

  _scheduleAutoSweep(trigger) {
    try {
      if (!this._refreshEnabledFromDisk()) return;
      const pending = this._loadPendingSessionArtifacts();
      if (pending.length < this.batchMinPending) return;
      if (this._hasQueuedBatchCheck()) return;
      this.queue.push({ type: 'batch_check', trigger: trigger || 'auto_sweep' });
      this._processQueue().catch(() => {});
    } catch {
      // Best effort only; never break server due to sweep errors.
    }
  }

  _appendRunLog(entry) {
    fs.appendFileSync(this.runsPath, `${JSON.stringify(entry)}${NL}`, 'utf-8');
  }

  _saveBatchRecord(record) {
    const file = path.join(this.batchesDir, `${record.id}.json`);
    fs.writeFileSync(file, `${JSON.stringify(record, null, 2)}${NL}`, 'utf-8');
    return file;
  }

  listRuns(limit) {
    const lim = Number(limit || 20);
    try {
      const raw = fs.readFileSync(this.runsPath, 'utf-8');
      const lines = raw.split(NL).map((l) => l.trim()).filter(Boolean);
      const parsed = [];
      for (const line of lines) {
        const obj = safeJsonParse(line);
        if (obj) parsed.push(obj);
      }
      return parsed.slice(-lim).reverse();
    } catch {
      return [];
    }
  }

  listPending(limit) {
    const lim = Number(limit || 100);
    return this._loadPendingSessionArtifacts()
      .slice(0, lim)
      .map((s) => ({
        id: s.id,
        source: s.source,
        prompt: trimText(s.prompt, 220),
        createdAt: s.createdAt,
        state: s.state,
        batchId: s.batchId || null,
        toolTraceCount: Array.isArray(s.toolTrace) ? s.toolTrace.length : 0,
        motif: null,
        pipelineStatus: s.pipeline && s.pipeline.status ? s.pipeline.status : 'queued',
        unresolvedSubparts: Array.isArray(s.pipeline && s.pipeline.unresolvedSubparts)
          ? s.pipeline.unresolvedSubparts.length
          : 0,
        result: {
          isError: Boolean(s.result && s.result.isError),
          screenshot: s.result && s.result.screenshot ? s.result.screenshot : null,
        },
      }));
  }

  getStatus(limitPending) {
    const enabled = this._refreshEnabledFromDisk();
    const pending = this.listPending(limitPending || 100);
    const queuedByType = {};
    for (const item of this.queue) {
      const type = item && item.type ? String(item.type) : 'unknown';
      queuedByType[type] = (queuedByType[type] || 0) + 1;
    }

    return {
      processing: Boolean(this.processing),
      queueLength: this.queue.length,
      queuedByType,
      pendingCount: pending.length,
      pending,
      batchMinPending: this.batchMinPending,
      repairBudget: this.repairBudget,
      autoSweepEnabled: this.autoSweepEnabled,
      enabled,
      cleanup: { ...this.cleanupReport },
      timestamp: nowIso(),
    };
  }

  _normalizeSessionArtifact(artifact) {
    const base = artifact || {};
    const pipeline = base.pipeline && typeof base.pipeline === 'object' ? base.pipeline : {};

    return {
      id: base.id || `session_${Date.now().toString(36)}_${Math.random().toString(36).slice(2, 8)}`,
      source: base.source || 'chat',
      prompt: String(base.prompt || ''),
      model: base.model ? String(base.model) : null,
      skills: Array.isArray(base.skills) ? uniqueStrings(base.skills) : [],
      toolTrace: Array.isArray(base.toolTrace) ? base.toolTrace : [],
      result: {
        isError: Boolean(base.result && base.result.isError),
        screenshot: base.result && base.result.screenshot ? base.result.screenshot : null,
      },
      createdAt: base.createdAt || nowIso(),
      state:
        base.state === SESSION_ARCHIVED
          ? SESSION_ARCHIVED
          : base.state === SESSION_FAILED_UNRESOLVED
            ? SESSION_FAILED_UNRESOLVED
            : SESSION_PENDING,
      batchId: base.batchId || null,
      archivedAt: base.archivedAt || null,
      archiveReason: base.archiveReason || null,
      sessionRef: base.sessionRef || null,
      pipeline: {
        status: String(pipeline.status || 'queued'),
        attempts: Number(pipeline.attempts || 0),
        selectedSkills: Array.isArray(pipeline.selectedSkills) ? uniqueStrings(pipeline.selectedSkills) : [],
        subparts: Array.isArray(pipeline.subparts) ? pipeline.subparts : [],
        unresolvedSubparts: Array.isArray(pipeline.unresolvedSubparts) ? pipeline.unresolvedSubparts : [],
        matchedToolIds: Array.isArray(pipeline.matchedToolIds) ? uniqueStrings(pipeline.matchedToolIds) : [],
        createdToolIds: Array.isArray(pipeline.createdToolIds) ? uniqueStrings(pipeline.createdToolIds) : [],
        createdSkillIds: Array.isArray(pipeline.createdSkillIds) ? uniqueStrings(pipeline.createdSkillIds) : [],
        lastRunAt: pipeline.lastRunAt || null,
        lastError: pipeline.lastError || null,
        llmDebug: pipeline.llmDebug || null,
      },
    };
  }

  _saveSessionArtifact(artifact) {
    const normalized = this._normalizeSessionArtifact(artifact);
    const file = path.join(this.sessionsDir, `${normalized.id}.json`);
    fs.writeFileSync(file, `${JSON.stringify(normalized, null, 2)}${NL}`, 'utf-8');
    return normalized;
  }

  _loadSessionArtifact(sessionId) {
    const file = path.join(this.sessionsDir, `${sessionId}.json`);
    if (!fs.existsSync(file)) return null;
    const parsed = safeJsonParse(fs.readFileSync(file, 'utf-8'));
    if (!parsed) return null;
    return this._normalizeSessionArtifact(parsed);
  }

  _patchSessionArtifact(sessionId, patch) {
    const current = this._loadSessionArtifact(sessionId);
    if (!current) return null;
    const next = this._normalizeSessionArtifact({
      ...current,
      ...(patch || {}),
      pipeline: {
        ...(current.pipeline || {}),
        ...((patch && patch.pipeline) || {}),
      },
    });
    const file = path.join(this.sessionsDir, `${next.id}.json`);
    fs.writeFileSync(file, `${JSON.stringify(next, null, 2)}${NL}`, 'utf-8');
    return next;
  }

  _loadPendingSessionArtifacts() {
    const files = fs
      .readdirSync(this.sessionsDir)
      .filter((f) => f.endsWith('.json'))
      .map((f) => path.join(this.sessionsDir, f));

    const pending = [];
    for (const f of files) {
      const parsed = safeJsonParse(fs.readFileSync(f, 'utf-8'));
      if (!parsed) continue;

      const normalized = this._normalizeSessionArtifact(parsed);
      if (normalized.state !== SESSION_PENDING) continue;
      if (normalized.result && normalized.result.isError) continue;
      pending.push(normalized);
    }

    pending.sort((a, b) => Date.parse(a.createdAt || 0) - Date.parse(b.createdAt || 0));
    return pending;
  }

  _archiveSessions(sessionIds, batchId, reason) {
    const archived = [];
    for (const id of uniqueStrings(sessionIds)) {
      const next = this._patchSessionArtifact(id, {
        state: SESSION_ARCHIVED,
        batchId,
        archivedAt: nowIso(),
        archiveReason: reason || 'all_subparts_resolved',
        pipeline: {
          status: 'completed',
          lastRunAt: nowIso(),
        },
      });
      if (next) archived.push(next.id);
    }
    return archived;
  }

  _findLatestPendingByPrompt(prompt) {
    const target = String(prompt || '').trim();
    if (!target) return null;
    const pending = this._loadPendingSessionArtifacts();
    for (let i = pending.length - 1; i >= 0; i -= 1) {
      const p = pending[i];
      if (String(p.prompt || '').trim() === target) return p;
    }
    return null;
  }

  queueSessionArtifact(artifact, trigger) {
    if (!this._refreshEnabledFromDisk()) return null;
    const normalized = this._saveSessionArtifact(artifact);
    if (!this._hasQueuedBatchCheck()) {
      this.queue.push({ type: 'batch_check', trigger: trigger || 'auto', sessionIds: [normalized.id] });
    }
    this._processQueue().catch(() => {});
    return normalized;
  }

  queueFromPrompt(payload) {
    if (!this._refreshEnabledFromDisk()) return null;
    const safe = payload || {};
    const artifactId = `prompt_${Date.now().toString(36)}_${Math.random().toString(36).slice(2, 8)}`;
    const normalized = this._saveSessionArtifact({
      id: artifactId,
      sessionRef: safe.sessionId || null,
      source: safe.source || 'prompt',
      prompt: safe.prompt,
      model: safe.model || null,
      skills: safe.skills || [],
      toolTrace: Array.isArray(safe.toolTrace) ? safe.toolTrace : [],
      result: safe.result || { isError: false, screenshot: null },
      createdAt: nowIso(),
      state: SESSION_PENDING,
      pipeline: {
        status: 'queued',
        attempts: 0,
      },
    });

    if (!this._hasQueuedBatchCheck()) {
      this.queue.push({ type: 'batch_check', trigger: safe.trigger || 'prompt_received', sessionIds: [normalized.id] });
    }
    this._processQueue().catch(() => {});
    return normalized;
  }

  queueFromChat(payload) {
    if (!this._refreshEnabledFromDisk()) return { queued: false, disabled: true };
    const safe = payload || {};

    setImmediate(() => {
      try {
        const rawPath = safe.rawLogPath || path.join(this.logsDir, 'raw_latest.jsonl');
        let raw = '';
        try {
          raw = fs.readFileSync(rawPath, 'utf-8');
        } catch {
          raw = '';
        }

        const toolTrace = extractToolTraceFromStreamJson(raw);
        const model = safe.model || extractSessionModelFromStreamJson(raw);

        const targetId = safe.evolutionSessionId ? String(safe.evolutionSessionId) : '';
        if (targetId) {
          const patched = this._patchSessionArtifact(targetId, {
            sessionRef: safe.sessionId || null,
            prompt: safe.prompt || undefined,
            model: model || null,
            skills: safe.skills || [],
            toolTrace,
            result: safe.result || { isError: false, screenshot: null },
          });
          if (patched) return;
        }

        let existing = null;
        if (safe.prompt) {
          existing = this._findLatestPendingByPrompt(safe.prompt);
        }

        if (existing) {
          this._patchSessionArtifact(existing.id, {
            sessionRef: safe.sessionId || existing.sessionRef || null,
            model: model || existing.model || null,
            skills: safe.skills || existing.skills || [],
            toolTrace: toolTrace.length > 0 ? toolTrace : existing.toolTrace,
            result: safe.result || existing.result,
          });
          return;
        }

        const artifactId = `chat_${Date.now().toString(36)}_${Math.random().toString(36).slice(2, 8)}`;
        this.queueSessionArtifact(
          {
            id: artifactId,
            sessionRef: safe.sessionId || null,
            source: 'chat',
            prompt: safe.prompt,
            model,
            skills: safe.skills || [],
            toolTrace,
            result: safe.result || { isError: false, screenshot: null },
            createdAt: nowIso(),
            state: SESSION_PENDING,
          },
          'chat_done'
        );
      } catch {
        // Best-effort background enqueue: swallow failures to avoid impacting live SSE requests.
      }
    });

    return { queued: true };
  }

  reprocess(request) {
    return {
      sessionIds: [],
      disabled: true,
      reason: 'strict_one_pass_mode',
    };
  }

  async _processQueue() {
    if (this.processing) return;
    this.processing = true;

    while (this.queue.length > 0) {
      const item = this.queue.shift();

      const runLog = {
        runId: crypto.randomUUID(),
        mode: 'pipeline',
        source: 'evolution',
        trigger: item && item.trigger ? item.trigger : 'auto',
        batchId: null,
        sessionId: null,
        sessionIds: [],
        startedAt: nowIso(),
        finishedAt: null,
        status: 'running',
        skill: { promoted: false, reason: 'pending' },
        tool: { promoted: false, reason: 'pending' },
      };

      try {
        const result = await this._runBatch(item);
        runLog.batchId = result.batchId || null;
        runLog.sessionIds = result.sessionIds || [];
        runLog.sessionId = runLog.sessionIds[0] || null;
        runLog.status = result.status || 'completed';
        runLog.skill = result.skill || { promoted: false, reason: 'none' };
        runLog.tool = result.tool || { promoted: false, reason: 'none' };
        runLog.createdSkillIds = result.createdSkillIds || [];
        runLog.updatedSkillIds = [];
        runLog.reusedSkillIds = result.reusedSkillIds || [];
        runLog.createdToolIds = result.createdToolIds || [];
        runLog.updatedToolIds = [];
        runLog.reusedToolIds = result.reusedToolIds || [];
        runLog.archivedSessionIds = result.archivedSessionIds || [];
        runLog.deferredSessionIds = result.deferredSessionIds || [];
        runLog.matchedCount = Number(result.matchedCount || 0);
        runLog.constructedCount = Number(result.constructedCount || 0);
        runLog.repairedCount = Number(result.repairedCount || 0);
        runLog.skippedCount = Number(result.skippedCount || 0);
        runLog.llmDebug = result.llmDebug || null;
        if (result.reason) runLog.reason = result.reason;
      } catch (err) {
        runLog.status = 'error';
        runLog.error = err && err.message ? err.message : String(err);
        runLog.skill = { promoted: false, reason: 'error' };
        runLog.tool = { promoted: false, reason: 'error' };
      }

      runLog.finishedAt = nowIso();
      this._appendRunLog(runLog);
    }

    this.processing = false;
  }

  _activeLearnedSkillsFull() {
    const metas = this.skillRegistry.list();
    const learned = metas
      .filter((s) => s.source === 'custom' && Array.isArray(s.tags) && s.tags.includes('learned'))
      .map((s) => this.skillRegistry.get(s.id))
      .filter(Boolean);

    return learned.map((s) => ({
      id: s.id,
      name: s.name,
      description: s.description,
      version: s.version,
      tags: s.tags || [],
      dependencies: s.dependencies || [],
      content: s.content || '',
      source: s.source,
      filePath: s.filePath,
    }));
  }

  _activeLearnedToolsFull() {
    return this.learnedToolStore.getEnabled().map((t) => ({ ...t }));
  }

  _cleanupLegacyLearnedTools() {
    const report = {
      lastRunAt: nowIso(),
      checkedCount: 0,
      removedCount: 0,
      removedToolIds: [],
      removedToolReasons: [],
      removedSkillCount: 0,
      removedSkillIds: [],
    };

    try {
      const pruneReport = this.learnedToolStore.pruneInvalid({ includeArchived: true });
      report.checkedCount = Number(pruneReport && pruneReport.checkedCount ? pruneReport.checkedCount : 0);
      report.removedCount = Number(pruneReport && pruneReport.removedCount ? pruneReport.removedCount : 0);
      report.removedToolIds = uniqueStrings(pruneReport && pruneReport.removedToolIds ? pruneReport.removedToolIds : []);
      report.removedToolReasons = Array.isArray(pruneReport && pruneReport.removedToolReasons)
        ? pruneReport.removedToolReasons.map((item) => ({
          id: String(item && item.id ? item.id : ''),
          reason: String(item && item.reason ? item.reason : 'invalid_tool'),
        })).filter((item) => item.id)
        : [];

      if (report.removedToolIds.length > 0) {
        const removedToolSet = new Set(report.removedToolIds);
        const directLinkedSkillIds = new Set(report.removedToolIds.map((id) => `${id}_skill`));
        const metas = Array.isArray(this.skillRegistry.list()) ? this.skillRegistry.list() : [];
        const removedSkillIds = [];

        for (const meta of metas) {
          const skillId = String(meta && meta.id ? meta.id : '').trim();
          if (!skillId) continue;

          const skill = this.skillRegistry.get(skillId);
          if (!skill || skill.source !== 'custom') continue;

          const tags = Array.isArray(skill.tags) ? skill.tags : [];
          const isLearnedSkill = tags.includes('learned');
          const refs = extractLearnedToolRefs(skill.content || '');
          const refsOnlyRemoved = refs.size > 0 && [...refs].every((id) => removedToolSet.has(id));
          const directLinked = directLinkedSkillIds.has(skillId);

          if (!(directLinked || (isLearnedSkill && refsOnlyRemoved))) continue;
          if (!skill.filePath || !fs.existsSync(skill.filePath)) continue;

          try {
            fs.unlinkSync(skill.filePath);
            removedSkillIds.push(skillId);
          } catch {
            // best effort cleanup
          }
        }

        if (removedSkillIds.length > 0) {
          this.skillRegistry.reload();
          report.removedSkillIds = uniqueStrings(removedSkillIds);
          report.removedSkillCount = report.removedSkillIds.length;
        }
      }
    } catch {
      // best-effort cleanup
    }

    this.cleanupReport = report;
    if (report.removedCount > 0) {
      this._appendRunLog({
        type: 'cleanup_invalid_tools',
        timestamp: report.lastRunAt,
        checkedCount: report.checkedCount,
        removedCount: report.removedCount,
        removedToolIds: report.removedToolIds,
        removedToolReasons: report.removedToolReasons,
        removedSkillCount: report.removedSkillCount,
        removedSkillIds: report.removedSkillIds,
      });
    }
  }

  _extractCandidateJson(text) {
    const cleaned = String(text || '').trim();
    if (!cleaned) return null;

    const direct = safeJsonParse(cleaned);
    if (direct && typeof direct === 'object') return direct;

    const start = cleaned.indexOf('{');
    const end = cleaned.lastIndexOf('}');
    if (start >= 0 && end > start) {
      const maybe = safeJsonParse(cleaned.slice(start, end + 1));
      if (maybe && typeof maybe === 'object') return maybe;
    }

    const fenceMatches = [...cleaned.matchAll(/```(?:json)?\s*([\s\S]*?)\s*```/gi)];
    for (const m of fenceMatches) {
      const candidate = safeJsonParse(String(m[1] || '').trim());
      if (candidate && typeof candidate === 'object') return candidate;
    }

    const objects = [];
    let depth = 0;
    let inString = false;
    let escape = false;
    let objStart = -1;

    for (let i = 0; i < cleaned.length; i += 1) {
      const ch = cleaned[i];

      if (inString) {
        if (escape) {
          escape = false;
          continue;
        }
        if (ch === '\\') {
          escape = true;
          continue;
        }
        if (ch === '"') {
          inString = false;
        }
        continue;
      }

      if (ch === '"') {
        inString = true;
        continue;
      }

      if (ch === '{') {
        if (depth === 0) objStart = i;
        depth += 1;
        continue;
      }

      if (ch === '}') {
        if (depth > 0) depth -= 1;
        if (depth === 0 && objStart >= 0) {
          objects.push(cleaned.slice(objStart, i + 1));
          objStart = -1;
        }
      }
    }

    for (let i = objects.length - 1; i >= 0; i -= 1) {
      const candidate = safeJsonParse(objects[i]);
      if (candidate && typeof candidate === 'object') return candidate;
    }

    return null;
  }

  async _invokeClaudeJson(systemPrompt, payload, modelHint) {
    const userText = JSON.stringify(payload, null, 2);

    const args = [
      '--input-format',
      'stream-json',
      '--output-format',
      'stream-json',
      '--verbose',
      '--dangerously-skip-permissions',
      '--append-system-prompt',
      String(systemPrompt || ''),
    ];

    const selectedModel = process.env.EVOLUTION_MODEL || modelHint || this.model || null;
    if (selectedModel) args.push('--model', selectedModel);

    return new Promise((resolve) => {
      let buffer = '';
      let acc = '';
      let stderr = '';
      let parseError = null;
      let finalized = false;

      const proc = spawn(this.claudeBin, args, {
        cwd: path.resolve(__dirname, '..'),
        env: process.env,
        stdio: ['pipe', 'pipe', 'pipe'],
      });

      proc.stdout.on('data', (chunk) => {
        buffer += chunk.toString();
        const lines = buffer.split(NL);
        buffer = lines.pop() || '';

        for (const line of lines) {
          const ev = safeJsonParse(line);
          if (!ev) continue;

          if (ev.type === 'assistant') {
            const content = ev.message && Array.isArray(ev.message.content) ? ev.message.content : [];
            for (const block of content) {
              if (block.type === 'text' && block.text) acc += block.text;
            }
          } else if (ev.type === 'stream_event') {
            const se = ev.event || {};
            if (se.type === 'content_block_delta' && se.delta && se.delta.type === 'text_delta') {
              acc += se.delta.text || '';
            }
          } else if (ev.type === 'result' && typeof ev.result === 'string') {
            acc += `${NL}${ev.result}`;
          }
        }
      });

      proc.stderr.on('data', (chunk) => {
        stderr += chunk.toString();
      });

      proc.on('error', (err) => {
        if (finalized) return;
        finalized = true;
        resolve({
          parsed: null,
          debug: {
            selectedModel: selectedModel || null,
            args,
            outputText: trimText(acc, 3000),
            outputLength: acc.length,
            parseOk: false,
            parseError: `spawn_error:${err && err.message ? err.message : String(err)}`,
            stderrText: trimText(stderr, 3000),
            stderrLength: stderr.length,
            exitCode: null,
            signal: null,
            hadOutput: acc.trim().length > 0,
          },
        });
      });

      proc.on('close', (code, signal) => {
        if (finalized) return;
        finalized = true;

        let parsed = null;
        try {
          parsed = this._extractCandidateJson(acc);
          if (!parsed) parseError = 'no_json_object_found';
        } catch (err) {
          parseError = err && err.message ? err.message : String(err);
        }

        resolve({
          parsed,
          debug: {
            selectedModel: selectedModel || null,
            args,
            outputText: trimText(acc, 3000),
            outputLength: acc.length,
            parseOk: Boolean(parsed),
            parseError: parseError || null,
            stderrText: trimText(stderr, 3000),
            stderrLength: stderr.length,
            exitCode: Number.isInteger(code) ? code : null,
            signal: signal || null,
            hadOutput: acc.trim().length > 0,
          },
        });
      });

      const inputEvent = {
        type: 'user',
        message: {
          role: 'user',
          content: [{ type: 'text', text: userText }],
        },
      };

      proc.stdin.write(`${JSON.stringify(inputEvent)}${NL}`);
      proc.stdin.end();
    });
  }

  _selectSkillsForPrompt(prompt, carriedSkills) {
    const base = uniqueStrings(Array.isArray(carriedSkills) ? carriedSkills : []);
    const retrieved = this.skillRegistry.retrieveForPrompt(prompt, {
      limit: 5,
      threshold: 0.12,
      onlyLearned: false,
    });

    for (const id of retrieved || []) {
      if (!base.includes(id)) base.push(id);
    }

    return base;
  }

  async _decomposePrompt(prompt, selectedSkills, modelHint) {
    const systemPrompt = [
      'You are decomposing a scene-generation prompt into reusable subparts for tool construction.',
      'Return STRICT JSON only.',
      'Schema:',
      '{"summary":"string","subparts":[{"id":"string","text":"string"}]}',
      'Rules:',
      '- 1 to 3 subparts.',
      '- Each subpart should be a reusable scene-building operation, not a one-off coordinate.',
      '- Keep each subpart concise and implementation-oriented.',
      '- Use generalized, reusable operation wording and avoid instance-specific phrasing.',
    ].join(NL);

    const payload = {
      prompt: String(prompt || ''),
      selectedSkills: selectedSkills || [],
    };

    const response = await this._invokeClaudeJson(systemPrompt, payload, modelHint);
    const parsed = response && response.parsed && typeof response.parsed === 'object' ? response.parsed : {};

    let subparts = [];
    if (Array.isArray(parsed.subparts)) {
      for (const p of parsed.subparts) {
        if (!p || typeof p !== 'object') continue;
        const id = slugify(p.id || p.text || `subpart_${subparts.length + 1}`) || `subpart_${subparts.length + 1}`;
        const text = String(p.text || '').trim();
        if (!text) continue;
        subparts.push({ id, text });
      }
    }

    if (subparts.length === 0) {
      subparts = [{ id: 'subpart_1', text: String(prompt || '').trim() }].filter((s) => s.text);
    }

    const seen = new Set();
    subparts = subparts.filter((s) => {
      if (!s || !s.text) return false;
      const key = s.text.toLowerCase();
      if (seen.has(key)) return false;
      seen.add(key);
      return true;
    }).slice(0, 8);

    return {
      summary: String(parsed.summary || '').trim(),
      subparts,
      debug: response.debug,
    };
  }

  _retrieveDynamicCandidates(dynamicTools, subpartText, limit) {
    const tools = Array.isArray(dynamicTools) ? dynamicTools : [];
    const lim = Math.max(1, Number(limit || 8));

    const qTokens = new Set(tokenize(subpartText));

    const scored = tools.map((tool) => {
      const hay = [
        tool.id,
        tool.name,
        tool.description,
        Array.isArray(tool.program) ? tool.program.map((step) => step && step.primitive).join(' ') : '',
        ...(Array.isArray(tool.tags) ? tool.tags : []),
      ].join(' ');

      const tTokens = new Set(tokenize(hay));
      let overlap = 0;
      for (const tok of qTokens) {
        if (tTokens.has(tok)) overlap += 1;
      }

      const lexical = qTokens.size + tTokens.size - overlap > 0
        ? overlap / (qTokens.size + tTokens.size - overlap)
        : 0;

      const score = lexical;

      return {
        id: tool.id,
        name: tool.name,
        description: tool.description,
        program: Array.isArray(tool.program) ? tool.program : [],
        defaults: tool.defaults || {},
        paramsSchema: tool.paramsSchema || {},
        score: Number(score.toFixed(4)),
      };
    });

    scored.sort((a, b) => b.score - a.score);
    return scored.slice(0, lim);
  }

  async _decideSubpart(session, subpart, candidates, selectedSkills, modelHint) {
    const systemPrompt = [
      'You decide whether a prompt subpart should reuse an existing dynamic tool or construct a new tool.',
      'Return STRICT JSON only with this schema:',
      '{"action":"match|construct","reason":"string","matchToolId":"string?"}',
      'Rules:',
      '- Action "match" must choose a tool id from candidates exactly.',
      '- Choose "construct" when no candidate provides a strong semantic match.',
      '- This phase outputs decision JSON only.',
    ].join(NL);

    const payload = {
      prompt: session.prompt,
      selectedSkills,
      subpart,
      dynamicCandidates: candidates,
      repairBudget: this.repairBudget,
    };

    const response = await this._invokeClaudeJson(systemPrompt, payload, modelHint);
    const parsed = response && response.parsed && typeof response.parsed === 'object' ? response.parsed : {};

    let action = String(parsed.action || '').trim();
    if (action !== 'match' && action !== 'construct') action = 'construct';

    const matchToolId = parsed.matchToolId != null ? String(parsed.matchToolId).trim() : '';

    return {
      action,
      reason: String(parsed.reason || '').trim(),
      matchToolId,
      candidateTool: null,
      debug: response.debug,
    };
  }

  async _constructGeneralTool(session, subpart, selectedSkills, modelHint) {
    const systemPrompt = [
      'Construct a new reusable dynamic tool for a scene-generation subpart.',
      'Return STRICT JSON only as a tool object (no wrapper):',
      '{"id":"string","name":"string","description":"string","paramsSchema":object,"defaults":object,"program":[{"primitive":"string","argumentsTemplate":object,"when":"optional"}]}',
      'Rules:',
      '- id must be snake_case and NOT start with learned__.',
      '- program must be an ordered list of primitive steps.',
      `- Every program[].primitive must be one of ${[...ALLOWED_PRIMITIVES].join('|')}.`,
      '- Every program step must include argumentsTemplate.',
      '- Primitive argument contracts are strict and must be respected in argumentsTemplate:',
      '- spawn_blueprint_actor: actor_name, blueprint_id, location (optional: rotation, scale).',
      '- spawn_actor: name, static_mesh, location (optional: rotation, scale).',
      '- set_actor_transform: name (optional: location, rotation, scale).',
      '- delete_actor: name.',
      '- take_screenshot: filename (optional).',
      '- For BP_* / CityDatabase blueprint assets (e.g. BP_Tree1..BP_Tree6, BP_Building_*), use spawn_blueprint_actor with blueprint_id.',
      '- Use spawn_actor only for true static mesh paths (e.g. /Game/.../SM_*.SM_*).',
      '- Use only supported ops with key "op": coalesce, add, mul, vec_add, vec_scale, concat.',
      '- Expression syntax must be JSON-template only: use { "op": "...", ... } and plain refs like "$args.x".',
      '- Do NOT use function-call strings or code-like expressions (for example concat(...), $expr, ${...}, "x + y").',
      '- For vectors, use array/vector-template forms; do NOT use object coordinate literals like {x:..., y:..., z:...}.',
      '- Canonical placeholder example (shape only, replace placeholders with your semantics):',
      '- {"primitive":"spawn_blueprint_actor","argumentsTemplate":{"actor_name":{"op":"concat","values":["$args.<name_prefix>","_","$index1"]},"blueprint_id":"$args.<blueprint_id>","location":["$args.<x>","$args.<y>","$args.<z>"],"rotation":[0,"$args.<yaw>",0],"scale":["$args.<scale>","$args.<scale>","$args.<scale>"]}}',
      '- Canonical vector-math placeholder example:',
      '- {"primitive":"spawn_blueprint_actor","argumentsTemplate":{"actor_name":{"op":"concat","values":["$args.<prefix>","_","$index1"]},"blueprint_id":"$args.<blueprint_id>","location":[{"op":"add","values":["$args.<base_x>",{"op":"mul","values":["$index","$args.<step_x>"]}]},{"op":"add","values":["$args.<base_y>",{"op":"mul","values":["$index","$args.<step_y>"]}]},"$args.<z>"]}}',
      '- Keep placeholder style generic (use <...> concepts), but preserve this exact JSON-template structure.',
      '- Do not invent alias keys (for example asset/x/y/z/label/count) for primitive calls.',
      '- paramsSchema must be a valid JSON schema object with top-level type "object", properties object, and optional required string array.',
      '- Use references like $args.*, $defaults.*, $index with supported op objects for generalized reuse.',
      '- Maximize reusable generality: prefer broader parameterization and composability over narrow one-instance behavior.',
      '- Prefer parameterized defaults over fixed one-off coordinates.',
      '- Keep semantic intent faithful to the subpart while designing for future prompts.',
    ].join(NL);

    const payload = {
      prompt: session.prompt,
      selectedSkills,
      subpart,
      existingDynamicTools: this._activeLearnedToolsFull().map((t) => ({
        id: t.id,
        programPrimitives: Array.isArray(t.program) ? t.program.map((step) => step && step.primitive) : [],
      })),
    };

    const response = await this._invokeClaudeJson(systemPrompt, payload, modelHint);
    const parsed = response && response.parsed && typeof response.parsed === 'object' ? response.parsed : null;

    return {
      candidateTool: parsed,
      debug: response.debug,
    };
  }

  async _repairTool(candidateTool, errorReason, session, subpart, selectedSkills, modelHint) {
    const systemPrompt = [
      'Repair a dynamic tool candidate that failed mechanical validation.',
      'Return STRICT JSON only as the fully corrected tool object:',
      '{"id":"string","name":"string","description":"string","paramsSchema":object,"defaults":object,"program":[{"primitive":"string","argumentsTemplate":object,"when":"optional"}]}',
      'Rules:',
      '- Fix ALL reported failures.',
      '- Keep semantic intent for the same subpart.',
      '- id must not start with learned__.',
      '- program must remain generalized and reusable.',
      '- paramsSchema must be a valid JSON schema object with top-level type "object", properties object, and optional required string array.',
      `- Every program[].primitive must be one of ${[...ALLOWED_PRIMITIVES].join('|')}.`,
      '- Primitive argument contracts are strict and must be respected in argumentsTemplate:',
      '- spawn_blueprint_actor: actor_name, blueprint_id, location (optional: rotation, scale).',
      '- spawn_actor: name, static_mesh, location (optional: rotation, scale).',
      '- set_actor_transform: name (optional: location, rotation, scale).',
      '- delete_actor: name.',
      '- take_screenshot: filename (optional).',
      '- For BP_* / CityDatabase blueprint assets (e.g. BP_Tree1..BP_Tree6, BP_Building_*), use spawn_blueprint_actor with blueprint_id.',
      '- Use spawn_actor only for true static mesh paths (e.g. /Game/.../SM_*.SM_*).',
      '- Use only supported ops with key "op": coalesce, add, mul, vec_add, vec_scale, concat.',
      '- Expression syntax must be JSON-template only: use { "op": "...", ... } and plain refs like "$args.x".',
      '- Do NOT use function-call strings or code-like expressions (for example concat(...), $expr, ${...}, "x + y").',
      '- For vectors, use array/vector-template forms; do NOT use object coordinate literals like {x:..., y:..., z:...}.',
      '- Canonical placeholder example (shape only, replace placeholders with your semantics):',
      '- {"primitive":"spawn_blueprint_actor","argumentsTemplate":{"actor_name":{"op":"concat","values":["$args.<name_prefix>","_","$index1"]},"blueprint_id":"$args.<blueprint_id>","location":["$args.<x>","$args.<y>","$args.<z>"],"rotation":[0,"$args.<yaw>",0],"scale":["$args.<scale>","$args.<scale>","$args.<scale>"]}}',
      '- Canonical vector-math placeholder example:',
      '- {"primitive":"spawn_blueprint_actor","argumentsTemplate":{"actor_name":{"op":"concat","values":["$args.<prefix>","_","$index1"]},"blueprint_id":"$args.<blueprint_id>","location":[{"op":"add","values":["$args.<base_x>",{"op":"mul","values":["$index","$args.<step_x>"]}]},{"op":"add","values":["$args.<base_y>",{"op":"mul","values":["$index","$args.<step_y>"]}]},"$args.<z>"]}}',
      '- Keep placeholder style generic (use <...> concepts), but preserve this exact JSON-template structure.',
      '- Replace any alias keys (for example asset/x/y/z/label/count) with valid primitive argument keys.',
      '- Improve generality when possible while preserving correctness.',
    ].join(NL);

    const payload = {
      prompt: session.prompt,
      selectedSkills,
      subpart,
      failedTool: candidateTool,
      validationError: errorReason,
    };

    const response = await this._invokeClaudeJson(systemPrompt, payload, modelHint);
    const parsed = response && response.parsed && typeof response.parsed === 'object' ? response.parsed : null;

    return {
      candidateTool: parsed,
      debug: response.debug,
    };
  }

  _validateCreateSkill(candidate) {
    if (!candidate || typeof candidate !== 'object') return { ok: false, reason: 'missing_skill' };

    const id = slugify(candidate.id || candidate.name || '');
    if (!id) return { ok: false, reason: 'missing_id' };
    if (this.skillRegistry.get(id)) return { ok: false, reason: 'duplicate_id' };

    const content = String(candidate.content || '').trim();
    if (!content) return { ok: false, reason: 'missing_content' };

    return {
      ok: true,
      candidate: {
        id,
        name: String(candidate.name || `Learned ${id}`).trim(),
        description: String(candidate.description || `Learned skill ${id}`).trim(),
        tags: uniqueStrings(['learned', ...(Array.isArray(candidate.tags) ? candidate.tags : [])]),
        content,
        sourceSessionIds: uniqueStrings(candidate.sourceSessionIds || []),
      },
    };
  }

  _validateCreateTool(candidate) {
    if (!candidate || typeof candidate !== 'object') return { ok: false, reason: 'missing_tool' };

    const id = slugify(candidate.id || candidate.name || '');
    if (!id) return { ok: false, reason: 'missing_id' };
    if (id.startsWith('learned__')) return { ok: false, reason: 'id_prefix_forbidden' };

    const existing = this.learnedToolStore.list({ includeArchived: true });
    if (existing.some((t) => t.id === id || t.mcpName === `learned__${id}`)) {
      return { ok: false, reason: 'duplicate_id' };
    }

    const program = Array.isArray(candidate.program) ? candidate.program : [];
    if (program.length === 0) {
      return { ok: false, reason: 'program_missing' };
    }

    if (candidate.paramsSchema != null) {
      const schemaErrors = validateParamsSchemaShape(candidate.paramsSchema, []);
      if (schemaErrors.length > 0) {
        return { ok: false, reason: `params_schema_invalid:${schemaErrors.join(';')}` };
      }
    }

    const tool = {
      ...candidate,
      id,
      mcpName: `learned__${id}`,
      enabled: candidate.enabled !== false,
      version: Number(candidate.version || 1),
      paramsSchema: candidate.paramsSchema && typeof candidate.paramsSchema === 'object'
        ? candidate.paramsSchema
        : { type: 'object', properties: {} },
      defaults: candidate.defaults || {},
      program: program.map((step) => ({ ...(step || {}) })),
      metrics: {
        estimatedCallSavings: Number(candidate.metrics && candidate.metrics.estimatedCallSavings != null
          ? candidate.metrics.estimatedCallSavings
          : 0),
        usageCount: Number(candidate.metrics && candidate.metrics.usageCount ? candidate.metrics.usageCount : 0),
        successCount: Number(candidate.metrics && candidate.metrics.successCount ? candidate.metrics.successCount : 0),
      },
      sourceSessionIds: uniqueStrings(candidate.sourceSessionIds || []),
    };

    try {
      validateToolShape(tool);
      buildDynamicToolDef(tool);
      dryRunExpansion(tool, tool.defaults);
    } catch (err) {
      return { ok: false, reason: `schema:${err.message}` };
    }

    return { ok: true, candidate: tool };
  }

  _validatePersistedTool(tool) {
    if (!tool || typeof tool !== 'object') return { ok: false, reason: 'missing_persisted_tool' };
    try {
      validateToolShape(tool);
      buildDynamicToolDef(tool);
      dryRunExpansion(tool, tool.defaults);
    } catch (err) {
      return {
        ok: false,
        reason: `post_persist_schema:${err && err.message ? err.message : String(err)}`,
      };
    }
    return { ok: true };
  }

  _readSkillRecord(id) {
    const skill = this.skillRegistry.get(id);
    if (!skill || !skill.filePath || !fs.existsSync(skill.filePath)) return null;

    const parsed = parseFrontmatter(fs.readFileSync(skill.filePath, 'utf-8'));
    const meta = parsed.meta || {};

    return {
      id: skill.id,
      filePath: skill.filePath,
      name: skill.name,
      description: skill.description,
      version: skill.version || meta.version || '1.0.0',
      tags: Array.isArray(skill.tags) ? skill.tags : [],
      dependencies: Array.isArray(skill.dependencies) ? skill.dependencies : [],
      content: skill.content || parsed.content || '',
      createdByBatchId: String(meta.createdByBatchId || ''),
      updatedByBatchIds: uniqueStrings(meta.updatedByBatchIds || []),
      sourceSessionIds: uniqueStrings(meta.sourceSessionIds || []),
      reusedByBatchIds: uniqueStrings(meta.reusedByBatchIds || []),
      provenanceSession: String(meta.provenance_session || ''),
    };
  }

  _writeSkillRecord(record) {
    const text = [
      '---',
      `id: ${yamlScalar(record.id)}`,
      `name: ${yamlScalar(record.name)}`,
      `version: ${yamlScalar(record.version || '1.0.0')}`,
      'author: "evolution-engine"',
      `tags: ${yamlArray(uniqueStrings(record.tags || []))}`,
      `dependencies: ${yamlArray(uniqueStrings(record.dependencies || []))}`,
      `description: ${yamlScalar(record.description || '')}`,
      `createdByBatchId: ${yamlScalar(record.createdByBatchId || '')}`,
      `updatedByBatchIds: ${yamlArray(uniqueStrings(record.updatedByBatchIds || []))}`,
      `sourceSessionIds: ${yamlArray(uniqueStrings(record.sourceSessionIds || []))}`,
      `reusedByBatchIds: ${yamlArray(uniqueStrings(record.reusedByBatchIds || []))}`,
      `provenance_session: ${yamlScalar(record.provenanceSession || '')}`,
      '---',
      '',
      String(record.content || '').trim(),
      '',
    ].join(NL);

    const filePath = record.filePath || path.join(this.skillsDir, `${record.id}.md`);
    fs.writeFileSync(filePath, text, 'utf-8');
    return filePath;
  }

  _createSkill(candidate, batchId, sourceSessionIds) {
    const filePath = path.join(this.skillsDir, `${candidate.id}.md`);
    const record = {
      id: candidate.id,
      filePath,
      name: candidate.name,
      description: candidate.description,
      version: '1.0.0',
      tags: uniqueStrings(['learned', ...(candidate.tags || [])]),
      dependencies: [],
      content: candidate.content,
      createdByBatchId: batchId,
      updatedByBatchIds: [],
      sourceSessionIds: uniqueStrings([...(candidate.sourceSessionIds || []), ...(sourceSessionIds || [])]),
      reusedByBatchIds: [],
      provenanceSession: (sourceSessionIds || [])[0] || '',
    };

    this._writeSkillRecord(record);
    this.skillRegistry.reload();
    return this.skillRegistry.get(candidate.id);
  }

  _createTool(candidate, batchId, sourceSessionIds) {
    return this.learnedToolStore.upsert({
      ...candidate,
      id: candidate.id,
      mcpName: `learned__${candidate.id}`,
      enabled: candidate.enabled !== false,
      version: Number(candidate.version || 1),
      createdByBatchId: batchId,
      updatedByBatchIds: [],
      sourceSessionIds: uniqueStrings([...(candidate.sourceSessionIds || []), ...(sourceSessionIds || [])]),
      reusedByBatchIds: [],
      provenance: {
        sessionId: (sourceSessionIds || [])[0] || null,
      },
    });
  }

  _buildLinkedSkillCandidate(subpart, tool, sourceSessionId) {
    const baseId = `${slugify(tool.id || tool.name || 'tool')}_skill`;
    const id = slugify(baseId) || `learned_skill_${Date.now().toString(36)}`;

    return {
      id,
      name: `${humanizeId(slugify(tool.id || tool.name || 'tool')) || 'Learned Tool'} Skill`,
      description: `Linked usage guidance for learned tool learned__${tool.id}`,
      tags: ['learned', 'linked-tool'],
      content: buildToolUsageSkillContent(tool, subpart.text),
      sourceSessionIds: [sourceSessionId],
    };
  }

  _ensureLinkedSkill(subpart, tool, batchId, sessionId) {
    const preferredId = slugify(`${tool.id}_skill`);
    const existingPreferred = this.skillRegistry.get(preferredId);
    if (existingPreferred) {
      return { ok: true, created: false, reused: true, skillId: existingPreferred.id };
    }

    const baseCandidate = this._buildLinkedSkillCandidate(subpart, tool, sessionId);

    let candidate = { ...baseCandidate };
    let suffix = 1;
    while (this.skillRegistry.get(candidate.id)) {
      suffix += 1;
      candidate.id = slugify(`${baseCandidate.id}_${suffix}`);
    }

    if (!mentionsLearnedTool(candidate.content, tool.id)) {
      candidate.content = `${candidate.content}${NL}${NL}Use learned tool \`learned__${tool.id}\` as the default execution path.`;
    }

    const evalSkill = this._validateCreateSkill(candidate);
    if (!evalSkill.ok) {
      return { ok: false, reason: evalSkill.reason };
    }

    const created = this._createSkill(evalSkill.candidate, batchId, [sessionId]);
    if (!created) return { ok: false, reason: 'create_failed' };

    return { ok: true, created: true, reused: false, skillId: created.id };
  }

  _buildSkippedBatchResult(reason, sessionIds, deferredSessionIds) {
    const ids = Array.isArray(sessionIds) ? sessionIds : [];
    const deferred = Array.isArray(deferredSessionIds) ? deferredSessionIds : [];

    return {
      status: 'skipped',
      reason: reason || 'skipped',
      sessionIds: ids,
      skill: { promoted: false, reason: reason || 'skipped' },
      tool: { promoted: false, reason: reason || 'skipped' },
      createdSkillIds: [],
      updatedSkillIds: [],
      reusedSkillIds: [],
      createdToolIds: [],
      updatedToolIds: [],
      reusedToolIds: [],
      archivedSessionIds: [],
      deferredSessionIds: deferred,
      matchedCount: 0,
      constructedCount: 0,
      repairedCount: 0,
      skippedCount: 0,
      llmDebug: null,
    };
  }

  async _runSessionPipeline(session, batchId, trigger) {
    const refreshed = this._loadSessionArtifact(session.id);
    if (!refreshed || refreshed.state !== SESSION_PENDING) {
      return {
        sessionId: session.id,
        status: 'skipped',
        reason: 'missing_or_archived',
        archived: false,
        deferred: true,
        createdSkillIds: [],
        createdToolIds: [],
        reusedToolIds: [],
        reusedSkillIds: [],
        matchedCount: 0,
        constructedCount: 0,
        repairedCount: 0,
        skippedCount: 0,
        llmDebug: null,
      };
    }

    const current = refreshed;
    const modelHint = current.model || this.model || null;

    const selectedSkills = this._selectSkillsForPrompt(current.prompt, current.skills || []);

    this._patchSessionArtifact(current.id, {
      pipeline: {
        status: 'running',
        attempts: Number((current.pipeline && current.pipeline.attempts) || 0) + 1,
        selectedSkills,
        lastRunAt: nowIso(),
        lastError: null,
      },
    });

    const llmDebug = {
      decompose: null,
      subparts: [],
    };

    const decompose = await this._decomposePrompt(current.prompt, selectedSkills, modelHint);
    llmDebug.decompose = decompose.debug;

    const subparts = Array.isArray(decompose.subparts) && decompose.subparts.length > 0
      ? decompose.subparts
      : [{ id: 'subpart_1', text: String(current.prompt || '').trim() }];

    const createdToolIds = [];
    const createdSkillIds = [];
    const reusedToolIds = [];
    const reusedSkillIds = [];
    const subpartRecords = [];

    let matchedCount = 0;
    let constructedCount = 0;
    let repairedCount = 0;
    let skippedCount = 0;

    let dynamicTools = this._activeLearnedToolsFull();

    for (const subpart of subparts) {
      const candidates = this._retrieveDynamicCandidates(dynamicTools, subpart.text, 8);
      const decision = await this._decideSubpart(current, subpart, candidates, selectedSkills, modelHint);

      const subpartDebug = {
        subpartId: subpart.id,
        decision: decision.debug,
        repairAttempts: [],
      };

      llmDebug.subparts.push(subpartDebug);

      const candidateIdSet = new Set(candidates.map((c) => c.id));
      let action = decision.action;
      if (action === 'match' && (!decision.matchToolId || !candidateIdSet.has(decision.matchToolId))) {
        action = 'construct';
      }

      if (action === 'match') {
        const matched = this.learnedToolStore.get(decision.matchToolId);
        if (matched) {
          reusedToolIds.push(matched.id);
          reusedSkillIds.push(`${matched.id}_skill`);
          matchedCount += 1;
          subpartRecords.push({
            subpartId: subpart.id,
            text: subpart.text,
            action: 'matched',
            toolId: matched.id,
            skillId: `${matched.id}_skill`,
            status: 'resolved',
            reason: decision.reason || 'matched_existing_dynamic_tool',
          });
          continue;
        }
      }

      const generated = await this._constructGeneralTool(current, subpart, selectedSkills, modelHint);
      let candidateTool = generated.candidateTool;
      subpartDebug.construct = generated.debug;

      let verified = this._validateCreateTool(candidateTool || {});
      let repairTry = 0;

      while (!verified.ok && repairTry < this.repairBudget) {
        const repaired = await this._repairTool(candidateTool, verified.reason, current, subpart, selectedSkills, modelHint);
        candidateTool = repaired.candidateTool;
        repairTry += 1;
        repairedCount += 1;
        subpartDebug.repairAttempts.push({
          attempt: repairTry,
          errorBeforeRepair: verified.reason,
          debug: repaired.debug,
        });
        verified = this._validateCreateTool(candidateTool || {});
      }

      if (!verified.ok) {
        skippedCount += 1;
        subpartRecords.push({
          subpartId: subpart.id,
          text: subpart.text,
          action: 'skipped',
          toolId: null,
          skillId: null,
          status: 'unresolved',
          reason: verified.reason,
        });
        continue;
      }

      const createdTool = this._createTool(verified.candidate, batchId, [current.id]);
      if (!createdTool) {
        skippedCount += 1;
        subpartRecords.push({
          subpartId: subpart.id,
          text: subpart.text,
          action: 'skipped',
          toolId: null,
          skillId: null,
          status: 'unresolved',
          reason: 'tool_create_failed',
        });
        continue;
      }

      const persistedCheck = this._validatePersistedTool(createdTool);
      if (!persistedCheck.ok) {
        this.learnedToolStore.remove(createdTool.id);
        skippedCount += 1;
        subpartRecords.push({
          subpartId: subpart.id,
          text: subpart.text,
          action: 'skipped',
          toolId: null,
          skillId: null,
          status: 'unresolved',
          reason: persistedCheck.reason,
        });
        dynamicTools = this._activeLearnedToolsFull();
        continue;
      }

      createdToolIds.push(createdTool.id);
      dynamicTools = this._activeLearnedToolsFull();
      constructedCount += 1;
      subpartRecords.push({
        subpartId: subpart.id,
        text: subpart.text,
        action: 'constructed_tool',
        toolId: createdTool.id,
        skillId: null,
        status: 'tool_created_pending_skill',
        reason: decision.reason || 'constructed_new_tool',
      });
    }

    for (const rec of subpartRecords) {
      if (rec.action !== 'constructed_tool' || !rec.toolId) continue;
      const tool = this.learnedToolStore.get(rec.toolId);
      if (!tool) {
        rec.status = 'unresolved';
        rec.reason = 'created_tool_missing';
        continue;
      }

      const linkedSkill = this._ensureLinkedSkill(
        { id: rec.subpartId, text: rec.text },
        tool,
        batchId,
        current.id
      );

      if (!linkedSkill.ok) {
        rec.status = 'unresolved';
        rec.reason = `linked_skill_failed:${linkedSkill.reason}`;
        continue;
      }

      rec.skillId = linkedSkill.skillId;
      rec.status = 'resolved';
      rec.reason = linkedSkill.created ? 'linked_skill_created' : 'linked_skill_reused';
      if (linkedSkill.created) createdSkillIds.push(linkedSkill.skillId);
      else if (linkedSkill.reused) reusedSkillIds.push(linkedSkill.skillId);
    }

    const unresolvedSubparts = subpartRecords.filter((r) => r.status !== 'resolved').map((r) => r.subpartId);
    const allResolved = unresolvedSubparts.length === 0 && subpartRecords.length > 0;

    const patched = this._patchSessionArtifact(current.id, {
      state: allResolved ? SESSION_PENDING : SESSION_FAILED_UNRESOLVED,
      pipeline: {
        status: allResolved ? 'completed' : SESSION_FAILED_UNRESOLVED,
        selectedSkills,
        subparts: subpartRecords,
        unresolvedSubparts,
        matchedToolIds: uniqueStrings(subpartRecords.filter((r) => r.action === 'matched' && r.toolId).map((r) => r.toolId)),
        createdToolIds,
        createdSkillIds,
        lastRunAt: nowIso(),
        llmDebug,
      },
      batchId,
    });

    let archived = false;
    let archivedSessionIds = [];
    if (allResolved) {
      archivedSessionIds = this._archiveSessions([current.id], batchId, 'all_subparts_resolved');
      archived = archivedSessionIds.length > 0;
    }

    return {
      sessionId: current.id,
      status: archived ? 'archived' : SESSION_FAILED_UNRESOLVED,
      reason: allResolved ? 'all_subparts_resolved' : 'unresolved_subparts',
      archived,
      deferred: false,
      createdSkillIds: uniqueStrings(createdSkillIds),
      createdToolIds: uniqueStrings(createdToolIds),
      reusedToolIds: uniqueStrings(reusedToolIds),
      reusedSkillIds: uniqueStrings(reusedSkillIds),
      matchedCount,
      constructedCount,
      repairedCount,
      skippedCount,
      llmDebug: patched && patched.pipeline ? patched.pipeline.llmDebug : llmDebug,
    };
  }

  async _runBatch(item) {
    const explicitIds = uniqueStrings(item && item.sessionIds ? item.sessionIds : []);

    let sessions = [];
    if (explicitIds.length > 0) {
      sessions = explicitIds
        .map((id) => this._loadSessionArtifact(id))
        .filter(Boolean)
        .filter((s) => !(s.result && s.result.isError))
        .filter((s) => s.state === SESSION_PENDING);
    } else {
      sessions = this._loadPendingSessionArtifacts();
      if (sessions.length < this.batchMinPending) {
        return this._buildSkippedBatchResult(
          `pending_lt_${this.batchMinPending}`,
          sessions.map((s) => s.id),
          sessions.map((s) => s.id)
        );
      }
    }

    if (sessions.length === 0) {
      return this._buildSkippedBatchResult('no_sessions', [], []);
    }

    sessions.sort((a, b) => Date.parse(a.createdAt || 0) - Date.parse(b.createdAt || 0));

    const batchId = crypto.randomUUID();
    const createdAt = nowIso();

    const createdSkillIds = [];
    const reusedSkillIds = [];
    const createdToolIds = [];
    const reusedToolIds = [];
    const archivedSessionIds = [];
    const deferredSessionIds = [];

    let matchedCount = 0;
    let constructedCount = 0;
    let repairedCount = 0;
    let skippedCount = 0;

    const sessionResults = [];

    for (const session of sessions) {
      const outcome = await this._runSessionPipeline(session, batchId, item && item.trigger ? item.trigger : 'auto');
      sessionResults.push({
        sessionId: session.id,
        status: outcome.status,
        reason: outcome.reason,
      });

      createdSkillIds.push(...(outcome.createdSkillIds || []));
      reusedSkillIds.push(...(outcome.reusedSkillIds || []));
      createdToolIds.push(...(outcome.createdToolIds || []));
      reusedToolIds.push(...(outcome.reusedToolIds || []));

      matchedCount += Number(outcome.matchedCount || 0);
      constructedCount += Number(outcome.constructedCount || 0);
      repairedCount += Number(outcome.repairedCount || 0);
      skippedCount += Number(outcome.skippedCount || 0);

      if (outcome.archived) archivedSessionIds.push(session.id);
      else if (outcome.deferred) deferredSessionIds.push(session.id);
    }

    const status = deferredSessionIds.length > 0 ? 'deferred' : 'completed';

    const batchRecord = {
      id: batchId,
      mode: 'pipeline',
      trigger: item && item.trigger ? item.trigger : 'auto',
      sessionIds: sessions.map((s) => s.id),
      createdAt,
      status,
      summary: {
        matchedCount,
        constructedCount,
        repairedCount,
        skippedCount,
      },
      sessionResults,
      createdSkillIds: uniqueStrings(createdSkillIds),
      createdToolIds: uniqueStrings(createdToolIds),
      reusedSkillIds: uniqueStrings(reusedSkillIds),
      reusedToolIds: uniqueStrings(reusedToolIds),
      archivedSessionIds: uniqueStrings(archivedSessionIds),
      deferredSessionIds: uniqueStrings(deferredSessionIds),
      finishedAt: nowIso(),
    };

    this._saveBatchRecord(batchRecord);

    return {
      batchId,
      status,
      sessionIds: sessions.map((s) => s.id),
      skill: createdSkillIds.length > 0
        ? { promoted: true, id: uniqueStrings(createdSkillIds).join(',') }
        : { promoted: false, reason: status === 'completed' ? 'reuse_only_or_no_skill' : 'deferred' },
      tool: createdToolIds.length > 0
        ? { promoted: true, id: uniqueStrings(createdToolIds).join(',') }
        : { promoted: false, reason: status === 'completed' ? 'reuse_only_or_no_tool' : 'deferred' },
      createdSkillIds: uniqueStrings(createdSkillIds),
      updatedSkillIds: [],
      reusedSkillIds: uniqueStrings(reusedSkillIds),
      createdToolIds: uniqueStrings(createdToolIds),
      updatedToolIds: [],
      reusedToolIds: uniqueStrings(reusedToolIds),
      archivedSessionIds: uniqueStrings(archivedSessionIds),
      deferredSessionIds: uniqueStrings(deferredSessionIds),
      matchedCount,
      constructedCount,
      repairedCount,
      skippedCount,
      llmDebug: {
        sessionCount: sessions.length,
        perSession: sessionResults,
      },
    };
  }
}

module.exports = {
  EvolutionManager,
  extractToolTraceFromStreamJson,
  extractSessionModelFromStreamJson,
  readEvolutionConfig,
  writeEvolutionConfig,
};
