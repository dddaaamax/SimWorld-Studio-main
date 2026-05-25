# SimWorld Studio — UI Modes Specification

SimWorld Studio has four explicit pipeline stages. All four share one `StudioShell` with mode-specific content.

---

## Shared Shell (all modes)

```
┌─────────────────────────────────────────────────────────────────────────┐
│ Logo | Project | [1 Scene]→[2 Task]→[3 Training]→[4 Co-evolution]        │
│      Studio | Library | Results                     [Primary CTA]        │
├──────────────────────────────────────────────────────────────────────────┤
│ Artifact Chain: [Scene v3] → [TaskSet_500] → [Run_042] → [Curriculum_1]  │
├─────────────────┬────────────────────────────────┬───────────────────────┤
│  LEFT PANEL     │  CENTER (Viewport)             │  RIGHT PANEL          │
│  (mode-specific)│  + Bottom Drawer               │  (mode-specific)      │
└─────────────────┴────────────────────────────────┴───────────────────────┘
```

**Top bar always shows:**
- SimWorld Studio brand
- `PipelineStepper`: 4 numbered tabs with connecting arrows
- Secondary nav: Studio | Library | Results
- Mode-aware primary CTA (changes per mode, color-coded)
- UE Engine + MCP Server + Claude Code status dots
- Running/Standby pill
- Settings gear

**Artifact chain** (below top bar, visible in Studio mode):  
Shows what has been produced: Scene → TaskSet → TrainingRun → CurriculumRun.  
Each chip is clickable to jump to that mode.

---

## Mode 1: Scene Generation

**Purpose:** Create and verify UE5 environments from text/image/edit prompts.

**Input:** Text prompt | Reference image | Existing scene  
**Output:** Verified Scene (versioned)  
**Primary CTA:** `Generate Scene` (blue)

### Layout
```
┌──────────────────┬────────────────────────────────┬─────────────────────┐
│ Intent + SimCoder│ UE5 Scene Viewport             │ Scene Inspector     │
│                  │                                │                     │
│ • User prompt    │  Actor labels overlay          │ Scene Health        │
│ • Reference image│  Collision boxes toggle        │  Collision: PASS    │
│ • Edit instr.    │  Object highlights             │  Gravity:   PASS    │
│ • Templates      │  Scene stats overlay           │  In Bounds: PASS    │
│ • SimCoder plan  │  Version compare               │  Fidelity:  8/10    │
│                  │                                │  Aesthetics:8/10    │
│ [Generate Scene] │  NO agent HUD                  │                     │
│ [Edit Scene]     │  NO reward charts              │ Scene Summary       │
│ [Run Verifiers]  │  NO Gym interface              │  42 actors          │
│ [Save Version]   │                                │  Afternoon lighting │
│                  │                                │  Ground: 200m       │
│                  │                                │  Version: v3        │
│                  │                                │                     │
│                  │                                │ Selected Actor      │
│                  │                                │  Name / Path        │
│                  │                                │  Location / Rot     │
├──────────────────┴────────────────────────────────┴─────────────────────┤
│ Build Timeline  │ Tool Calls │ Assets │ Skills │ Verifiers │ Versions    │
└─────────────────────────────────────────────────────────────────────────┘
```

**Must NOT show:** agent reward, SPL, Gym runtime, NavMesh overlay, episode table, co-evolution charts.

**Next step button:** `Next: Generate Tasks →`

---

## Mode 2: Task Generation

**Purpose:** Convert a verified scene into PointNav / ObjectNav / custom task sets with NavMesh validation.

**Input:** Verified Scene  
**Output:** TaskSet (e.g., PointNav_500)  
**Primary CTA:** `Generate Tasks` (green)

### Layout
```
┌──────────────────┬────────────────────────────────┬─────────────────────┐
│ Task Builder     │ Scene + NavMesh + Paths         │ Task Inspector      │
│                  │                                │                     │
│ Task type:       │  NavMesh overlay (blue)        │ Task Set            │
│  PointNav ▾     │  Reachable regions (green)     │  Type: PointNav     │
│ Episodes: 500    │  Blocked regions (red)         │  Total:   500       │
│ Min path: 3m     │  Start markers (green ●)       │  Valid:   486       │
│ Max path: 20m    │  Goal markers  (red ●)         │  Rejected: 14       │
│ Success r: 0.5m  │  Shortest paths (white lines)  │                     │
│ Max steps: 500   │  Object target highlights      │ Validation          │
│                  │                                │  NavMesh:  PASS     │
│ PointNav options:│  NO agent execution HUD        │  Solvable: PASS     │
│  ✓ NavMesh conn. │  NO reward charts              │  Reachable:PASS     │
│  ✓ Filter length │                                │  Col-free: PASS     │
│  ✓ Reach. pairs  │                                │                     │
│                  │                                │ Selected Episode    │
│ [Generate Tasks] │                                │  Start / Goal       │
│ [Validate Tasks] │                                │  Path: 18.4 m       │
│ [Preview Gym API]│                                │  Max steps: 500     │
├──────────────────┴────────────────────────────────┴─────────────────────┤
│ Task Sets │ Episodes │ Validation Reports │ Path Statistics │ Gym Preview │
└─────────────────────────────────────────────────────────────────────────┘
```

**Gym Preview tab shows:**
```python
env = simworld.make("Urban_Avenue_Morning/PointNav-v1")
obs, info = env.reset()
obs, reward, done, trunc, info = env.step(action)
```

**Must NOT show:** SimCoder chat as the primary focus, training reward curves.

**Next step button:** `Next: Train Agent →`

---

## Mode 3: Agent Training

**Purpose:** Run embodied agents on task sets, collect trajectories, evaluate behavior.

**Input:** Scene + TaskSet + Agent config  
**Output:** TrainingRun + rollouts + trajectories + metrics + memory/rules  
**Primary CTA:** `Start Training` (violet)

### Layout
```
┌──────────────────┬────────────────────────────────┬─────────────────────┐
│ Training Config  │ Agent Execution View            │ Agent Monitor       │
│                  │                                │                     │
│ Scene:  v3       │  ┌──────────┬──────────┐      │ Live Observation     │
│ TaskSet: PN_500  │  │First-    │Depth     │      │  RGB thumbnail      │
│ Agent:  Qwen-7B  │  │person RGB│view      │      │  Depth thumbnail    │
│ Obs: RGB-D       │  └──────────┴──────────┘      │                     │
│ Method: PPO      │                                │ Current Episode     │
│ Budget: 10k eps  │  Top-down map + path trace     │  Step: 142          │
│ Memory: Enabled  │  Current pose indicator        │  Action: move_fwd   │
│                  │  Goal bearing                  │  Reward: +0.12      │
│ Metrics:         │  Distance to goal              │  Dist to goal: 3.2m │
│  SR:  64%        │  Collision indicator           │  Success: False     │
│  SPL: 0.42       │  Trajectory replay             │                     │
│  nDTW:0.71       │  Failure replay                │ Training Metrics    │
│  Avg R:0.37      │                                │  SR:    64%         │
│                  │  NO SimCoder chat as primary   │  SPL:   0.42        │
│ [Start Training] │  NO scene authoring tools      │  SoftSPL:0.58       │
│ [Run Evaluation] │                                │  nDTW:  0.71        │
│ [Export Results] │                                │  Avg R: 0.37        │
├──────────────────┴────────────────────────────────┴─────────────────────┤
│ Episodes │ Trajectories │ Reward Curve │ Success Metrics │ Failures │ Logs│
└─────────────────────────────────────────────────────────────────────────┘
```

**Must NOT show:** SimCoder prompt as the primary interaction, scene authoring tools as the primary focus.

**Next step button:** `Next: Co-evolve →`

---

## Mode 4: Co-evolution

**Purpose:** Adaptive curriculum where SimCoder adjusts scene/task difficulty based on agent failure feedback.

**Input:** Task generator template + Agent + Curriculum settings  
**Output:** CurriculumRun + adaptation decisions + learned rules/skills  
**Primary CTA:** `Run Co-evolution` (orange)

### Layout
```
┌──────────────────┬────────────────────────────────┬─────────────────────┐
│ Curriculum       │ Co-evolution Loop Canvas        │ Round Inspector     │
│ Builder          │                                │                     │
│                  │ ┌─────────┐  generates         │ Current Round       │
│ Difficulty axes: │ │SimCoder │──────────────→     │  Round: 12 / 25     │
│  Path: 12–22m    │ └─────────┘            ↓       │  Difficulty: L4     │
│  Heading: 0–90°  │      ↑              ┌──────┐   │  Path: 12–22 m      │
│  Obstacles: 0.20 │      │ feedback     │Env + │   │  SR:  64%           │
│  Clutter: medium │      │              │Tasks │   │  Next: Advance L5   │
│  Distract.: 3    │ ┌─────────┐  ↓     └──────┘   │                     │
│                  │ │Difficulty│← ← ←    rollout   │ Round History       │
│ Config:          │ │Adapter  │         ┌──────┐   │  R1  L0  82% Adv.  │
│  Mastery: 70%    │ └─────────┘         │Agent │   │  R2  L1  76% Adv.  │
│  Eps/round: 500  │      ↑              │Outc. │   │  R3  L2  58% Hold  │
│  Max rounds: 25  │      └──────────────└──────┘   │  R4  L2  71% Adv.  │
│  Advance: consec.│                                │  R12 L4  64% Act.  │
│                  │ Below: scene previews          │                     │
│ [Run Co-evolve]  │        failure snapshots       │ SimCoder Adapt.     │
│ [Pause]          │        batch overview          │  ↑ obstacle density │
│ [Evaluate]       │                                │  ↑ route length     │
│ [Export]         │                                │  ↓ sharp turns      │
├──────────────────┴────────────────────────────────┴─────────────────────┤
│ Rounds │ Difficulty Schedule │ Success Rate │ Failure Modes │ Rules      │
└─────────────────────────────────────────────────────────────────────────┘
```

**The loop canvas must clearly show the feedback cycle:**
SimCoder → Environment & Tasks → Agent → Outcomes → Difficulty Adapter → SimCoder

**Must NOT look like ordinary training only.** The adaptive curriculum and the SimCoder adaptation decisions must be visually prominent.

---

## Empty States

### Task Generation — no scene selected
```
No verified scene selected.

To generate tasks, first create a scene that passes:
  ✓ Collision check
  ✓ NavMesh check
  ✓ Scene verification

[← Go to Scene Generation]
```

### Agent Training — no task set
```
No task set selected.

Create PointNav or ObjectNav tasks before training an embodied agent.

[← Go to Task Generation]
```

### Co-evolution — prerequisites missing
```
Co-evolution requires:
  1. A scene/task generator
  2. A task set template
  3. An embodied agent
  4. A difficulty schedule

[Configure Co-evolution]
```

---

## Bottom Drawer Tabs by Mode

| Mode | Tab 1 | Tab 2 | Tab 3 | Tab 4 | Tab 5 |
|------|-------|-------|-------|-------|-------|
| Scene | Assets | Scene Versions | Tool Calls | Skills | Verifiers |
| Task | Task Sets | Episodes | Validation | Path Stats | Gym Preview |
| Training | Episodes | Trajectories | Reward Curve | Failures | Logs |
| Co-evolution | Rounds | Difficulty | Success Rate | Rules | Skills |
