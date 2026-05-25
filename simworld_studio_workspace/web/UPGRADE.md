# SimWorld Studio — Production Upgrade Plan
> Target: 50 concurrent users, each isolated UE instance, 30-min session lifecycle

---

## Architecture Overview

```
Browser (up to 50)
    │
    ├── HTTPS / WSS
    │
    ▼
[ Nginx / Reverse Proxy ]  ← rate limiting, SSL termination
    │
    ▼
[ Node.js Server  :3002 ]
    ├── Session Manager      ← slot pool, 30-min TTL, waiting queue
    ├── Chat Queue           ← p-queue, max 10 concurrent Claude procs
    ├── SSE Hub              ← per-session, heartbeat, auto-evict dead clients
    └── MCP Bridge           ← per-session UE port routing
    │
    ▼ (per session)
[ UE Instance Pool ]        ← max 10–50 slots (hardware dependent)
    ├── UE slot 0  :55559 / :8585 / :8586
    ├── UE slot 1  :55560 / :8587 / :8588
    ├── ...
    └── UE slot N  :55559+(N*2) / :8585+(N*2)
```

---

## Session Lifecycle

```
User arrives
    │
    ▼
[ Session Manager ]
    ├── Slots available? ──YES──► Assign slot → issue sessionToken (JWT)
    │                              Start 30-min TTL timer
    │
    └── Slots full? ───────NO───► Return 503 + queuePosition
                                  (or waiting room UI)

During session:
    ├── Every API call refreshes TTL (sliding window)
    ├── Heartbeat ping every 60s resets TTL
    └── Explicit /api/session/release frees slot immediately

Session expires (30-min idle or hard limit):
    ├── Kill Claude subprocess for this session
    ├── Close SSE connection
    ├── Release UE slot back to pool
    └── Clean up tmp files / context snapshots
```

---

## UE Instance Pool

Each UE instance runs with offset ports:
```
Slot 0:  MCP_PORT=55559  CIRRUS_HTTP=8585  CIRRUS_WS=8586
Slot 1:  MCP_PORT=55561  CIRRUS_HTTP=8587  CIRRUS_WS=8588
Slot N:  MCP_PORT=55559+(N*2)  CIRRUS_HTTP=8585+(N*2)  CIRRUS_WS=8586+(N*2)
```

Pool config (`.env`):
```
UE_POOL_SIZE=10          # max simultaneous UE instances (hardware limit)
UE_MAX_QUEUE=40          # waiting queue size before hard reject
SESSION_TTL_MS=1800000   # 30 minutes
SESSION_HARD_MAX_MS=3600000  # 1 hour absolute hard limit
MAX_CONCURRENT_CHATS=10  # Claude subprocess concurrency
```

---

## Changes by File

### NEW: server/session-manager.js
- Slot pool (Map: slotId → {sessionToken, userId, assignedAt, lastActivity})
- `acquire(userId)` → assigns slot, starts TTL
- `release(sessionToken)` → frees slot, triggers cleanup
- `heartbeat(sessionToken)` → resets sliding TTL
- Background sweeper every 60s evicts expired sessions
- Emits `session:expired` event for cleanup hooks

### NEW: server/ue-pool.js
- Maps slotId → UE port config
- `getPortsForSession(sessionToken)` → {mcpPort, cirrusHttp, cirrusWs}
- Validates UE instance health on assignment

### MODIFIED: server/index.js
```
BEFORE:  let activeChatProc = null
AFTER:   const chatProcs = new Map()  // sessionToken → ChildProcess

BEFORE:  const _sseClients = new Set()
AFTER:   const sseClients = new Map()  // sessionToken → {res, lastSeen}
         + heartbeat sweeper every 10s
         + write() wrapped in try/catch → remove dead clients

BEFORE:  const STUDIO_SESSION = crypto.randomUUID()  (global)
AFTER:   sessionToken from req.headers / query param per request

BEFORE:  immediate Claude spawn
AFTER:   chatQueue = new PQueue({ concurrency: MAX_CONCURRENT_CHATS })
```

### MODIFIED: server/scenes.js
```
+ Add ownerId field to scene schema
+ Validate req.sessionToken === scene.ownerId on GET/DELETE
+ Replace writeFileSync with write-queue (async-mutex per scene file)
```

### MODIFIED: server/skills.js / skill-selector.js
```
+ Wrap selectSkillsWithClaude() in try/catch with 15s timeout
+ On failure: log warn + return [] (proceed with no skills)
+ Never block main chat on skill selector failure
```

### MODIFIED: server/arena.js
```
+ Add 5-minute hard timeout to runBattle() via AbortController
+ Align state names: pending/generating/voting/result (match frontend)
+ Gallery: add cursor-based pagination, return total count
```

### MODIFIED: server/agent-controller.js
```
+ AgentSession persistence: write to arena_data/agent_sessions.json
  on every state change, reload on startup
+ CommHistory: key _publicChat by sessionToken not globally
+ Guard: if agent.status === 'running', reject new /api/agent-chat with 409
```

### MODIFIED: server/logger.js → REPLACED with pino
```js
import pino from 'pino';
export const logger = pino({
  level: process.env.LOG_LEVEL || 'info',
  base: { service: 'simworld-studio' },
}, pino.destination({ dest: './logs/app.ndjson', sync: false }));
// Structured JSON, async write, no interleaving
// Fields: timestamp, level, requestId, sessionToken, msg, ...data
```

### NEW: server/middleware/auth.js
```js
// Validates sessionToken from Authorization header or ?token= query
// Attaches req.sessionToken, req.slotId, req.ueConfig to request
// Returns 401 if token missing/expired
// Returns 503 if slot revoked (session expired mid-request)
```

### NEW: server/middleware/rate-limit.js
```js
import rateLimit from 'express-rate-limit';
// Global: 200 req/min per IP
// /api/chat: 10 req/min per session
// /api/arena/run: 2 req/min per session
```

### FRONTEND: src/contexts/ (split PollContext)
```
BEFORE:  single PollContext with {agents, objects, environment, chatLog, pieActive, health}
         → ALL consumers re-render on every 3s SSE update

AFTER:   AgentsContext    (agents[], sessions[])
         SceneContext     (objects[], environment, round)
         ChatLogContext   (chatLog[])
         StatusContext    (pieActive, health)
         → each consumer only re-renders when its slice changes
```

### FRONTEND: src/components/ChatPanel.jsx (extract from App.jsx)
```
+ useRef buffer for SSE text events
+ requestAnimationFrame batching: flush buffer every frame, not per event
+ React.memo on ChatMessage, ToolCallBlock
+ @tanstack/react-virtual for message list (skip DOM for off-screen messages)
```

### FRONTEND: Session Integration
```
+ On app load: POST /api/session/acquire → get sessionToken
+ Store sessionToken in sessionStorage (not localStorage — tab-scoped)
+ Add Authorization: Bearer <token> header to all API calls
+ Heartbeat: ping /api/session/heartbeat every 60s
+ Show countdown timer in topbar ("Session: 28:42 remaining")
+ On 5-min warning: toast "Your session expires in 5 minutes. Click to extend."
+ On expiry: full-page modal "Session ended. Refresh to start a new session."
+ On 503: waiting room UI with queue position
```

---

## File System → SQLite Migration (P3, optional)

Replace JSON file storage with `better-sqlite3` for:
- scenes (currently: `arena_data/scenes/*/scene.json`)
- gallery (currently: `arena_data/gallery.json`)
- agent sessions
- learned tools

Benefits: atomic writes, concurrent read-safe, no lockfile needed, query support.

```sql
CREATE TABLE sessions (token TEXT PK, slot_id INT, user_id TEXT, created_at INT, last_active INT);
CREATE TABLE scenes (id TEXT PK, owner_token TEXT, name TEXT, data JSON, created_at INT);
CREATE TABLE gallery (id TEXT PK, prompt TEXT, agent_name TEXT, screenshots JSON, tags JSON, elo REAL);
```

---

## Deployment Checklist (before going live)

- [ ] P0-1: chatProcs Map (per-session subprocess isolation)
- [ ] P0-2: SSE heartbeat + dead-client eviction
- [ ] P0-3: Per-session STUDIO_SESSION / studioSessionId
- [ ] P0-4: p-queue on Claude invocations (MAX_CONCURRENT_CHATS=10)
- [ ] P1-1: Scene ownerId + access control
- [ ] P1-2: async-mutex on file writes (or SQLite)
- [ ] P1-3: Skill selector fallback (never block main chat)
- [ ] P1-4: Arena 5-min SSE timeout
- [ ] Session Manager: slot pool, 30-min TTL, sweeper
- [ ] UE Pool: port-mapping per slot
- [ ] Auth middleware: validate sessionToken on all routes
- [ ] Rate limiting: global + per-route
- [ ] Pino logger: structured JSON, requestId tracing
- [ ] Health endpoint: include queue size, active sessions, slot usage
- [ ] Frontend: session acquire/heartbeat/expiry UI
- [ ] Frontend: PollContext split (4 fine-grained contexts)
- [ ] Frontend: React.memo on ChatMessage, ToolCallBlock, AgentCard, SkillItem, AssetCard
- [ ] Frontend: SSE text batching with rAF
- [ ] Nginx config: SSL, rate limit, proxy_pass :3002
- [ ] .env: UE_POOL_SIZE, SESSION_TTL_MS, MAX_CONCURRENT_CHATS
- [ ] Load test: 50 concurrent sessions with k6 or Artillery

---

## Environment Variables (.env)

```env
# Server
PORT=3002
NODE_ENV=production
LOG_LEVEL=info

# Session
UE_POOL_SIZE=10
UE_MAX_QUEUE=40
SESSION_TTL_MS=1800000
SESSION_HARD_MAX_MS=3600000
MAX_CONCURRENT_CHATS=10

# UE
UE_BASE_MCP_PORT=55559
UE_BASE_CIRRUS_HTTP=8585
UE_BASE_CIRRUS_WS=8586
UE_PORT_STRIDE=2

# Claude
ANTHROPIC_API_KEY=sk-ant-...
CLAUDE_MODEL=claude-sonnet-4-6

# JWT
JWT_SECRET=<random 64-byte hex>
JWT_EXPIRES_IN=3600s
```
