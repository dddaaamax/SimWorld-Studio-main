'use strict';

const fs = require('fs');
const pathMod = require('path');

// ---------------------------------------------------------------------------
// Scene entity classification — driven by agent-registry.json
// ---------------------------------------------------------------------------

const REGISTRY = JSON.parse(fs.readFileSync(pathMod.resolve(__dirname, 'agent-registry.json'), 'utf-8'));

// Build patterns from registry + some universal fallbacks
const AGENT_PATTERNS = [
  ...(REGISTRY.classifyPatterns || []).map(p => new RegExp(p.regex, 'i')),
  /^Agent_/i,
  /^Pedestrian_/i,
  /^Humanoid_/i,
  /^TestAgent_/i,
];

// Blueprint short-id → semantic category for objects
const BLUEPRINT_CATEGORY = {
  BP_Building_01: 'building', BP_Building_02: 'building',
  BP_Building_03: 'building', BP_Building_04: 'building',
  BP_Building_05: 'building', BP_Building_06: 'building',
  BP_Tree1: 'tree', BP_Tree2: 'tree', BP_Tree3: 'tree',
  BP_Tree4: 'tree', BP_Tree5: 'tree', BP_Tree6: 'tree',
  BP_Hydrant: 'furniture',
  BP_Trash_bin_a: 'furniture', BP_Trash_bin_b: 'furniture', BP_Trash_can: 'furniture',
  BP_Table: 'furniture', BP_Table2: 'furniture', BP_Table3: 'furniture',
  BP_RoadBlocker: 'furniture', BP_RoadCone: 'furniture', BP_Couch: 'furniture',
  BP_Scooter_01: 'vehicle', BP_Scooter_02: 'vehicle',
  BP_Scooter_03: 'vehicle', BP_Scooter_04: 'vehicle',
  BP_Cart: 'vehicle', BP_Cart2: 'vehicle',
};

// Prefixes of actor labels that belong to level infrastructure — skip these.
const INFRA_PREFIXES = [
  'Floor', 'SM_', 'SkySphere', 'Light', 'Atmo', 'Fog', 'Sky',
  'Camera', 'Player', 'Default', 'GameMode', 'WorldSettings',
  'Brush', 'Volume', 'Note', 'AbstractNav', 'NavMesh',
];

function blueprintShortId(cls) {
  // '/Game/.../BP_Foo.BP_Foo_C' → 'BP_Foo'
  return String(cls || '').split('/').pop().replace(/\.[^.]+$/, '').replace(/_C$/, '');
}

function classifyActor(label, cls) {
  const shortId = blueprintShortId(cls);
  const isAgent = AGENT_PATTERNS.some((re) => re.test(label) || re.test(shortId));
  const category = BLUEPRINT_CATEGORY[shortId]
    || (isAgent ? 'agent' : 'object');
  return { isAgent, category };
}

function isInfra(label) {
  return INFRA_PREFIXES.some((p) => label.startsWith(p));
}

function fmtVec(loc) {
  if (!Array.isArray(loc) || loc.length < 3) return null;
  return `(${loc.map((v) => Math.round(Number(v) || 0)).join(', ')})`;
}

// ---------------------------------------------------------------------------
// Data structures
// ---------------------------------------------------------------------------

/**
 * A single entity tracked in the scene.
 *
 * @property {string}        name      - Actor label / spawn name
 * @property {string}        cls       - Blueprint short id or mesh name
 * @property {string}        category  - Semantic category (building/tree/vehicle/…)
 * @property {number[]|null} location  - [x, y, z] in UE centimetres, or null
 */
class SceneEntity {
  constructor({ name, cls, category, location }) {
    this.name = name;
    this.cls = cls;
    this.category = category;
    this.location = location || null;
  }
}

/**
 * Full snapshot of the scene at a point in time.
 *
 * @property {SceneEntity[]} agents      - AI / autonomous / mobile entities
 * @property {SceneEntity[]} objects     - Static environment objects
 * @property {object}        environment - Global scene flags
 * @property {number}        round       - Turn counter (incremented each chat turn)
 * @property {string|null}   updatedAt   - ISO timestamp of last snapshot
 */
class SceneState {
  constructor() {
    this.agents = [];
    this.objects = [];
    this.environment = {
      ready: false,   // true after setup_environment succeeds
    };
    this.round = 0;
    this.updatedAt = null;
  }
}

// ---------------------------------------------------------------------------
// ContextManager
// ---------------------------------------------------------------------------

/**
 * Manages per-session scene state and renders it as structured context for
 * injection into the agent's prompt.
 *
 * Design intent
 * -------------
 * - `updateFromSnapshot(sessionId, actorsPayload)` is the single write path;
 *   call it after each agent round with the raw `get_actors_in_level` result.
 * - `renderForPrompt(sessionId)` is the read path used during prompt assembly.
 *   Currently produces a text block; swap/extend for multimodal later.
 * - Session state is keyed by Claude session_id.  Before the first round
 *   resolves a real session_id, use `'__new__'` as the key; call
 *   `resolveSession(newId)` once the real id is known to migrate the entry.
 */
class ContextManager {
  constructor() {
    /** @type {Map<string, SceneState>} */
    this._sessions = new Map();
  }

  // ---- Internal helpers ---------------------------------------------------

  _state(sessionId) {
    const key = String(sessionId || '__new__');
    if (!this._sessions.has(key)) {
      this._sessions.set(key, new SceneState());
    }
    return this._sessions.get(key);
  }

  // ---- Write path ---------------------------------------------------------

  /**
   * Populate scene state from the raw response of `get_actors_in_level`.
   * Expected payload shape (either root or under `.result`):
   *   { actors: [{ name, label, class, location }, …] }
   */
  updateFromSnapshot(sessionId, payload) {
    const state = this._state(sessionId);
    const raw = payload && typeof payload === 'object' ? payload : {};
    const actors = raw.result?.actors || raw.actors || [];

    const agents = [];
    const objects = [];

    for (const a of actors) {
      const label = String(a.label || a.name || '');
      if (!label || isInfra(label)) continue;

      const cls = blueprintShortId(a.class || '');
      const loc = Array.isArray(a.location) ? a.location : null;
      const { isAgent, category } = classifyActor(label, a.class || '');
      const entity = new SceneEntity({ name: label, cls, category, location: loc });

      (isAgent ? agents : objects).push(entity);
    }

    // Preserve agents that were explicitly added via addActor (e.g. spawn_agent)
    // but not seen in the MCP snapshot (PIE agents are invisible to editor snapshot)
    const snapshotNames = new Set([...agents, ...objects].map(e => e.name));
    for (const existingAgent of state.agents) {
      if (!snapshotNames.has(existingAgent.name)) {
        agents.push(existingAgent);
      }
    }

    state.agents = agents;
    state.objects = objects;
    state.updatedAt = new Date().toISOString();
  }

  /**
   * Add a single actor from a tool result (spawn_blueprint_actor / spawn_actor).
   * Called in real-time as tool results stream in — no UE TCP needed.
   */
  addActor(sessionId, { name, cls, category, location }) {
    const state = this._state(sessionId);
    const { isAgent: autoAgent, category: derivedCat } = classifyActor(name, cls || '');
    const cat = category || derivedCat;
    const isAgent = autoAgent || category === 'agent';
    const entity = new SceneEntity({ name, cls: blueprintShortId(cls || ''), category: cat, location: location || null });
    // Replace if same name already exists
    state.agents = state.agents.filter((a) => a.name !== name);
    state.objects = state.objects.filter((o) => o.name !== name);
    (isAgent ? state.agents : state.objects).push(entity);
    state.updatedAt = new Date().toISOString();
  }

  /** Remove a single actor by name (delete_actor). */
  removeActor(sessionId, name) {
    const state = this._state(sessionId);
    state.agents = state.agents.filter((a) => a.name !== name);
    state.objects = state.objects.filter((o) => o.name !== name);
    state.updatedAt = new Date().toISOString();
  }

  /** Clear all spawned actors (delete_all_spawned). */
  clearAllSpawned(sessionId) {
    const state = this._state(sessionId);
    state.agents = [];
    state.objects = [];
    state.updatedAt = new Date().toISOString();
  }

  /** Mark the environment as initialized (called when setup_environment runs). */
  setEnvironmentReady(sessionId) {
    const state = this._state(sessionId);
    state.environment.ready = true;
    if (!state.updatedAt) state.updatedAt = new Date().toISOString();
  }

  /** Increment the round counter at the start of each agent turn. */
  beginRound(sessionId) {
    this._state(sessionId).round += 1;
  }

  /**
   * Migrate state accumulated under `'__new__'` to the real session_id once
   * Claude Code returns it in the `system/init` event.
   */
  resolveSession(realSessionId, previousSessionId) {
    if (!realSessionId) return;
    const key = String(realSessionId);
    // Migrate __new__ state
    if (this._sessions.has('__new__') && !this._sessions.has(key)) {
      this._sessions.set(key, this._sessions.get('__new__'));
      this._sessions.delete('__new__');
    }
    // Carry over state from previous session (when not using --resume,
    // each turn creates a new session but the scene state persists)
    if (previousSessionId && previousSessionId !== realSessionId) {
      const prevKey = String(previousSessionId);
      const prev = this._sessions.get(prevKey);
      if (prev && !this._sessions.has(key)) {
        // Deep copy the previous state to the new session
        const newState = new SceneState();
        newState.agents = [...prev.agents];
        newState.objects = [...prev.objects];
        newState.environment = { ...prev.environment };
        newState.round = prev.round;
        newState.updatedAt = prev.updatedAt;
        this._sessions.set(key, newState);
      }
    }
  }

  // ---- Read path ----------------------------------------------------------

  /** Return the raw SceneState for a session (null if unknown). */
  getState(sessionId) {
    const key = String(sessionId || '__new__');
    const state = this._sessions.get(key);
    if (state) return state;
    // Fallback: return the most recently updated session's state
    let latest = null;
    let latestTime = null;
    for (const s of this._sessions.values()) {
      if (s.updatedAt && (!latestTime || s.updatedAt > latestTime)) {
        latest = s;
        latestTime = s.updatedAt;
      }
    }
    return latest;
  }

  /**
   * Render the current scene state as a text block suitable for injection
   * into `--append-system-prompt`.
   *
   * Returns null if no snapshot exists yet (first turn before any round has
   * completed), so callers can skip injection cleanly.
   *
   * Prompt ordering rule: this block is placed AFTER the static system
   * prompt and skill docs (rarely change) but BEFORE user feedback and tool
   * observations (change every turn).
   */
  renderForPrompt(sessionId) {
    const state = this.getState(sessionId);
    if (!state || state.updatedAt === null) return null;

    const lines = [];

    lines.push(`## Current Scene State  (round ${state.round})`);
    lines.push(`Environment: ${state.environment.ready ? 'initialized' : 'not yet initialized'}`);
    lines.push('');

    // --- Agents ---
    lines.push(`### Agents  (${state.agents.length} total)`);
    if (state.agents.length === 0) {
      lines.push('(none)');
    } else {
      for (const a of state.agents) {
        const loc = fmtVec(a.location);
        lines.push(`- ${a.name}  [${a.cls || a.category}]${loc ? `  @ ${loc}` : ''}`);
      }
    }

    lines.push('');

    // --- Objects, grouped by category (cap at 200 to avoid ENAMETOOLONG) ---
    const MAX_OBJ_DISPLAY = 200;
    lines.push(`### Objects  (${state.objects.length} total${state.objects.length > MAX_OBJ_DISPLAY ? `, showing first ${MAX_OBJ_DISPLAY}` : ''})`);
    if (state.objects.length === 0) {
      lines.push('(none)');
    } else {
      const displayObjs = state.objects.slice(0, MAX_OBJ_DISPLAY);
      const byCategory = new Map();
      for (const o of displayObjs) {
        if (!byCategory.has(o.category)) byCategory.set(o.category, []);
        byCategory.get(o.category).push(o);
      }
      for (const [cat, items] of byCategory) {
        lines.push(`  ${cat}s (${items.length}):`);
        for (const o of items) {
          const loc = fmtVec(o.location);
          lines.push(`  - ${o.name}  [${o.cls || cat}]${loc ? `  @ ${loc}` : ''}`);
        }
      }
    }

    // Hard cap on total rendered context size to prevent ENAMETOOLONG
    const result = lines.join('\n');
    return result.length > 8000 ? result.slice(0, 8000) + '\n...(truncated)' : result;
  }
}

module.exports = { ContextManager, SceneState, SceneEntity };
