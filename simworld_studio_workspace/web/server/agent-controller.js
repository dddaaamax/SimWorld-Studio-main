'use strict';

const { spawn } = require('child_process');
const path = require('path');
const fs = require('fs');
const log = require('./logger');
const { getBroker } = require('./unreal-bridge');

const MCP_CONFIG = path.resolve(__dirname, '../mcp.json');
const CLAUDE_BIN = process.env.CLAUDE_BIN || 'claude';
const { SkillRegistry } = require('./skills');
const REGISTRY = JSON.parse(fs.readFileSync(path.resolve(__dirname, 'agent-registry.json'), 'utf-8'));

// Shared skill registry — panel agents get agent-relevant skills auto-injected
const skillRegistry = new SkillRegistry();

// ---------------------------------------------------------------------------
// UnrealCV access — all UCV traffic in this process goes through the singleton
// UcvBroker (see unreal-bridge.js). The old per-call one-shot TCP implementation
// raced with mcp-server subprocesses on port 9000 and silently dropped commands
// when spawn_agent reset the connection. The broker owns one persistent
// connection, serializes commands FIFO, retries on disconnect.
// ---------------------------------------------------------------------------

const broker = getBroker();

/** Compat shim — preserves old `ucvCommand(cmd, timeoutMs)` signature so any
 *  existing call site (e.g. AgentSession.stop) works unchanged. */
function ucvCommand(cmd, timeoutMs = 10000) {
  return broker.send(cmd, { timeoutMs });
}

async function getObservation(agentName) {
  try {
    const [loc, rot, vel] = await Promise.all([
      broker.send(`vget /object/${agentName}/location`, { timeoutMs: 8000, retries: 3, queueDeadlineMs: 30000 }),
      broker.send(`vget /object/${agentName}/rotation`, { timeoutMs: 8000, retries: 3, queueDeadlineMs: 30000 }),
      broker.send(`vget /object/${agentName}/velocity`, { timeoutMs: 4000, retries: 1, queueDeadlineMs: 10000 }),
    ]);
    const location = loc.trim().split(/\s+/).map(Number);
    const rotation = rot.trim().split(/\s+/).map(Number);
    const velocity = vel ? vel.trim().split(/\s+/).map(Number) : null;
    const speed    = velocity ? Math.sqrt(velocity.reduce((s,v)=>s+v*v, 0)) : 0;
    return { location, rotation, velocity, speed };
  } catch (err) {
    log.agent('warn', `getObservation(${agentName}) failed: ${err.message}`);
    return { location: null, rotation: null, velocity: null, speed: 0 };
  }
}

async function getEnvironmentFeedback(agentName, radiusCm = 300) {
  try {
    const raw = await broker.send(`vget /object/${agentName}/nearby ${radiusCm}`,
      { timeoutMs: 3000, retries: 1, queueDeadlineMs: 5000 });
    return JSON.parse(raw || '[]');
  } catch { return []; }
}

async function enableHitTracking(agentName) {
  try {
    await broker.send(`vset /object/${agentName}/track_hits`,
      { timeoutMs: 3000, retries: 1, queueDeadlineMs: 5000 });
  } catch { /* ignore — actor might not exist yet */ }
}

async function getHitEvents(agentName) {
  try {
    const raw = await broker.send(`vget /object/${agentName}/hit_events`,
      { timeoutMs: 3000, retries: 1, queueDeadlineMs: 5000 });
    return JSON.parse(raw || '[]');
  } catch { return []; }
}

async function getOverlaps(agentName) {
  try {
    const raw = await broker.send(`vget /object/${agentName}/overlaps`,
      { timeoutMs: 3000, retries: 1, queueDeadlineMs: 5000 });
    return JSON.parse(raw || '[]');
  } catch { return []; }
}

// ---------------------------------------------------------------------------
// ReAct activity log entry
// ---------------------------------------------------------------------------

/**
 * Activity log entry — one per agent turn, captures the full ReAct cycle.
 * { thought, actions: [{tool, input, result, ok}], response, timestamp, cost }
 */

// ---------------------------------------------------------------------------
// Per-agent session
// ---------------------------------------------------------------------------

class AgentSession {
  constructor({ agentName, agentClass, location }) {
    this.agentName    = agentName;
    this.agentClass   = agentClass;
    this.location     = location;
    this.rotation     = null;   // [pitch, yaw, roll] from UE
    this.velocity     = null;   // [vx, vy, vz] cm/s
    this.speed        = 0;      // scalar speed cm/s
    this._prevOverlaps = new Set(); // tracks previous overlap set to detect NEW contacts

    this.status        = 'idle'; // idle | running
    this.currentAction = null;   // tool name currently executing
    this.proc          = null;

    // Turn history & activity
    this.history           = [];  // conversation turns
    this.inbox             = [];  // inter-agent messages
    this.activity          = [];  // ReAct logs (last 20 turns)
    this._currentActivity  = null;

    // Real-time tracking
    this.positionUpdatedAt = null;
    this.trajectory        = [];  // [{loc, rot, ts, action}] last 200 points
    this.collisionCount    = 0;   // total collisions detected
    this.recentCollisions  = [];  // last 20 collision events {ts, overlapping, loc}
    this.envFeedback       = [];  // last 20 environment feedback events
    this.memory            = [];  // summarized memory entries from past turns

    // Stats
    this.totalTurns  = 0;
    this.totalCostUsd = 0;
    this.createdAt   = Date.now();
  }

  _resolveType() {
    const cls = (this.agentClass || '').toLowerCase();
    for (const [typeName, def] of Object.entries(REGISTRY.agentTypes)) {
      if (def.namePatterns.some(p => cls.includes(p))) return typeName;
    }
    return 'pedestrian';
  }

  _systemPrompt() {
    const loc = Array.isArray(this.location)
      ? `(${this.location.map(v => Math.round(v)).join(', ')})`
      : 'unknown';
    const type = this._resolveType();
    const typeDef = REGISTRY.agentTypes[type];

    const lines = [
      `You control agent "${this.agentName}" (${type}) at ${loc}.`,
      '',
      '## Actions (use agent_action tool)',
    ];

    if (typeDef?.actions) {
      for (const [name, def] of Object.entries(typeDef.actions)) {
        const paramStr = def.params ? `, params: {${def.params.join(', ')}}` : '';
        lines.push(`- agent_action(agent_name="${this.agentName}", action="${name}", agent_type="${type}"${paramStr}) — ${def.description}`);
      }
    }

    lines.push(
      '',
      '## Other Tools',
      `- agent_stop(agent_name="${this.agentName}", agent_type="${type}")`,
      `- agent_rotate(agent_name="${this.agentName}", angle=N, direction="left"|"right", agent_type="${type}")`,
      `- get_agent_state(agent_name="${this.agentName}")`,
      '- get_actors_in_level()',
      '- take_screenshot()',
      '',
      '## Communication',
      'To message another agent, include @AgentName in your response.',
      '',
      '## Rules',
      `- Always use agent_name="${this.agentName}"`,
      '- Only control YOUR agent.',
      '- Think step by step: observe → think → act → verify.',
      '- Be concise.',
    );

    // Inject agent-relevant skills (movement, navigation, facing, etc.)
    const agentSkills = skillRegistry.search('agent', ['agent', 'movement', 'navigation']);
    if (agentSkills.length > 0) {
      const composed = skillRegistry.compose(agentSkills.map(s => s.id));
      if (composed) {
        lines.push('', '## SKILLS (reference documentation)', composed);
      }
    }

    if (this.history.length > 0) {
      lines.push('', '## Recent History');
      for (const h of this.history.slice(-6)) {
        lines.push(`${h.role === 'user' ? 'User' : 'You'}: ${h.content.slice(0, 300)}`);
      }
    }

    if (this.inbox.length > 0) {
      lines.push('', '## Incoming Messages');
      for (const msg of this.inbox) {
        lines.push(`- ${msg.from}: "${msg.text}"`);
      }
      this.inbox = [];
    }

    return lines.join('\n');
  }

  /**
   * Run a turn. Streams ReAct events to onEvent callback.
   * ALWAYS resets status to 'idle' when done, even on error.
   */
  async run(message, onEvent) {
    // Force-reset if stuck (safety valve)
    if (this.status === 'running' && this.proc) {
      log.agent('warn', `${this.agentName} force-killing stuck process`);
      try { this.proc.kill('SIGTERM'); } catch {}
      this.proc = null;
    }

    this.status = 'running';
    this.history.push({ role: 'user', content: message, timestamp: Date.now() });

    // Init activity entry for this turn
    this._currentActivity = {
      thought: '',
      actions: [],
      response: '',
      timestamp: Date.now(),
      cost: null,
    };

    log.agent('info', `${this.agentName} turn start`, { message: message.slice(0, 200) });

    // Get observation (location + rotation + velocity)
    const obs = await getObservation(this.agentName);
    if (obs.location) {
      this.location = obs.location;
      this.positionUpdatedAt = Date.now();
      // Append to trajectory (max 200 points)
      this.trajectory.push({ loc: obs.location, rot: obs.rotation, ts: Date.now(), action: 'turn_start' });
      if (this.trajectory.length > 200) this.trajectory.shift();
    }
    if (obs.rotation)  this.rotation  = obs.rotation;
    if (obs.velocity)  this.velocity  = obs.velocity;
    if (obs.speed !== undefined) this.speed = obs.speed;

    // Consume any queued physics hit events at turn-start
    const hits = await getHitEvents(this.agentName);
    if (hits.length > 0) {
      this.collisionCount += hits.length;
      for (const h of hits) {
        this.recentCollisions.push({
          ts: h.ts || Date.now(),
          overlapping: [h.other],
          impulse: h.impulse,
          point: h.point,
          loc: obs.location,
        });
      }
      if (this.recentCollisions.length > 50) {
        this.recentCollisions.splice(0, this.recentCollisions.length - 50);
      }
    }

    // Nearby environment feedback (300 cm radius)
    const nearby = await getEnvironmentFeedback(this.agentName, 300);
    if (nearby.length > 0) {
      this.envFeedback.push({ ts: Date.now(), nearby, loc: obs.location });
      if (this.envFeedback.length > 20) this.envFeedback.shift();
    }
    log.agent('debug', `${this.agentName} obs loc=${obs.location} overlaps=${overlaps.length} nearby=${nearby.length}`);

    const systemPrompt = this._systemPrompt();

    try {
      await this._spawnClaude(message, systemPrompt, onEvent);
    } catch (err) {
      log.agent('error', `${this.agentName} run error: ${err.message}`);
      try { onEvent('error', { message: err.message }); } catch {};
    } finally {
      // ALWAYS reset status
      this.status = 'idle';
      this.proc = null;

      // Finalize activity
      if (this._currentActivity) {
        this.activity.push(this._currentActivity);
        if (this.activity.length > 20) this.activity.splice(0, this.activity.length - 20);
        this._currentActivity = null;
      }
    }
  }

  _spawnClaude(message, systemPrompt, onEvent) {
    return new Promise((resolve, reject) => {
      const args = [
        '-p', message,
        '--output-format', 'stream-json',
        '--include-partial-messages',
        '--verbose',
        '--dangerously-skip-permissions',
        '--mcp-config', MCP_CONFIG,
        '--append-system-prompt', systemPrompt,
      ];
      const model = process.env.CLAUDE_MODEL || '';
      if (model) args.push('--model', model);

      const env = { ...process.env };
      // Remove ALL Claude-related env vars to prevent SDK/extension mode interference
      for (const key of Object.keys(env)) {
        if (key.startsWith('CLAUDE')) delete env[key];
      }

      const proc = spawn(CLAUDE_BIN, args, {
        cwd: path.resolve(__dirname, '..'),
        env,
        stdio: ['ignore', 'pipe', 'pipe'],
      });
      this.proc = proc;

      let buf = '';
      let assistantText = '';
      let lastOutput = Date.now();
      let toolInProgress = false;   // true while a tool call is executing
      const act = this._currentActivity;

      // ── Observability (Phase 1.5) ────────────────────────────────────────
      // Track Claude subprocess lifecycle so we can diagnose silent hangs
      // (the Pedestrian_1 case: init received → 3.5min silence → SIGTERM with
      // no clue what Claude was doing). On idle-timeout we now dump:
      //   - elapsed since spawn / since last stream event
      //   - init→first-output gap (proxy for Claude API startup latency)
      //   - tail of unparsed stdout buffer
      //   - tail of recent stderr (was previously dropped after debug log)
      const spawnedAt = Date.now();
      let initAt = null;          // when system/init JSON arrived
      let firstStreamAt = null;   // when first stream_event arrived
      let lastStreamAt = null;    // when most recent stream_event arrived
      let stderrTail = '';        // ring buffer of recent stderr (last 4KB)

      const safeEvent = (type, data) => {
        try { onEvent(type, data); } catch (err) {
          log.agent('warn', `${this.agentName} onEvent error: ${err.message}`);
        }
      };

      const flush = (line) => {
        if (!line.trim()) return;
        let msg;
        try { msg = JSON.parse(line); } catch { return; }

        if (msg.type === 'system' && msg.subtype === 'init') {
          initAt = Date.now();
          log.agent('debug', `${this.agentName} session: ${msg.session_id} (init ${initAt - spawnedAt}ms after spawn)`);
          safeEvent('system', { sessionId: msg.session_id });

        } else if (msg.type === 'stream_event') {
          const now = Date.now();
          if (!firstStreamAt) {
            firstStreamAt = now;
            const initGap = initAt ? now - initAt : -1;
            log.agent('debug', `${this.agentName} first stream_event ${initGap}ms after init`);
          }
          lastStreamAt = now;
          const ev = msg.event || {};
          if (ev.type === 'content_block_delta' && ev.delta?.type === 'text_delta') {
            assistantText += ev.delta.text;
            if (act) act.thought += ev.delta.text;
            safeEvent('text', { delta: ev.delta.text });
          }
          if (ev.type === 'content_block_delta' && ev.delta?.type === 'thinking_delta') {
            if (act) act.thought += ev.delta.thinking;
            safeEvent('thinking', { delta: ev.delta.thinking });
          }
          if (ev.type === 'content_block_start' && ev.content_block?.type === 'tool_use') {
            const tc = ev.content_block;
            const displayName = tc.name.replace(/^mcp__\w+__/, '');
            if (act) act.actions.push({ tool: displayName, input: '', result: '', ok: null });
            toolInProgress = true;
            this.currentAction = displayName;
            safeEvent('tool_start', { id: tc.id, name: tc.name, displayName });
          }
          if (ev.type === 'content_block_delta' && ev.delta?.type === 'input_json_delta') {
            if (act && act.actions.length > 0) {
              act.actions[act.actions.length - 1].input += ev.delta.partial_json;
            }
            safeEvent('tool_input', { delta: ev.delta.partial_json });
          }

        } else if (msg.type === 'user') {
          toolInProgress = false;   // tool result arrived → no longer in-progress
          for (const p of (msg.message?.content || [])) {
            if (p.type === 'tool_result') {
              const text = Array.isArray(p.content)
                ? p.content.map(c => c.text || '').join('')
                : String(p.content || '');
              const isErr = p.is_error || false;
              if (act && act.actions.length > 0) {
                const last = act.actions[act.actions.length - 1];
                last.result = text.slice(0, 500);
                last.ok = !isErr;
              }
              safeEvent('tool_result', { toolUseId: p.tool_use_id, result: text.slice(0, 2000), isError: isErr });
            }
          }

        } else if (msg.type === 'result') {
          this.currentAction = null;
          if (act) {
            act.response = assistantText;
            act.cost = msg.total_cost_usd;
          }
          this.history.push({ role: 'assistant', content: assistantText, timestamp: Date.now() });
          log.agent('info', `${this.agentName} done`, { cost: msg.total_cost_usd, actions: act?.actions?.length });
          safeEvent('done', {
            isError: msg.is_error || msg.subtype === 'error_during_turn',
            costUsd: msg.total_cost_usd,
            text: assistantText,
          });
        }
      };

      // Idle timer — kill only when genuinely idle (no tool running)
      // Tool calls (MCP→UE) can easily take 2+ minutes, so skip check while tool is in progress
      const IDLE_LIMIT = 180000; // 3 min with no output AND no tool running
      const idleTimer = setInterval(() => {
        if (toolInProgress) {
          // Tool is executing — reset timer so we don't kill mid-tool
          lastOutput = Date.now();
          return;
        }
        if (Date.now() - lastOutput > IDLE_LIMIT) {
          clearInterval(idleTimer);
          // Dump everything we know about subprocess state before killing.
          // This is the diagnostic the Pedestrian_1 hang case was missing.
          const now = Date.now();
          const diag = {
            sinceSpawn: now - spawnedAt,
            sinceInit: initAt ? now - initAt : null,
            sinceFirstStream: firstStreamAt ? now - firstStreamAt : null,
            sinceLastStream: lastStreamAt ? now - lastStreamAt : null,
            initToFirstStream: (initAt && firstStreamAt) ? firstStreamAt - initAt : null,
            stdoutBufferTail: buf.slice(-512),
            stderrTail: stderrTail.slice(-1024),
          };
          log.agent('warn', `${this.agentName} idle timeout (${IDLE_LIMIT/1000}s) diag: ${JSON.stringify(diag)}`);
          proc.kill('SIGTERM');
        }
      }, 10000);

      proc.stdout.on('data', chunk => {
        lastOutput = Date.now();
        buf += chunk.toString();
        const lines = buf.split('\n');
        buf = lines.pop() ?? '';
        for (const ln of lines) flush(ln);
      });

      proc.stderr.on('data', d => {
        lastOutput = Date.now(); // stderr counts as activity
        const txt = d.toString();
        // Keep a rolling 4KB tail so we can attach it to idle-timeout diagnostics
        stderrTail = (stderrTail + txt).slice(-4096);
        const trimmed = txt.trim();
        if (trimmed) log.agent('debug', `${this.agentName} stderr: ${trimmed.slice(0, 200)}`);
      });

      proc.on('close', (code, signal) => {
        clearInterval(idleTimer);
        if (buf.trim()) flush(buf);
        log.agent('debug', `${this.agentName} exit code=${code} signal=${signal}`);
        resolve();
      });

      proc.on('error', (err) => {
        clearInterval(idleTimer);
        reject(err);
      });
    });
  }

  stop() {
    if (this.proc && !this.proc.killed) {
      this.proc.kill('SIGTERM');
      log.agent('info', `${this.agentName} stopped by user`);
    }
    this.status = 'idle';
    this.proc = null;
    // Also stop the agent in UE
    const type = this._resolveType();
    const typeDef = REGISTRY.agentTypes[type];
    const stopCmd = typeDef?.stopCmd || 'StopAgent';
    ucvCommand(`vbp ${this.agentName} ${stopCmd}`).catch(() => {});
  }

  toJSON() {
    const lastAct  = this.activity.length > 0 ? this.activity[this.activity.length - 1] : null;
    const lastTool = lastAct?.actions?.length > 0 ? lastAct.actions[lastAct.actions.length - 1].tool : null;
    return {
      agentName:        this.agentName,
      agentClass:       this.agentClass,
      location:         this.location,
      rotation:         this.rotation,
      velocity:         this.velocity,
      speed:            this.speed,
      positionUpdatedAt:this.positionUpdatedAt,
      status:           this.status,
      currentAction:    this.currentAction,
      lastAction:       lastTool,
      historyLength:    this.history.length,
      lastActivity:     lastAct,
      activityCount:    this.activity.length,
      collisionCount:   this.collisionCount,
      recentCollisions: this.recentCollisions.slice(-5),
      envFeedback:      this.envFeedback.slice(-3),
      memory:           this.memory.slice(-10),
      totalTurns:       this.totalTurns,
      totalCostUsd:     this.totalCostUsd,
      trajectoryLength: this.trajectory.length,
      // Send last 10 trajectory points for the card display
      trajectoryPreview: this.trajectory.slice(-10),
    };
  }

  // Called externally to get full trajectory for the detail panel
  getFullTrajectory() { return this.trajectory; }
}

// ---------------------------------------------------------------------------
// Controller
// ---------------------------------------------------------------------------

class AgentController {
  constructor() {
    this._sessions = new Map();
    this._publicChat = [];
    this._metricsHub = null;
    this._startPositionPoller();
  }

  /** Wire in MetricsHub so hit events are recorded in real-time */
  setMetricsHub(hub) { this._metricsHub = hub; }

  // Background poll: refresh position+rotation for all known agents every 3s.
  // Running agents get fresh obs at turn-start already; idle agents would otherwise
  // stay stale forever. Skip agents currently running (they handle their own obs).
  _startPositionPoller() {
    setInterval(async () => {
      const idle = [...this._sessions.values()].filter(s => s.status === 'idle');
      for (const session of idle) {
        try {
          const obs = await getObservation(session.agentName);
          // Fetch hit events (physics OnActorHit) — these are the real collisions
          // Returns events accumulated since last poll, then clears the queue
          const hits = await getHitEvents(session.agentName);
          const hasHit = hits.length > 0;

          if (obs.location) {
            session.location = obs.location;
            session.positionUpdatedAt = Date.now();
            // Trajectory point: include hit flag if any collisions this interval
            const tPoint = { loc: obs.location, rot: obs.rotation, ts: Date.now(), action: null };
            if (hasHit) tPoint.hit = true;
            session.trajectory.push(tPoint);
            if (session.trajectory.length > 200) session.trajectory.shift();
          }
          if (obs.rotation)  session.rotation  = obs.rotation;
          if (obs.velocity)  session.velocity  = obs.velocity;
          if (obs.speed !== undefined) session.speed = obs.speed;

          // Record collision events from physics hits
          if (hasHit) {
            session.collisionCount += hits.length;
            for (const h of hits) {
              session.recentCollisions.push({
                ts: h.ts || Date.now(),
                overlapping: [h.other],
                impulse: h.impulse,
                point: h.point,
                loc: obs.location,
              });
              // Notify MetricsHub immediately so chart updates in real-time
              if (this._metricsHub) this._metricsHub.recordAgentHit(session.agentName, h);
            }
            if (session.recentCollisions.length > 50) {
              session.recentCollisions.splice(0, session.recentCollisions.length - 50);
            }
          }
        } catch(pollErr) {
          // Broker handles reconnect; log persistent errors for diagnostics
          if (pollErr?.message && !pollErr.message.includes('timeout')) {
            log.agent('warn', `poller ${session.agentName}: ${pollErr.message?.slice(0,80)}`);
          }
        }
      }
    }, 3000);
  }

  getOrCreate(name, cls, location) {
    const isNew = !this._sessions.has(name);
    if (isNew) {
      this._sessions.set(name, new AgentSession({ agentName: name, agentClass: cls, location }));
      // Enable physics hit tracking as soon as the agent is registered
      enableHitTracking(name).catch(() => {});
    }
    const s = this._sessions.get(name);
    if (location) s.location = location;
    if (cls) s.agentClass = cls;
    return s;
  }

  get(name) { return this._sessions.get(name) || null; }
  list() { return [...this._sessions.values()].map(s => s.toJSON()); }
  stop(name) { const s = this._sessions.get(name); if (s) s.stop(); }
  stopAll() { for (const s of this._sessions.values()) s.stop(); }
  remove(name) { this.stop(name); this._sessions.delete(name); }

  /** Get full activity log for an agent */
  getActivity(name) {
    const s = this._sessions.get(name);
    return s ? s.activity : [];
  }

  syncWithContext(contextState) {
    if (!contextState) return;
    const seen = new Set();
    for (const a of contextState.agents || []) {
      seen.add(a.name);
      this.getOrCreate(a.name, a.cls, a.location);
    }
    for (const name of this._sessions.keys()) {
      // Don't remove agents that are currently running
      const session = this._sessions.get(name);
      if (!seen.has(name) && session?.status !== 'running') this.remove(name);
    }
  }

  // ── Communication ──

  sendMessage(from, to, text) {
    const msg = { from, to: to || 'all', text, timestamp: Date.now() };
    log.agent('info', `msg ${from} → ${to || 'all'}`, { text: text.slice(0, 100) });
    this._publicChat.push(msg);
    if (this._publicChat.length > 200) this._publicChat.splice(0, this._publicChat.length - 200);

    if (to && to !== 'all') {
      const target = this._sessions.get(to);
      if (target) target.inbox.push(msg);
    } else {
      for (const [name, session] of this._sessions) {
        if (name !== from) session.inbox.push(msg);
      }
    }
    return msg;
  }

  parseAndForwardMentions(fromAgent, text) {
    const mentions = text.match(/@(\w+)/g);
    if (!mentions) return [];
    const forwarded = [];
    for (const m of mentions) {
      const targetName = m.slice(1);
      if (this._sessions.has(targetName) && targetName !== fromAgent) {
        this.sendMessage(fromAgent, targetName, text);
        forwarded.push(targetName);
      }
    }
    return forwarded;
  }

  getPublicChat(since = 0) {
    return this._publicChat.filter(m => m.timestamp > since);
  }
}

module.exports = { AgentController, AgentSession };
