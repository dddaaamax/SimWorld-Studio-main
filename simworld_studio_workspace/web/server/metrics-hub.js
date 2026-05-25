'use strict';

const MAX_POINTS = 60;  // 5 min at 5s interval
const MAX_SERIES = 50;  // max concurrent tracked agents (prevents unbounded memory on churn)

/**
 * MetricsHub — time-series data store for all agents.
 * Sampled every INTERVAL_MS. Consumers call snapshot() to get current state.
 *
 * Series structure:
 *   { [agentName]: { ts: number[], collision: number[], speed: number[], turns: number[], status: string[] } }
 */
class MetricsHub {
  constructor(intervalMs = 5000) {
    this._intervalMs = intervalMs;
    this._series = {};       // agentName → { ts, collision, speed, turns, status }
    this._agentCtrl = null;
    this._timer = null;
    this._sceneCollisions = []; // global scene collision count over time [{ts, count}]
  }

  /** Call once after agentCtrl is created */
  init(agentCtrl) {
    this._agentCtrl = agentCtrl;
    this._timer = setInterval(() => this._sample(), this._intervalMs);
  }

  _sample() {
    if (!this._agentCtrl) return;
    const ts = Date.now();
    const sessions = this._agentCtrl.list();

    for (const s of sessions) {
      const name = s.agentName;
      if (!this._series[name]) {
        // Cap total series to prevent unbounded memory on high-churn scenarios
        if (Object.keys(this._series).length >= MAX_SERIES) continue;
        this._series[name] = { ts: [], collision: [], speed: [], turns: [], status: [] };
      }
      const series = this._series[name];
      series.ts.push(ts);
      series.collision.push(s.collisionCount || 0);
      series.speed.push(Math.round((s.speed || 0) / 100)); // cm/s → m/s
      series.turns.push(s.totalTurns || 0);
      series.status.push(s.status || 'idle');

      // Trim to MAX_POINTS
      if (series.ts.length > MAX_POINTS) {
        series.ts.shift();
        series.collision.shift();
        series.speed.shift();
        series.turns.shift();
        series.status.shift();
      }
    }

    // Prune series for agents that no longer exist
    const alive = new Set(sessions.map(s => s.agentName));
    for (const name of Object.keys(this._series)) {
      if (!alive.has(name)) delete this._series[name];
    }
  }

  /** Record a physics hit event from OnActorHit — call immediately when hit detected */
  recordAgentHit(agentName, hitEvent) {
    if (!this._series[agentName]) return;
    const series = this._series[agentName];
    // Stamp the latest collision count so chart updates promptly
    const last = series.collision.length > 0 ? series.collision[series.collision.length - 1] : 0;
    series.ts.push(Date.now());
    series.collision.push(last + 1);
    series.speed.push(series.speed[series.speed.length - 1] || 0);
    series.turns.push(series.turns[series.turns.length - 1] || 0);
    series.status.push('hit');
    if (series.ts.length > MAX_POINTS) {
      series.ts.shift(); series.collision.shift();
      series.speed.shift(); series.turns.shift(); series.status.shift();
    }
  }

  /** Record a scene-level collision count snapshot (called from verifier or on demand) */
  recordSceneCollisions(count) {
    this._sceneCollisions.push({ ts: Date.now(), count });
    if (this._sceneCollisions.length > MAX_POINTS) this._sceneCollisions.shift();
  }

  /** Returns a compact snapshot for SSE push */
  snapshot() {
    return {
      series: this._series,
      sceneCollisions: this._sceneCollisions,
      sampledAt: Date.now(),
      intervalMs: this._intervalMs,
    };
  }

  /** Clear all series (e.g. on scene reset) */
  clear() {
    this._series = {};
    this._sceneCollisions = [];
  }

  destroy() {
    if (this._timer) { clearInterval(this._timer); this._timer = null; }
  }
}

module.exports = { MetricsHub };
