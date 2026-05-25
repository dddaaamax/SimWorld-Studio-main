# SimWorld Studio — Backend Server

Node.js / Express backend. Single process that owns the UE connection, Claude subprocess management, SSE push, and all API endpoints.

## Entry Point

`index.js` — starts everything. Key responsibilities:

1. **SSE broadcast** — pushes status every 3s (hash-diff: skips if nothing changed, keepalive at 9s)
2. **Asset catalog** — builds full tree from UE Python scan at startup (`/api/asset-tree`)
3. **Agent auto-discovery** — polls `vget /objects` every 5s in PIE, registers pawn-like actors
4. **Metrics sampling** — MetricsHub samples agent stats every 5s, pushes via SSE

---

## Modules

### `index.js` — Express API + Orchestration

All HTTP endpoints. Key groups:

| Prefix | Description |
|---|---|
| `/api/chat` | SSE stream: spawns Claude Code subprocess, pipes events to browser |
| `/api/agent-*` | Agent control: run, stop, broadcast, discover, track, camera, state |
| `/api/assets`, `/api/asset-tree` | Asset catalog (path-browsing, UE Python scan) |
| `/api/pie-start`, `/api/pie-status` | PIE mode control |
| `/api/context-snapshot` | Force re-sync scene state from UE |
| `/api/metrics`, `/api/metrics/scene-collision` | Time-series data |
| `/api/vlm-score` | VLM scene scoring via Claude Code CLI (reads image file) |
| `/api/ue-command` | UE console command passthrough |
| `/api/camera` | Set/get viewport camera |
| `/api/screenshot/latest` | Latest PNG from screenshot dir |

### `mcp-server.js` — MCP Tool Bridge

Runs as a subprocess spawned by Claude Code (via `--mcp-config mcp.json`). Implements all MCP tools:

| Tool | Description |
|---|---|
| `spawn_blueprint_actor` | Spawn BP actor with auto unique name suffix (`_SID`) |
| `spawn_actor` | Spawn static mesh actor |
| `spawn_agent` | Spawn embodied agent (requires PIE) |
| `delete_actor` / `delete_all_spawned` | Remove actors |
| `set_actor_transform` | Move/rotate/scale actor |
| `setup_environment` | Create sun, sky, fog, ground (must call first) |
| `take_screenshot` | Capture viewport PNG |
| `execute_python_script` | Run arbitrary UE Python |
| `list_assets` | Browse asset catalog by category |
| `verify_scene` | Claude evaluates scene placement quality |
| `agent_action` / `agent_rotate` / `agent_stop` | Agent control via UnrealCV |
| `get_agent_state` | Query agent position + rotation |

**Name collision prevention**: all spawned actor names get a `_SID` suffix (4-char session hex) so cross-map restarts never produce duplicate name crashes.

### `agent-controller.js` — Agent Session Manager

Manages per-agent state across the lifetime of the server:

```
AgentSession {
  agentName, agentClass
  location, rotation, velocity, speed   ← updated by background poller (3s)
  trajectory[]                          ← array of {loc, rot, ts, hit?}
  recentCollisions[]                    ← OnActorHit events with impulse
  collisionCount, totalTurns
  status: idle | running
  currentAction, lastAction
  history[], activity[], inbox[]
  _prevOverlaps: Set                    ← delta collision detection
}
```

**Background poller** (3s interval):
- Calls `getObservation()` → position, rotation, velocity, speed
- Calls `getHitEvents()` → physics collision events from `OnActorHit`
- Appends trajectory point (with `hit: true` flag if collision occurred)
- Notifies MetricsHub immediately on hit for real-time chart updates

**`enableHitTracking(name)`** — called on first registration, binds `OnActorHit` in plugin via `vset /object/{name}/track_hits`.

### `context-manager.js` — Scene State

Tracks the current scene (agents, objects, environment readiness, round number). Updated from:
- MCP tool results (spawn, delete, setup_environment)
- `snapshotScene()` calls (end of chat turn, manual sync)

Renders context for Claude prompts via `renderForPrompt()`.

### `metrics-hub.js` — Time-Series Data

Samples every 5s from all agent sessions:
- `collision[]`, `speed[]` (m/s), `turns[]`, `status[]`
- Up to 60 data points (5 min window)
- Cap: 50 concurrent agent series (prevents unbounded memory on churn)
- `recordAgentHit()` — called immediately on physics hit for real-time update
- `recordSceneCollisions()` — called after verifier collision check

### `unreal-bridge.js` — UnrealCV TCP Broker

Single persistent TCP connection to UnrealCV (port 9001). Features:
- **FIFO queue** — serializes all UCV commands, prevents race conditions
- **Retry with backoff** — 3 retries, 300/600ms delays
- **Queue deadline** — drops commands queued too long (configurable per call)
- **Auto-reconnect** — exponential backoff 500ms → 5s

### `context-manager.js` · `skills.js` · `scenes.js` · `arena.js`

Supporting modules for scene state, skill registry, saved scenes, and arena battles.

---

## SSE Payload (`/api/events`)

Pushed every 3s (or sooner on activity). Skipped if content hash unchanged (max 9s keepalive).

```json
{
  "context": { "agents": [...], "objects": [...], "environment": {...}, "round": N },
  "sessions": [{ "agentName": "...", "status": "idle|running", "location": [...], "rotation": [...],
                 "speed": N, "collisionCount": N, "currentAction": "...", "trajectoryPreview": [...] }],
  "activities": { "AgentName": [{ "thought": "...", "actions": [...], "cost": N }] },
  "chatLog": [...],
  "pieActive": true,
  "health": { "ueConnected": true, "mcpConnected": true },
  "metrics": { "series": { "AgentName": { "ts": [...], "collision": [...], "speed": [...] } },
               "sceneCollisions": [...] }
}
```

---

## Asset Catalog

Two sources (merged at startup):

1. **Static** `assets_full.json` — 125 buildings, trees, vehicles, street furniture, static meshes, agents, map templates, 17 allow-AI pack roots
2. **Live UE scan** (`/api/asset-tree/refresh`) — `EditorAssetLibrary.list_assets('/Game/PackName/', recursive=True, include_folder=True)` for all 17 packs; auto-triggered 2s after UE connects

Frontend loads tree once (`/api/asset-tree`), navigates locally (zero subsequent API calls).

---

## UCV Command Reference (Extended Plugin)

| Command | Args | Returns |
|---|---|---|
| `vget /object/{n}/velocity` | — | `vx vy vz` cm/s |
| `vget /object/{n}/overlaps` | — | JSON array of overlapping actor names |
| `vget /object/{n}/nearby {r}` | radius cm | JSON `[{name, distance, class}]` |
| `vset /object/{n}/track_hits` | — | Binds `OnActorHit`, starts event queue |
| `vget /object/{n}/hit_events` | — | JSON `[{ts, other, impulse, point}]`, clears queue |
| `vget /camera/actor/{n}/lit` | optional filename | PNG file path (renders from actor's camera) |
