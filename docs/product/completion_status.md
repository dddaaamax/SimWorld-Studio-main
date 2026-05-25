# SimWorld Studio — Completion Status

> Honest audit of what works vs what is a UI placeholder.
> Last updated: 2026-05-11

---

## TL;DR

| Layer | Status |
|-------|--------|
| Scene Generation pipeline | **Complete** — real MCP + UE5 + VLM |
| UI shell (4 modes, nav, themes) | **Complete** — functional frontend |
| Embodied Agent panel | **Complete** — real SSE data |
| Task Generation backend | **Missing** — UI stub only |
| Agent Training backend | **Missing** — UI stub only |
| Co-evolution orchestration | **Missing** — UI stub only |
| Artifact chain persistence | **Partial** — ephemeral (lost on refresh) |

---

## What Is Fully Working

### 1. Scene Generation Pipeline ✅

The core research contribution is **complete end-to-end**.

```
User prompt
  → ChatPanel (SimCoder via Claude API)
  → MCP tool calls (spawn_blueprint_actor, setup_environment, etc.)
  → UE5 real-time scene modification (TCP port 55557)
  → Pixel Streaming viewport (WebRTC, live)
  → VLM verifier (Claude vision API)
  → Scene Inspector (rule checks + semantic score)
```

- SimCoder chat with streaming SSE response
- MCP tools: spawn, delete, transform, screenshot, setup_environment, verify_scene
- Real UE5 Pixel Streaming viewport
- Rule-based verifier: collision, vertical support, in-bounds
- VLM semantic verifier: prompt fidelity, aesthetics score
- Asset browser: real UE5 catalog
- Scene save/load/version
- Self-evolution: SimCoder writes new skills/tools from experience
- Skills CRUD (builtin + custom + learned)
- Tools CRUD

### 2. Embodied Agent Panel ✅

```
SSE stream (every ~3s from server)
  → AgentPanel: live position, heading, speed, action
  → Trajectory visualization (SVG map)
  → Collision counter
  → AgentAggregatePanelTabs: leaderboard, metrics tabs
```

- Real agent state via SSE
- Live trajectory map
- Collision detection integration
- Camera capture (agent POV screenshots)

### 3. Arena ✅

- Multi-agent battle logic (real backend)
- Claude judge evaluates outcomes
- Elo-based leaderboard (real persistence)
- Vote UI

### 4. UI Shell ✅

- PipelineStepper (4-mode nav with arrows)
- ArtifactChain (visual pipeline state)
- Dark / Light theme (CSS variables, no hardcoded colors)
- Mode-aware primary CTA button
- Library section (Skills + Tools + Arena)
- Results section (Gallery + Leaderboard)
- Settings modal (theme + mode switcher)
- All emoji replaced with SVG icons
- Shared primitives: Badge, Btn, ToggleBtn, TagChip, ModalOverlay, PageHeader, etc.

---

## What Is a UI Placeholder

### 1. Task Generation ❌

**UI exists.** Backend does not.

| Feature | Status |
|---------|--------|
| TaskGenPanel (left) | UI stub — fields render, no API calls |
| TaskInspectorPanel (right) | UI stub — hardcoded mock data |
| NavMesh overlay on viewport | Not implemented |
| Start/goal markers | Not implemented |
| Episode sampler | Not implemented |
| Path length filter | Not implemented |
| NavMesh connectivity check | Not implemented |
| Gym export | Not implemented |
| `POST /api/tasks/generate` | Endpoint does not exist |
| `GET /api/tasks/:id/episodes` | Endpoint does not exist |

**What is needed:**
- NavMesh query API in UE5 (via new MCP tool or Python script)
- PointNav/ObjectNav episode sampler
- Path validation (reachability, length filter)
- Gymnasium-style environment export
- New API endpoints: `/api/tasks/*`

### 2. Agent Training ❌

**UI exists.** Training engine does not.

| Feature | Status |
|---------|--------|
| TrainingConfigPanel (left) | UI stub — no wiring |
| Agent Monitor (right) | **Partially real** — reuses AgentPanel (live SSE) |
| Reward curve chart | Stub — mock data only |
| Success metrics (SPL, SoftSPL, nDTW) | Stub — hardcoded values |
| First-person RGB view | Stub — no observation capture |
| Depth view | Not implemented |
| Trajectory replay | Not implemented |
| Memory / rules accumulation | Partially real (self-evolution writes skills) |
| `POST /api/training/runs` | Endpoint does not exist |
| PPO / DAgger / BC training loop | Not implemented |

**What is needed:**
- Agent observation capture (RGB-D from UE5 camera)
- Rollout executor (step-by-step action loop)
- Trajectory logger (per-step state/action/reward)
- Metric aggregator (SR, SPL, SoftSPL, nDTW computation)
- Connect to actual RL framework (Habitat-baselines, SB3, or custom)
- New API endpoints: `/api/training/*`

### 3. Co-evolution Orchestration ❌

**UI exists.** Closed-loop orchestration does not.

| Feature | Status |
|---------|--------|
| CurriculumBuilderPanel (left) | UI stub — config fields render, no wiring |
| RoundInspectorPanel (right) | UI stub — hardcoded round history |
| Co-evolution loop canvas (center) | Static SVG diagram — no live updates |
| Curriculum round manager | Not implemented |
| Mastery gate (advance/hold) | Not implemented |
| Difficulty adapter | Not implemented |
| SimCoder feedback routing | Not implemented (SimCoder chat exists, routing does not) |
| `POST /api/coevolve/runs` | Endpoint does not exist |

**What is needed:**
- Curriculum orchestrator: round lifecycle, mastery gating, difficulty schedule
- Agent outcome → SimCoder prompt routing
- SimCoder adaptation: parse failure patterns → regenerate scene with harder parameters
- New API endpoints: `/api/coevolve/*`

### 4. Artifact Chain Persistence ⚠️

**Partially works.** State is ephemeral — lost on page refresh.

| Feature | Status |
|---------|--------|
| ArtifactChain component | Renders correctly |
| Scene artifact populated | Not wired (always null) |
| Task artifact populated | Not wired |
| Training artifact populated | Not wired |
| Curriculum artifact populated | Not wired |
| localStorage persistence | Not implemented |

**What is needed:**
- After successful scene generation: write `artifacts.scene` to state + localStorage
- After task generation: write `artifacts.task`
- After training run: write `artifacts.training`
- After curriculum run: write `artifacts.coevolve`

---

## Gap Analysis by Paper Contribution

The paper describes: scene generation → task generation → embodied agent training → co-evolution.

| Paper Component | Demo-ready | Production-ready |
|----------------|-----------|-----------------|
| Scene generation from prompt | ✅ Yes | ✅ Yes |
| VLM scene verification | ✅ Yes | ✅ Yes |
| Self-evolving skill library | ✅ Yes | ⚠️ Partial |
| Task set generation (NavMesh/PointNav) | ❌ No | ❌ No |
| Gym-style environment export | ❌ No | ❌ No |
| Embodied agent rollouts | ❌ No | ❌ No |
| SPL / SR / nDTW metrics | ❌ No | ❌ No |
| Co-evolution feedback loop | ❌ No | ❌ No |
| Curriculum difficulty adaptation | ❌ No | ❌ No |
| Full pipeline artifact chain | ❌ No | ❌ No |

---

## Recommended Implementation Order

Based on `codex_tasks.md`, these are the shortest paths to making each mode real:

### Phase 1 — Paper demo completeness (2–4 weeks)

**1a. Artifact Chain persistence** (1 day)  
Wire `onChatDone` → write scene artifact to `localStorage`.  
Unblocks the visual pipeline story.

**1b. Scene Inspector real data** (1–2 days)  
Pull actor count from SSE `SceneContext`.  
Show real verifier results instead of hardcoded mock.

**1c. Co-evolution loop canvas** (2–3 days)  
Replace static SVG with animated CSS loop diagram.  
Shows the SimCoder ↔ Agent feedback cycle visually.  
This is Figure 1 in the paper — must look good.

### Phase 2 — Task Generation (2–3 weeks)

**2a. NavMesh query MCP tool** (3–5 days)  
New UE5 Python script to query NavMesh reachability.  
Expose as `query_navmesh` MCP tool.

**2b. Episode sampler** (3–5 days)  
Sample start/goal pairs from NavMesh.  
Filter by path length, reachability.  
Return as `Episode[]` JSON.

**2c. Task Gen API + UI wiring** (2–3 days)  
`POST /api/tasks/generate` endpoint.  
Wire `TaskGenPanel` Generate button.  
Episode table in bottom drawer.

### Phase 3 — Agent Training (3–6 weeks, depends on RL framework choice)

**3a. Observation capture** (1 week)  
RGB + Depth capture from UE5 agent camera.  
Stream as base64 PNG per step.

**3b. Rollout executor** (1–2 weeks)  
Step loop: observe → agent decides → send action to UE5 → record.  
Trajectory logging to disk/DB.

**3c. Metric computation** (3–5 days)  
SR, SPL, SoftSPL, nDTW from trajectory data.  
Chart in Training bottom drawer.

### Phase 4 — Co-evolution (4–8 weeks, most complex)

**4a. Curriculum orchestrator** (1–2 weeks)  
Round lifecycle: sample episodes → run training → check mastery → advance/hold.

**4b. SimCoder feedback routing** (1–2 weeks)  
Parse agent failure patterns → format as SimCoder prompt → regenerate scene.  
This connects Scene Generation + Agent Training in a real loop.

**4c. Difficulty adapter** (1 week)  
Update difficulty axes (obstacle density, path length, etc.) based on mastery threshold.

---

## For Paper Submission

If the goal is a paper figure showing the full system:

**Minimum viable demo:**
1. ✅ Scene generation (already works)
2. Add animated co-evolution loop canvas (Phase 1c above — 2–3 days)
3. Add artifact chain showing "Scene v3 → TaskSet_500 → Run_042 → Curriculum_001" (Phase 1a — 1 day)
4. Mock the TaskSet and TrainingRun artifacts with plausible data
5. Screenshot each of the 4 modes → put in `docs/design/`

This gives you a complete-looking 4-mode UI for Figure 1/3 purposes while the actual ML pipeline catches up.

**For a live demo:**
Phases 1–3 are needed. Phase 4 is the hardest and should be done last.
