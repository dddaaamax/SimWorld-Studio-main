'use strict';
/**
 * logger.js — Structured JSON logger (P3-1 fix)
 *
 * Writes newline-delimited JSON (NDJSON) to a rotating daily file.
 * Each record: { time, level, category, msg, requestId?, sessionToken?, ...data }
 *
 * - Async stream writes: no interleaving between concurrent requests
 * - Log level filter via LOG_LEVEL env var
 * - Backwards-compatible API: same method signatures as the old logger
 */

const fs   = require('fs');
const path = require('path');

const LOG_DIR  = process.env.LOG_DIR || path.resolve(__dirname, '../../logs');
fs.mkdirSync(LOG_DIR, { recursive: true });

const LOG_LEVEL  = process.env.LOG_LEVEL || 'info';
const LEVELS     = { debug: 0, info: 1, warn: 2, error: 3 };
const IS_DEV     = process.env.NODE_ENV !== 'production';

// ── Stream cache (one stream per file per day) ────────────────────────────────
const _streams   = new Map();
function _getStream(filename) {
  if (_streams.has(filename)) return _streams.get(filename);
  const ws = fs.createWriteStream(path.join(LOG_DIR, filename), { flags: 'a' });
  ws.on('error', err => process.stderr.write(`[logger] stream error ${filename}: ${err.message}\n`));
  _streams.set(filename, ws);
  return ws;
}

// Rotate daily — close stale streams at midnight
let _currentDate = new Date().toISOString().slice(0, 10);
setInterval(() => {
  const today = new Date().toISOString().slice(0, 10);
  if (today !== _currentDate) {
    for (const [name, ws] of _streams) {
      if (!name.includes(today)) { try { ws.end(); } catch {} _streams.delete(name); }
    }
    _currentDate = today;
  }
}, 60_000).unref?.();

// ── Core log function ─────────────────────────────────────────────────────────
function _log(category, level, message, data) {
  if ((LEVELS[level] || 0) < (LEVELS[LOG_LEVEL] || 0)) return;

  const record = {
    time:     new Date().toISOString(),
    level,
    category,
    msg:      message,
    ...(data && typeof data === 'object' ? data : data != null ? { data } : {}),
  };

  const line = JSON.stringify(record) + '\n';
  const date = record.time.slice(0, 10);

  // Write to category file + combined file (async, non-blocking)
  try { _getStream(`${category}_${date}.ndjson`).write(line); } catch {}
  try { _getStream(`combined_${date}.ndjson`).write(line); } catch {}

  // Dev console — colorized human-readable
  if (IS_DEV) {
    const COLORS = { debug: '\x1b[90m', info: '\x1b[36m', warn: '\x1b[33m', error: '\x1b[31m' };
    const RST = '\x1b[0m';
    const extra = data ? ' ' + JSON.stringify(data) : '';
    process.stderr.write(`${COLORS[level] || ''}[${category}] ${message}${extra}${RST}\n`);
  }
}

// ── Public API (backwards-compatible with old logger.js) ─────────────────────
const logger = {
  debug:  (cat, msg, data) => _log(cat, 'debug', msg, data),
  info:   (cat, msg, data) => _log(cat, 'info',  msg, data),
  warn:   (cat, msg, data) => _log(cat, 'warn',  msg, data),
  error:  (cat, msg, data) => _log(cat, 'error', msg, data),

  // Category shortcuts used throughout the codebase
  chat:   (level, msg, data) => _log('chat',   level, msg, data),
  agent:  (level, msg, data) => _log('agent',  level, msg, data),
  ucv:    (level, msg, data) => _log('ucv',    level, msg, data),
  mcp:    (level, msg, data) => _log('mcp',    level, msg, data),
  system: (level, msg, data) => _log('system', level, msg, data),
  ctx:    (level, msg, data) => _log('ctx',    level, msg, data),

  // Request-scoped child logger: logger.child({ requestId, sessionToken })
  child(ctx) {
    return {
      debug:  (cat, msg, data) => _log(cat, 'debug', msg, { ...ctx, ...data }),
      info:   (cat, msg, data) => _log(cat, 'info',  msg, { ...ctx, ...data }),
      warn:   (cat, msg, data) => _log(cat, 'warn',  msg, { ...ctx, ...data }),
      error:  (cat, msg, data) => _log(cat, 'error', msg, { ...ctx, ...data }),
    };
  },
};

module.exports = logger;
