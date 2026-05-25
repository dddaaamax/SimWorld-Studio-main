# SimWorld Studio — Codex Task Queue

Tasks are ordered by priority. Do one PR at a time.  
Read `AGENTS.md` before starting any task.

---

## Task 1 — Task Generation mode (real NavMesh + episode UI)

**Status:** UI stub exists. Backend not integrated.

**Context:**  
`TaskGenPanel` and `TaskInspectorPanel` are placeholder components in `App.jsx`.  
The Task Generation mode needs a real episode table, NavMesh visualization, and backend API calls.

**Requirements:**
- Left panel (`TaskGenPanel`): wire up Generate Tasks button to `POST /api/tasks/generate`
- Center viewport: add NavMesh overlay toggle (blue mesh on top of scene)
- Center viewport: render start/goal markers as colored SVG circles
- Right panel (`TaskInspectorPanel`): show real episodes from `GET /api/tasks/:id/episodes`
- Bottom drawer "Episodes" tab: paginated episode table with columns: Episode#, Start, Goal, Path length, Status
- Bottom drawer "Validation" tab: show validation report from `POST /api/tasks/:id/validate`
- Bottom drawer "Gym Preview" tab: show Python API code block

**Mock data acceptable** for NavMesh overlay and episode data while backend is being built.

**Acceptance:**
- `npx vite build --mode development` passes
- Task Gen mode renders without errors
- Episode table shows at least mock data
- "Generate Tasks" button shows loading state

---

## Task 2 — Agent Training mode (real metrics + trajectory view)

**Status:** UI stub exists. AgentPanel renders live data but TrainingConfig is placeholder.

**Context:**  
`TrainingConfigPanel` is a stub in `App.jsx`.  
The right panel already uses `AgentPanel` (real data).  
Need to complete the training config wiring and add metrics charts.

**Requirements:**
- Left panel (`TrainingConfigPanel`): wire up Start/Stop Training to backend
- Center viewport: add view mode tabs: "Agent RGB" | "Depth" | "Top-down" | "Replay"
  - "Agent RGB" and "Depth" show placeholder thumbnail or actual Pixel Stream view
  - "Top-down" shows existing viewport
  - "Replay" is placeholder
- Bottom drawer "Reward Curve" tab: line chart of reward over steps (use `recharts` or plain SVG)
- Bottom drawer "Success Metrics" tab: table with SR, SPL, SoftSPL, nDTW columns
- Bottom drawer "Failures" tab: list of failed episodes with failure type labels
- Bottom "Memory / Rules" tab: list of accumulated agent rules

**Mock data acceptable** for chart data.

**Acceptance:**
- Training mode renders without errors
- Reward curve chart visible with mock data
- Success metrics table visible

---

## Task 3 — Co-evolution loop canvas (visual)

**Status:** UI stub shows plain text in center. No visual loop diagram.

**Context:**  
The center panel in co-evolution mode should show an animated loop canvas:  
SimCoder → Environment & Tasks → Agent Outcomes → Difficulty Adapter → SimCoder  
This is the flagship visual of the product (Figure 1 in the paper).

**Requirements:**
- Replace the placeholder center text with an SVG/CSS loop diagram
- Four nodes: SimCoder | Env + Tasks | Agent | Difficulty Adapter
- Animated arrows showing flow direction (CSS animation, pulsing)
- Each node shows current status (idle / active / done)
- Below diagram: 2-column grid showing current batch of generated scene thumbnails
- Agent outcome summaries (SR, key failures) below the diagram
- No real animation blocking needed — static diagram with CSS pulse is acceptable

**Acceptance:**
- Co-evolution mode renders the loop canvas visually
- Arrows and nodes are visible and styled with theme colors

---

## Task 4 — Artifact chain persistence

**Status:** `ArtifactChain` component shows hardcoded null state (no artifacts).

**Context:**  
The artifact chain in the navbar should show what has been built:  
`[Scene v3] → [TaskSet_500] → [TrainingRun_042] → [Curriculum_001]`  
Currently `artifacts` state is always `{ scene: null, task: null, training: null, coevolve: null }`.

**Requirements:**
- After a successful Generate Scene → store scene artifact in `artifacts.scene`
  - Trigger: `ChatPanel` `onChatDone` callback
  - Store: `{ name: "Scene v3", sceneId, version }`
- After Generate Tasks → store task artifact in `artifacts.task`
- After Start Training → store training artifact in `artifacts.training`
- After Run Co-evolution → store curriculum artifact in `artifacts.coevolve`
- Persist to `localStorage` (key: `sw_artifacts`)
- Load from localStorage on mount
- Each artifact chip in ArtifactChain shows: `[icon] Name` and is clickable to jump to that mode

**Acceptance:**
- After chat completes, scene chip shows in artifact chain
- Reloading the page restores artifact chain state

---

## Task 5 — Scene Inspector real data

**Status:** `SceneInspectorPanel` shows hardcoded mock values (42 actors, Afternoon, etc.)

**Context:**  
The right panel in Scene Generation mode wraps `CodingVerifierPanel` at the bottom but shows fake health data at the top.  
It should pull real data from the SSE scene context.

**Requirements:**
- Scene Health section: pull from `verify_scene` MCP tool result (when available)
  - If no verification run yet: show "— Not verified" for each check
  - After verification: show PASS / FAIL / score per check
- Scene Summary section: pull from `SceneContext` (SSE):
  - Actor count: `scene.objects.length`
  - Environment ready: `scene.environment.ready`
  - Round: `scene.round`
- Selected Actor section: show info when user clicks an actor in the viewport
  - Use `window.postMessage` from Pixel Streaming iframe for actor selection events
  - Fallback: show "Click an actor in the viewport to inspect"
- Output artifact pill: show current scene name + version from `SceneManager`

**Acceptance:**
- Real actor count from SSE displays in Scene Summary
- Health section shows "Not verified" by default
- After running verifiers, health section updates

---

## Task 6 — Dark/Light theme polish

**Status:** Dark theme is functional. Some hardcoded colors remain in sub-components.

**Audit and fix:**
- Search for any remaining `#ffffff`, `#f4f6fa`, `#1e293b`, `#475569` in `App.jsx`
- Replace with CSS variables
- Ensure Library page (Skills/Tools/Arena) fully themes correctly
- Ensure Results page (Gallery/Leaderboard) fully themes correctly
- Ensure all modal backgrounds use `var(--panel)` not hardcoded white
- Test by switching to Light theme and checking all pages

**Check command:**
```bash
grep -n '"#[0-9a-fA-F]\{6\}"' src/App.jsx | grep -v "TAG_COLORS\|AGENT_COLORS\|CATEGORY_COLORS\|0b1220" | head -30
```

---

## Task 7 — TypeScript migration (optional, future)

**Status:** Not started.

**When to do:** Only after Tasks 1–6 are complete and the UI is stable.

**Scope:**
- Add TypeScript + `tsconfig.json` to `simworld_studio_workspace/web/`
- Migrate `App.jsx` → `App.tsx` incrementally (start with shared primitives)
- Add type definitions for API responses, SSE events, MCP tool results
- Do NOT change any logic — types only

---

## Completed Tasks

- [x] Pipeline stepper (4-mode nav with arrows)
- [x] Artifact chain component
- [x] Mode-aware primary CTA button
- [x] Scene Generation: Intent+SimCoder left, Scene Inspector right
- [x] Task Generation: TaskGenPanel left, TaskInspectorPanel right (placeholder)
- [x] Agent Training: TrainingConfig left, Agent Monitor right
- [x] Co-evolution: Curriculum Builder left, Round Inspector right
- [x] Shared primitives: Badge, Btn, ToggleBtn, TagChip, ModalOverlay, PageHeader, etc.
- [x] Library page (Skills + Tools + Arena tabs)
- [x] Results page (Gallery + Leaderboard tabs)
- [x] Dark theme (paper-ui palette)
- [x] Light theme toggle
- [x] Emoji → SVG icon replacement
- [x] All hardcoded light colors → CSS variables (major pass)
- [x] SkillPageCard, SkillPageDetailModal, SkillPageCreateModal refactored
- [x] ToolPageCard, ToolDetailModal refactored
- [x] Mode-specific bottom drawer tabs
