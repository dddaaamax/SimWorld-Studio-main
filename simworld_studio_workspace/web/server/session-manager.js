'use strict';
/**
 * session-manager.js  (CommonJS)
 * UE slot pool: max N concurrent users, 30-min sliding TTL, wait queue.
 */

const crypto       = require('crypto');
const { EventEmitter } = require('events');

const UE_POOL_SIZE     = parseInt(process.env.UE_POOL_SIZE      || '10',  10);
const UE_MAX_QUEUE     = parseInt(process.env.UE_MAX_QUEUE      || '40',  10);
const SESSION_TTL_MS   = parseInt(process.env.SESSION_TTL_MS    || String(30 * 60 * 1000), 10);
const SESSION_HARD_MAX = parseInt(process.env.SESSION_HARD_MAX_MS || String(60 * 60 * 1000), 10);
const SWEEP_INTERVAL   = 60_000;

// UE port layout: each slot gets its own port range (stride = 2)
const UE_BASE_MCP      = parseInt(process.env.UE_BASE_MCP_PORT    || '55559', 10);
const UE_BASE_CIRRUS_H = parseInt(process.env.UE_BASE_CIRRUS_HTTP || '8585',  10);
const UE_BASE_CIRRUS_W = parseInt(process.env.UE_BASE_CIRRUS_WS   || '8586',  10);
const UE_PORT_STRIDE   = parseInt(process.env.UE_PORT_STRIDE       || '2',     10);

function uePortsForSlot(slotId) {
  return {
    mcpPort:    UE_BASE_MCP      + slotId * UE_PORT_STRIDE,
    cirrusHttp: UE_BASE_CIRRUS_H + slotId * UE_PORT_STRIDE,
    cirrusWs:   UE_BASE_CIRRUS_W + slotId * UE_PORT_STRIDE,
  };
}

class SessionManager extends EventEmitter {
  constructor() {
    super();
    /** @type {Map<string, object>} token → record */
    this._sessions  = new Map();
    /** @type {Set<number>} available slot IDs */
    this._freeSlots = new Set(Array.from({ length: UE_POOL_SIZE }, (_, i) => i));
    /** @type {Array<{userId,resolve,reject,enqueuedAt}>} */
    this._queue     = [];
    this._sweeper   = setInterval(() => this._sweep(), SWEEP_INTERVAL);
    if (this._sweeper.unref) this._sweeper.unref();
  }

  // ── Public ─────────────────────────────────────────────────────────────────

  get totalSlots()     { return UE_POOL_SIZE; }
  get freeSlots()      { return this._freeSlots.size; }
  get activeSessions() { return this._sessions.size; }
  get queueLength()    { return this._queue.length; }

  /**
   * Acquire a session slot (returns existing if userId already has one).
   * @param {string} userId
   * @returns {Promise<object>} session record
   */
  acquire(userId) {
    // Reuse existing session for same user
    for (const rec of this._sessions.values()) {
      if (rec.userId === userId) {
        rec.lastActivity = Date.now();
        return Promise.resolve(rec);
      }
    }

    if (this._freeSlots.size > 0) {
      return Promise.resolve(this._assign(userId));
    }

    if (this._queue.length >= UE_MAX_QUEUE) {
      const err = new Error('Server at capacity. Please try again later.');
      err.code = 'POOL_FULL';
      err.queueLength = this._queue.length;
      return Promise.reject(err);
    }

    return new Promise((resolve, reject) => {
      this._queue.push({ userId, resolve, reject, enqueuedAt: Date.now() });
      this.emit('queued', { userId, position: this._queue.length });
    });
  }

  /**
   * Refresh TTL and return record, or null if token unknown.
   * @param {string} token
   * @returns {object|null}
   */
  touch(token) {
    const rec = this._sessions.get(token);
    if (!rec) return null;
    rec.lastActivity = Date.now();
    return rec;
  }

  /** Release a slot explicitly (user logout / tab close). */
  release(token) {
    if (this._sessions.has(token)) this._evict(token, 'released');
  }

  /** Admin snapshot for health endpoint. */
  snapshot() {
    const now = Date.now();
    return [...this._sessions.values()].map(r => ({
      token:    r.token.slice(0, 8) + '…',
      slotId:   r.slotId,
      userId:   r.userId,
      ageMs:    now - r.acquiredAt,
      idleMs:   now - r.lastActivity,
      uePorts:  r.uePorts,
    }));
  }

  destroy() {
    clearInterval(this._sweeper);
    // Reject all pending queue waiters so callers don't hang
    for (const w of this._queue) {
      try { w.reject(new Error('SessionManager destroyed')); } catch {}
    }
    this._queue = [];
  }

  // ── Internal ───────────────────────────────────────────────────────────────

  _assign(userId) {
    const slotId = [...this._freeSlots][0];
    this._freeSlots.delete(slotId);
    const now = Date.now();
    const rec = {
      token:        crypto.randomBytes(32).toString('hex'),
      slotId,
      userId,
      acquiredAt:   now,
      lastActivity: now,
      uePorts:      uePortsForSlot(slotId),
    };
    this._sessions.set(rec.token, rec);
    this.emit('acquired', { token: rec.token, slotId, userId });
    return rec;
  }

  _evict(token, reason) {
    const rec = this._sessions.get(token);
    if (!rec) return;
    this._sessions.delete(token);
    this._freeSlots.add(rec.slotId);
    this.emit('released', { token, slotId: rec.slotId, reason });
    // Drain wait queue
    while (this._queue.length > 0 && this._freeSlots.size > 0) {
      const waiter = this._queue.shift();
      try { waiter.resolve(this._assign(waiter.userId)); }
      catch (e) { waiter.reject(e); }
    }
  }

  _sweep() {
    const now = Date.now();
    for (const [token, rec] of this._sessions) {
      const idle = now - rec.lastActivity;
      const age  = now - rec.acquiredAt;
      if (idle > SESSION_TTL_MS || age > SESSION_HARD_MAX) {
        this._evict(token, idle > SESSION_TTL_MS ? 'idle_timeout' : 'hard_limit');
      }
    }
    // Evict stuck waiters (> 5 min in queue)
    const WAIT_MAX = 5 * 60 * 1000;
    this._queue = this._queue.filter(w => {
      if (now - w.enqueuedAt > WAIT_MAX) {
        w.reject(new Error('Queue wait timed out'));
        return false;
      }
      return true;
    });
  }
}

const sessionManager = new SessionManager();
module.exports = { SessionManager, sessionManager };
