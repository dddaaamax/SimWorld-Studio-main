# SimWorld Studio — API Contract

Base URL: `http://localhost:9001/api`

All requests/responses are JSON unless noted.

---

## Health

```
GET /api/health
→ { ueConnected: bool, mcpConnected: bool, version: string }
```

---

## Session

```
GET /api/session
→ { sessionId: string, dev: bool, ttl: number | null }

POST /api/session/refresh
→ { sessionId: string, ttl: number }
```

---

## Chat (SimCoder)

```
POST /api/chat
Body: {
  message: string,
  sessionId: string,
  skills?: string[],           // skill IDs to include
  feedback?: string,           // optional verifier feedback
  skillSelectionMode?: "auto" | "manual"
}
→ Server-Sent Events stream:
  data: { type: "text",    content: string }
  data: { type: "tool",    name: string, input: object }
  data: { type: "result",  name: string, content: string, isError: bool }
  data: { type: "done" }
  data: { type: "error",   message: string }
```

---

## Real-Time State (SSE)

```
GET /api/events?token=<session_token>
→ Server-Sent Events (text/event-stream)

Each event data:
{
  "sessions": [
    {
      "agentName": string,
      "status": "idle" | "running" | "done" | "error",
      "location": [x, y, z],
      "heading": number,
      "currentAction": string | null,
      "collisionCount": number,
      "speed": number
    }
  ],
  "context": {
    "agents": [...],
    "objects": [{ "name": string, "class": string, "location": [...] }],
    "environment": { "ready": bool },
    "round": number
  },
  "activities": {
    "<agentName>": [
      { "tool": string, "ok": bool | null, "timestamp": number }
    ]
  },
  "chatLog": [
    { "from": string, "to": string, "text": string, "timestamp": number }
  ],
  "pieActive": bool,
  "health": { "ueConnected": bool, "mcpConnected": bool },
  "metrics": {
    "series": { "<agentName>": { "sr": number[], "collisions": number[] } },
    "sceneCollisions": [...],
    "sampledAt": number,
    "intervalMs": number
  }
}
```

---

## Assets

```
GET /api/assets
→ {
    categories: {
      buildings: [...],
      trees: [...],
      vehicles: [...],
      street_furniture: [...],
      roads: [...],
      static_meshes: [...],
      agents: [...],
      maps: [...]
    },
    total: number
  }
```

---

## Scenes

```
GET /api/scenes
→ Scene[]

POST /api/scenes
Body: { name: string, description: string, tags: string[], objects: object[] }
→ Scene

DELETE /api/scenes/:id
→ { success: bool }
```

Scene shape:
```typescript
{
  id: string,
  name: string,
  description: string,
  tags: string[],
  objects: object[],
  createdAt: number,
  version: number
}
```

---

## Skills

```
GET /api/skills
→ Skill[]

GET /api/skills/:id
→ Skill (with content)

POST /api/skills
Body: {
  id: string,          // lowercase, a-z0-9_
  name: string,
  description: string,
  tags: string[],
  content: string      // Markdown
}
→ Skill

DELETE /api/skills/:id
→ { success: bool }
```

Skill shape:
```typescript
{
  id: string,
  name: string,
  description: string,
  tags: string[],
  source: "builtin" | "custom" | "learned",
  version: string,
  author: string,
  dependencies: string[],
  content?: string    // included in GET /:id
}
```

---

## MCP Tools (via `/api/mcp/*` proxy)

These are forwarded to the MCP UE bridge.

```
POST /api/mcp/list_assets
POST /api/mcp/take_screenshot
POST /api/mcp/get_actors_in_level
POST /api/mcp/find_actors_by_name
POST /api/mcp/spawn_actor
POST /api/mcp/spawn_blueprint_actor
POST /api/mcp/delete_actor
POST /api/mcp/set_actor_transform
POST /api/mcp/setup_environment
POST /api/mcp/verify_scene
POST /api/mcp/execute_python_script  ← RESTRICTED
POST /api/mcp/delete_all_spawned     ← RESTRICTED
```

---

## Arena

```
POST /api/arena/run
Body: { prompt: string, agentIds: string[] }
→ SSE stream:
  data: { type: "battle_created", battleId: string }
  data: { type: "progress", agentId: string, step: number }
  data: { type: "complete", battle: Battle }

GET /api/arena/battles
→ Battle[]

POST /api/arena/battles/:id/vote
Body: { vote: string }  // agentId of winner
→ { success: bool }
```

---

## Leaderboard

```
GET /api/leaderboard
→ {
    entries: [
      { agentId: string, name: string, elo: number, wins: number, losses: number }
    ]
  }
```

---

## Gallery

```
GET /api/gallery?limit=100&sort=newest
→ {
    scenes: [
      { id, name, tags, thumbnail, createdAt, sharedAt }
    ]
  }

POST /api/gallery/share/:sceneId
→ { success: bool, galleryId: string }
```

---

## Planned Endpoints (not yet implemented)

### Task Generation
```
POST /api/tasks/generate
Body: { sceneId, taskType, episodes, pathRange, successRadius }
→ { taskSetId, status: "queued" }

GET /api/tasks/:taskSetId
→ TaskSet

GET /api/tasks/:taskSetId/episodes
→ Episode[]

POST /api/tasks/:taskSetId/validate
→ ValidationReport
```

### Training
```
POST /api/training/runs
Body: { sceneId, taskSetId, agentConfig, trainingConfig }
→ { runId, status: "queued" }

GET /api/training/runs/:runId
→ TrainingRun

GET /api/training/runs/:runId/metrics
→ { sr: number[], spl: number[], reward: number[], steps: number[] }

GET /api/training/runs/:runId/trajectories
→ Trajectory[]
```

### Co-evolution
```
POST /api/coevolve/runs
Body: { taskTemplate, agentId, curriculumConfig }
→ { runId, status: "queued" }

GET /api/coevolve/runs/:runId
→ CurriculumRun

GET /api/coevolve/runs/:runId/rounds
→ CurriculumRound[]
```
