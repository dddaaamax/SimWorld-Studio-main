'use strict';

// ---------------------------------------------------------------------------
// UnrealBridge — single-process broker for UnrealCV (port 9000).
//
// WHY THIS EXISTS:
//   Before this file, every code path that needed to talk to UnrealCV opened
//   its own one-shot TCP socket: agent-controller's getObservation, agent stop,
//   the per-Claude mcp-server subprocesses, the health check. With N panel
//   agents running, that's 5+ independent UCV clients hammering port 9000 with
//   no global coordination. Spawning a new agent in UE resets the connection,
//   silently killing every other in-flight command. Observation calls had no
//   retry, so agents would run blind without anyone noticing.
//
// WHAT THIS DOES:
//   Owns ONE persistent TCP connection to UCV. Serializes commands through a
//   FIFO queue (single-flight). Auto-reconnects with exponential backoff.
//   Requeues in-flight commands on disconnect. Per-command timeout + retries.
//   Drops jobs that have been queued longer than the queue deadline.
//
// SCOPE:
//   Phase 1: agent-controller.js calls broker directly (same process).
//   Phase 2: mcp-server.js subprocesses call HTTP RPC into the main server,
//            which forwards to this broker. (not yet implemented)
// ---------------------------------------------------------------------------

const net = require('net');
const log = require('./logger');

const UCV_PORT = parseInt(process.env.UCV_PORT || '9000', 10);
const UCV_HOST = process.env.UCV_HOST || '127.0.0.1';
const UCV_MAGIC = 0x9E2B83C1;

const RECONNECT_DELAY_MIN = 500;
const RECONNECT_DELAY_MAX = 5000;

const DEFAULT_TIMEOUT_MS = 10000;
const DEFAULT_RETRIES = 3;
const DEFAULT_QUEUE_DEADLINE_MS = 30000;

class UcvBroker {
  constructor() {
    this.sock = null;
    this.connecting = false;
    this.connected = false;
    this.gotBanner = false;
    this.buf = Buffer.alloc(0);

    this.queue = [];          // [{cmd, timeoutMs, retries, attempts, enqueuedAt, queueDeadlineMs, resolve, reject, timer}]
    this.inFlight = null;     // current job awaiting UCV response

    this.msgIdCounter = 200;
    this.reconnectDelay = RECONNECT_DELAY_MIN;
    this.reconnectTimer = null;

    // Metrics for /api/events health surface
    this.totalSent = 0;
    this.totalErrors = 0;
    this.totalRequeues = 0;
    this.lastError = null;
    this.lastConnectedAt = null;
  }

  /**
   * Public API — drop-in replacement for the old ucvCommand().
   * Returns a Promise<string> with the UCV payload (id prefix stripped).
   *
   * @param {string} cmd  e.g. "vget /object/Pedestrian_1/location"
   * @param {object} opts
   * @param {number} opts.timeoutMs       per-attempt wire timeout (default 10s)
   * @param {number} opts.retries         max attempts before rejecting (default 3)
   * @param {number} opts.queueDeadlineMs reject if not started within this many ms (default 30s)
   */
  send(cmd, opts = {}) {
    const {
      timeoutMs = DEFAULT_TIMEOUT_MS,
      retries = DEFAULT_RETRIES,
      queueDeadlineMs = DEFAULT_QUEUE_DEADLINE_MS,
    } = opts;

    return new Promise((resolve, reject) => {
      const job = {
        cmd,
        timeoutMs,
        retries,
        queueDeadlineMs,
        attempts: 0,
        enqueuedAt: Date.now(),
        resolve,
        reject,
        timer: null,
      };
      this.queue.push(job);
      this._pump();
    });
  }

  status() {
    return {
      connected: this.connected,
      gotBanner: this.gotBanner,
      queueDepth: this.queue.length,
      inFlight: this.inFlight ? this.inFlight.cmd.slice(0, 80) : null,
      totalSent: this.totalSent,
      totalErrors: this.totalErrors,
      totalRequeues: this.totalRequeues,
      lastError: this.lastError,
      lastConnectedAt: this.lastConnectedAt,
    };
  }

  // ── Connection management ────────────────────────────────────────────────

  _ensureConnected() {
    if (this.sock || this.connecting) return;
    this.connecting = true;
    this.gotBanner = false;
    this.buf = Buffer.alloc(0);

    log.agent('debug', `[ucv-broker] connecting to ${UCV_HOST}:${UCV_PORT}`);
    const sock = new net.Socket();
    this.sock = sock;

    sock.on('connect', () => {
      log.agent('info', `[ucv-broker] socket connected, awaiting banner`);
      this.connecting = false;
      this.connected = true;
      this.lastConnectedAt = Date.now();
      this.reconnectDelay = RECONNECT_DELAY_MIN; // reset backoff
    });

    sock.on('data', (chunk) => this._onData(chunk));
    sock.on('close', () => this._onDisconnect('close'));
    sock.on('error', (err) => {
      this.lastError = err.message;
      this._onDisconnect(`error: ${err.message}`);
    });

    sock.connect(UCV_PORT, UCV_HOST);
  }

  _onDisconnect(reason) {
    // Idempotent — error and close both fire on bad sockets, only handle once
    if (!this.sock && !this.connecting && !this.connected) return;

    log.agent('warn', `[ucv-broker] disconnected: ${reason}`);
    try { this.sock?.destroy(); } catch {}
    this.sock = null;
    this.connected = false;
    this.connecting = false;
    this.gotBanner = false;
    this.buf = Buffer.alloc(0);

    // If a command was in-flight, requeue it (it'll consume one retry budget)
    if (this.inFlight) {
      const job = this.inFlight;
      this.inFlight = null;
      if (job.timer) { clearTimeout(job.timer); job.timer = null; }

      if (job.attempts < job.retries) {
        this.totalRequeues++;
        log.agent('info', `[ucv-broker] requeue "${job.cmd.slice(0, 60)}" (attempts ${job.attempts}/${job.retries})`);
        this.queue.unshift(job);
      } else {
        this.totalErrors++;
        job.reject(new Error(`UCV disconnected after ${job.attempts} attempts: ${reason}`));
      }
    }

    // Sweep stale queued jobs — important when UCV is unreachable: without this,
    // jobs sit in the queue forever during reconnect cycling because _pump is
    // only called on successful connect. We sweep here AND in the reconnect
    // timer callback so deadlines are honored even in pure-failure loops.
    this._dropStaleJobs();

    // Schedule reconnect only if there are still live jobs waiting.
    if (this.reconnectTimer) clearTimeout(this.reconnectTimer);
    if (this.queue.length > 0) {
      this.reconnectTimer = setTimeout(() => {
        this.reconnectTimer = null;
        this._dropStaleJobs();
        if (this.queue.length > 0) this._ensureConnected();
      }, this.reconnectDelay);
      this.reconnectDelay = Math.min(this.reconnectDelay * 2, RECONNECT_DELAY_MAX);
    }
  }

  _dropStaleJobs() {
    const now = Date.now();
    const live = [];
    for (const job of this.queue) {
      if (now - job.enqueuedAt > job.queueDeadlineMs) {
        this.totalErrors++;
        job.reject(new Error(`UCV queue deadline ${job.queueDeadlineMs}ms exceeded: ${job.cmd.slice(0, 60)}`));
      } else {
        live.push(job);
      }
    }
    this.queue = live;
  }

  // ── Protocol parsing ─────────────────────────────────────────────────────

  _onData(chunk) {
    this.buf = Buffer.concat([this.buf, chunk]);
    while (true) {
      const frame = this._parseFrame();
      if (frame === null) break;
      if (!this.gotBanner) {
        this.gotBanner = true;
        log.agent('debug', `[ucv-broker] banner received, ready`);
        this._pump();
        continue;
      }
      this._onResponse(frame);
    }
  }

  _parseFrame() {
    if (this.buf.length < 8) return null;
    if (this.buf.readUInt32LE(0) !== UCV_MAGIC) {
      // Out of sync — drop connection and reconnect
      log.agent('warn', `[ucv-broker] frame magic mismatch, force reconnect`);
      this._onDisconnect('magic mismatch');
      return null;
    }
    const sz = this.buf.readUInt32LE(4);
    if (this.buf.length < 8 + sz) return null;
    const payload = this.buf.slice(8, 8 + sz).toString('utf-8');
    this.buf = this.buf.slice(8 + sz);
    return payload;
  }

  _onResponse(payload) {
    const job = this.inFlight;
    if (!job) {
      log.agent('warn', `[ucv-broker] unsolicited frame: ${payload.slice(0, 80)}`);
      return;
    }
    // Strip "<id>:" prefix from response (matches old ucvCommand behavior)
    let result = payload;
    const ci = result.indexOf(':');
    if (ci > 0 && ci < 6) result = result.slice(ci + 1);

    if (job.timer) { clearTimeout(job.timer); job.timer = null; }
    this.inFlight = null;
    this.totalSent++;
    job.resolve(result);
    this._pump();
  }

  // ── Queue pump ───────────────────────────────────────────────────────────

  _pump() {
    this._dropStaleJobs();

    if (this.inFlight) return;
    if (this.queue.length === 0) return;

    if (!this.sock || !this.gotBanner) {
      this._ensureConnected();
      return;
    }

    const job = this.queue.shift();
    this.inFlight = job;
    job.attempts++;

    job.timer = setTimeout(() => {
      log.agent('warn', `[ucv-broker] timeout "${job.cmd.slice(0, 60)}" attempt ${job.attempts}/${job.retries}`);
      // Force a disconnect — _onDisconnect will requeue or reject based on retries
      this._onDisconnect('command timeout');
    }, job.timeoutMs);

    const id = this.msgIdCounter++;
    const msg = `${id}:${job.cmd}`;
    const payload = Buffer.from(msg, 'utf-8');
    const header = Buffer.alloc(8);
    header.writeUInt32LE(UCV_MAGIC, 0);
    header.writeUInt32LE(payload.length, 4);

    try {
      this.sock.write(Buffer.concat([header, payload]));
    } catch (err) {
      log.agent('warn', `[ucv-broker] write failed: ${err.message}`);
      // Treat as disconnect — _onDisconnect will requeue with retry budget
      this._onDisconnect(`write error: ${err.message}`);
    }
  }
}

// Singleton — one broker per process
let _instance = null;
function getBroker() {
  if (!_instance) _instance = new UcvBroker();
  return _instance;
}

module.exports = { UcvBroker, getBroker };
