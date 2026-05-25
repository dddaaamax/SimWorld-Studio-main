# SimWorld Studio — Software Architecture

## System Overview

```
Browser (React+Vite)
  ↓ REST + SSE + WebSocket
Node.js API Server (Express)
  ↓ HTTP / stdin-stdout
MCP UE Tool Server
  ↓ TCP port 55557
Unreal Engine 5 Worker (local)
  ↓ Pixel Streaming WebRTC
Browser Viewport (iframe)
```

---

## Current Implementation

### Frontend — `simworld_studio_workspace/web/src/`

Single-page React 18 application built with Vite.

**Key files:**
```
src/
  App.jsx       — all components (9 000+ lines)
  index.css     — CSS variables + all styles
  main.jsx      — React root mount
  PixelStreamPlayer.jsx — UE5 viewport iframe wrapper
```

**State management:** React hooks only (useState, useEffect, useRef, useCallback).  
**Real-time:** EventSource (SSE) for server push; fetch for chat stream (SSE).  
**No Redux, no Zustand, no external state library.**

**Shared primitives (in App.jsx):**
- `Badge`, `SourceBadge`, `StatusBadge`
- `Btn`, `ToggleBtn`
- `TagChip`
- `ModalOverlay`, `ModalHeader`, `ModalFooter`
- `PageHeader`
- `Eyebrow`, `Field`
- `PipelineStepper`, `ArtifactChain`
- `StudioLanding`

**Mode components:**
- `TaskGenPanel`, `TaskInspectorPanel`
- `TrainingConfigPanel`
- `CurriculumBuilderPanel`, `RoundInspectorPanel`
- `SceneInspectorPanel`
- `LibraryPage`, `ResultsPage`

**Functional components (real backend):**
- `ChatPanel` — SimCoder chat with SSE streaming
- `CodingVerifierPanel` — rule + VLM verifier
- `AgentPanel` — live embodied agent state
- `AgentAggregatePanelTabs` — agent metrics/leaderboard
- `ViewportPanel` — UE5 Pixel Streaming viewport
- `AssetBrowser` — UE5 asset catalog
- `SceneManager` — saved scene browser
- `ArenaPage` — agent battle arena

---

### Backend — `simworld_studio_workspace/web/server/`

Node.js + Express server. No framework beyond Express.

**Key files:**
```
server/
  index.js          — Express app, REST routes, SSE poller
  mcp-server.js     — MCP protocol server (TCP 55557)
  agents.js         — agent session management + SSE aggregation
  arena.js          — arena battle logic (Claude judge)
  skills.js         — skill CRUD + storage
  scenes.js         — scene CRUD + JSON persistence
  context-manager.js— scene context tracker
  unreal-bridge.js  — UE5 MCP client (TCP)
  metrics-hub.js    — agent metrics aggregation
  session-manager.js— token + session lifecycle
  evolution.js      — self-evolution artifact tracking
```

**API base:** `/api`  
**SSE stream:** `GET /api/events`  
**Chat stream:** `POST /api/chat` (SSE response)  
**Port:** 9001 (configurable via `PORT` env)

---

### MCP UE Tool Server

Protocol: Model Context Protocol over TCP/stdin.  
Implementation: `server/mcp-server.js`  
UE bridge port: 55557 (configurable via `UNREAL_PORT`)

**Safe tools (enabled by default):**
```
list_assets
take_screenshot
get_actors_in_level
find_actors_by_name
set_actor_transform
spawn_actor
spawn_blueprint_actor
delete_actor
setup_environment
verify_scene
get_agent_state
agent_action
agent_rotate
agent_stop
```

**Restricted tools (require explicit approval):**
```
execute_python_script  — arbitrary Python in UE context
delete_all_spawned     — clears entire scene
```

---

### Unreal Engine 5 Worker

- Runs locally on Windows (packaged UE5 project)
- Pixel Streaming for viewport (WebRTC via Cirrus)
- Cirrus config: `simworld_studio_workspace/cirrus-config.json`
- Pixel Streaming HTML: `simworld_studio_workspace/web/public/ue-player.html`
- UE commands via TCP socket on port 55557
- Takes ~90s to launch to a playable state

---

## Core Data Entities

```
Project
  └─ Scene[]
       └─ SceneVersion[]
       └─ GenerationRun[]
            └─ ToolCall[]
            └─ VerificationReport[]

TaskSet
  └─ Episode[]
       └─ TrajectoryStep[]
  └─ ValidationReport[]
  └─ GymExport

Agent
  └─ TrainingRun[]
       └─ Rollout[]
       └─ Metrics{}
       └─ MemoryRule[]

CurriculumRun
  └─ CurriculumRound[]
       └─ DifficultyConfig{}
       └─ AgentOutcomeSummary{}
       └─ SimCoderAdaptation[]

Skill
Asset
```

---

## Real-Time Event Stream (SSE)

Server pushes state every ~3s via `GET /api/events`.

**Event payload shape:**
```json
{
  "sessions": [...],          // agent sessions
  "context": {
    "agents": [...],
    "objects": [...],
    "environment": { "ready": bool },
    "round": 0
  },
  "activities": {},           // per-agent tool call logs
  "chatLog": [...],           // inter-agent messages
  "pieActive": false,         // UE PIE running
  "health": {
    "ueConnected": bool,
    "mcpConnected": bool
  },
  "metrics": {                // from MetricsHub
    "series": {},
    "sceneCollisions": [],
    "sampledAt": 0,
    "intervalMs": 5000
  }
}
```

**Frontend contexts** (split for performance):
- `AgentsContext` — sessions + activities
- `SceneContext` — objects + environment + round
- `ChatLogContext` — inter-agent messages
- `StatusContext` — pieActive + health
- `MetricsContext` — time-series metrics
- `SyncContext` — SSE health + stale agent tracking

---

## Planned Service Extensions

For full pipeline support (currently stubs in the UI):

### Task Generation Service
- NavMesh query API
- PointNav/ObjectNav episode sampler
- Path length filter
- Solvability checker
- Gym export

### Agent Training Service
- Rollout executor
- Trajectory logger
- Metric aggregator (SR, SPL, SoftSPL, nDTW)
- Memory/rule accumulator

### Co-evolution Orchestrator
- Curriculum round manager
- Mastery gate (advance/hold)
- Difficulty adapter
- SimCoder feedback loop

These services are currently UI placeholders.  
Real implementation requires backend extensions beyond the Node.js server.

---

## Local Development

```bash
# Install
cd simworld_studio_workspace/web
npm install

# Start frontend
npx vite

# Start backend (separate terminal)
cd server
node index.js

# Start UE5 (Windows, ~90s startup)
# Launch packaged SimWorld UE5 project

# Build for production
npx vite build --mode development
```

**Ports:**
- Frontend dev: 5173
- API server: 9001
- MCP UE bridge: 55557
- UE Pixel Streaming: 8888 (Cirrus)

---

## Environment Variables

```bash
PORT=9001                    # API server port
ANTHROPIC_API_KEY=sk-...     # Claude API key (SimCoder)
UNREAL_HOST=127.0.0.1        # UE5 host
UNREAL_PORT=55557            # UE5 MCP port
CIRRUS_PORT=8888             # Pixel Streaming port
```
