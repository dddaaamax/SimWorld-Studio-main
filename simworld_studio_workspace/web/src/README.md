# SimWorld Studio — Frontend

React 18 single-page app. Built with Vite. No external UI library — all components are bespoke inline-styled JSX.

## Architecture

```
App.jsx (root ~9000 lines)
├── PollProvider            SSE connection, React contexts for all real-time data
│   ├── AgentsContext       Agent sessions (location, rotation, status, activity)
│   ├── SceneContext        Scene objects, environment readiness
│   ├── ChatLogContext      Public agent chat messages
│   ├── StatusContext       PIE status, UE health
│   ├── MetricsContext      Time-series data (collision, speed, turns per agent)
│   └── SyncContext         Stale agent detection, SSE ok/error
│
└── App (3-column resizable layout)
    ├── Left column
    │   ├── ChatPanel            Coding agent chat + streaming
    │   └── CodingVerifierPanel  Collision check + VLM scoring
    ├── Center column
    │   ├── ViewportPanel        Pixel Streaming iframe + screenshot fallback
    │   └── Drawer               Assets / Scenes / Context (collapsible, resizable)
    └── Right column
        ├── AgentPanel           Agent cards + ↻ Sync + floating detail window
        └── AgentAggregatePanelTabs  Overview / Testbed / Comm
```

## Key Components

### `PollProvider`
- Opens one `EventSource` to `/api/events`
- Updates 6 React contexts via fine-grained comparison (avoids unnecessary re-renders)
- SSE stale agent detection: requires 3+ consecutive missed pushes (hysteresis)
- ChatLog dedup key: `from|timestamp|text[:20]` (stable across retries)
- MetricsContext: only updates when `sampledAt` changes

### `ChatPanel`
- Streams Claude's response via SSE (`/api/chat`)
- Renders `tool_start` / `tool_result` / `text` / `screenshot` events
- `verify_scene` results show PASS/FAIL badge inline
- Ref-based API (`chatRef`) for programmatic insertion from Asset Drawer

### `ViewportPanel`
- Tries Pixel Streaming first (via `PixelStreamPlayer`)
- Falls back to polling latest screenshot (`/api/screenshot/latest?t=...`)
- `PixelStreamPlayer.jsx`: iframe → Cirrus URL, click-to-activate, status badge

### `AssetBrowser` (Content Drawer)
- Loads full tree once on first visibility via `/api/asset-tree`
- Re-polls every 5s until `source === 'ue-python'` (live UE scan complete)
- **Navigation** (UE Content Browser–style):
  - Normal: shows direct children only (subfolders + assets of current node)
  - Category filter (left sidebar): global cross-tree filter, no path override
  - Search: recursive subtree, optional category narrowing
- Local pagination (40/page, Load more) — no additional API calls

### `AgentPanel`
- Reads from `AgentsContext` (SSE)
- ↻ Sync button → `POST /api/context-snapshot` → force UE snapshot + agent sync
- Agent cards → click → opens `AgentDetailPanel` as `ReactDOM.createPortal` floating window

### `AgentDetailPanel`
- Draggable floating window (fixed position, `onMouseDown` drag handler)
- `liveState` derives from SSE `pollData.sessions` — zero extra API calls
- Shows `"syncing…"` badge until first SSE push
- **Camera tab**: polls `/api/agent-camera/:name` every 4s (3-strategy server fallback)
- **Trajectory tab**: uses `trajectoryPreview` from SSE (last 10 pts), "Load all N" → `/api/agent-trajectory/:name`
- **Activity tab**: past ReAct turns with thought + tool results
- **Chat tab**: send direct command to agent via `/api/agent-broadcast`

### `AgentOverviewPanel` (Statistics bottom-right)
- Stat chips: Agents / Running / Collisions / Total Turns (22–24px bold)
- SVG 2D top-down map: trajectory paths + heading arrows + collision red dots
- `MultiLineChart`: collision over time per agent (from MetricsContext)
- `LineChart`: speed over time per agent

### `CodingVerifierPanel` (Statistics bottom-left)
- **Collision tab**: calls `vget /scene/collisions` → counts + pairs + history chart
  - **Auto-triggers** when `latestScreenshot` prop changes (new scene = coding agent just built)
  - Records to MetricsHub via `POST /api/metrics/scene-collision`
- **VLM tab**: uploads screenshot → `POST /api/vlm-score` → Claude reads image, returns JSON score

### `MultiAgentTestbed`
- Configurable agent count + goal text
- Step 1: check PIE → request start → poll until confirmed active (max 30s, not blind wait)
- Step 2: spawn N agents at radius positions
- Step 3: broadcast goal to each agent

## Reusable SVG Charts

```jsx
<LineChart series={[1,2,3,5,7]} label="Collisions" color="#dc2626" W={440} H={80} />
<MultiLineChart seriesMap={{ Agent1: [...], Agent2: [...] }} label="Speed m/s" W={440} H={90} />
```

Both handle: Y-axis grid + ticks, area fill, last-value dot, label.

## Contexts & Hooks

| Hook | Returns | Source |
|---|---|---|
| `useAgents()` | `{ agents, sessions, activities }` | SSE |
| `useScene()` | `{ objects, environment, round }` | SSE |
| `useChatLog()` | `Message[]` | SSE |
| `useStatus()` | `{ pieActive, health }` | SSE |
| `useMetrics()` | `{ series, sceneCollisions }` | SSE |
| `useSync()` | `{ staleAgents, sseOk }` | SSE |
| `usePoll()` | legacy combined object | SSE (avoid for new code) |

## CSS Design System

Three font sizes (CSS variables):
- `--fs-title`: 20px — panel headers, section titles
- `--fs-panel`: 15px — tab labels, nav items
- `--fs-body`: 13px — all content, labels, data (minimum)

Shadow system:
- `.sw-panel-card`: static `2px 3px 10px rgba(.09)` natural bottom-right drop shadow
- Columns use `overflow: visible` so shadows are never clipped

Layout:
- 3-column resizable (`colLeft` / `flex:1` / `colRight`)
- Each column split into 2 independent panels with 8px gap
- Default ratio ≈ 28% / 44% / 28%
