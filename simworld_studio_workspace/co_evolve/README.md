# Co-Evolution Module

Two-agent adversarial co-evolution: **Coding Agent** designs scenes + tasks,
**Embodied Agent** learns to navigate them. Both have independent hierarchical memory.

## Quick Start

```bash
# 1. Set environment variables
export UE_PROJECT="/path/to/SimWorld.uproject"  # UE project file
export NAV_BASE_URL="http://your-llm-server:8002/v1"
export CODING_BASE_URL="http://your-llm-server:8002/v1"
export NAV_MODEL_ID="Qwen3.5-9B"
export CODING_MODEL_ID="Qwen3.5-9B"

# 2. Start UE Editor manually (load agent_test map)

# 3. Run co-evolution
python -m co_evolve --mode live --generations 30

# With CLI overrides:
python -m co_evolve --mode live --generations 30 \
  --nav-model-id Qwen3.5-27B \
  --nav-base-url http://server:8001/v1

# Resume interrupted experiment:
python -m co_evolve --resume runs/co_evolve/coevolve_XXXXXXXX
```

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `UE_PROJECT` | (none) | Path to SimWorld.uproject |
| `UE_EDITOR` | (none) | Path to UnrealEditor binary |
| `NAV_BASE_URL` | `http://localhost:8002/v1` | Nav agent LLM endpoint |
| `NAV_MODEL_ID` | `Qwen3.5-9B` | Nav agent model |
| `CODING_BASE_URL` | `http://localhost:8002/v1` | Coding agent LLM endpoint |
| `CODING_MODEL_ID` | `Qwen3.5-9B` | Coding agent model |
| `NAV_API_KEY` | `EMPTY` | Nav LLM API key |
| `CODING_API_KEY` | `EMPTY` | Coding LLM API key |
| `UCV_PORT` | `9001` | UnrealCV TCP port |
| `MCP_PORT` | `55557` | MCP TCP port |

## Architecture (v4 — PIE Cycle)

Each epoch follows this cycle:

```
 Editor Mode                          PIE Mode
┌──────────────────────┐    ┌──────────────────────┐
│ 1. exit_pie          │    │ 4. start_pie         │
│ 2. Coding agent      │    │ 5. NavMesh verify    │
│    spawn/destroy     │──> │ 6. Run nav episodes  │
│    objects via UCV   │    │ 7. Collect SR/SPL    │
│ 3. Build NavMesh     │    │                      │
│    via MCP           │ <──│ (loop back)          │
└──────────────────────┘    └──────────────────────┘
```

Key commands (via UnrealCV):
- `vexec /action/start_pie` — Enter play mode
- `vexec /action/exit_pie` — Return to editor
- `vset /objects/spawn_bp_asset ...` — Spawn objects (works in editor AND PIE)
- `vset /object/{name}/destroy` — Destroy objects

### Coding Agent Actions

The coding agent can output three actions:

| Action | Effect |
|--------|--------|
| `keep_scene` | No scene changes, only adjust path/steps |
| `modify_scene` | Incremental: add AND/OR remove specific objects |
| `new_scene` | Clear ALL objects, then add new ones |

## Two-Agent Design

```
Coding Agent (LLM)                    Embodied Agent (LLM)
├── SEES:                             ├── SEES:
│   ├── Nav agent strategies          │   ├── Bearing + distance
│   ├── Performance history           │   └── Own strategy memory
│   └── Own design memory             │
├── CONTROLS:                         ├── CONTROLS:
│   ├── Spawn/destroy objects         │   └── MOVE/TURN/STOP
│   ├── Task params (path, steps)     │
│   └── Task type                     │
├── REWARD: 1 - nav_SR               ├── REWARD: reach goal
│   (0 if SR < 0.1)                   │
└── Adversarial: push difficulty      └── Cooperative: navigate
```

## Task Difficulty Rubric (0-10)

| Dimension | Range | Formula |
|-----------|-------|---------|
| Path length | 0-2.5 | geodesic_cm / 1000 |
| Detour ratio | 0-2.5 | (geo/eucl - 1) x 2.5 |
| Scene blocked | 0-2.5 | blocked_ratio x 5 |
| Heading offset | 0-1.0 | offset_deg / 180 |
| Task type | 0-1.5 | pointnav=0, objectnav=1.5 |

## Memory Systems

### Embodied Agent: StrategyMemory
- `reflect()` after each episode extracts navigation principles
- `query()` injects strategies into system prompt
- Persists across epochs

### Coding Agent: CodingAgentMemory (L1-L3)
- **L1 (Working):** Last 30 design records
- **L2 (Episodic):** Which designs got good coding_reward (0.25-0.75)
- **L3 (Skills):** Distilled design principles (max 8)
- `maybe_reflect()` every 3 epochs

## Asset Catalog

104 assets from CityDatabase:
- **Buildings** (76): `building_01` ... `building_48`, `building_100` ... `building_127`
- **Trees** (6): `tree_1` ... `tree_6`
- **Obstacles** (22): `table`, `hydrant`, `box`, `road_blocker`, `couch`, etc.

Collision mode 2 (XY-only): objects separate from each other but stay at z=0.

## Output Structure

```
runs/co_evolve/coevolve_YYYYMMDD_HHMMSS/
├── config.json               # Full configuration
├── checkpoint.json           # Resume state
├── coding_memory.json        # Coding agent L1-L3
├── strategy_memory.json      # Nav agent strategies
├── all_results.json          # All epoch results
├── coevolution_results.png   # 6-panel plot
├── sr_vs_difficulty.png      # SR vs difficulty plot
├── maps/
│   └── scene_NNN.json        # Object lists per scene
└── epoch_NNN/
    ├── scene_spec.json       # Coding agent decision
    ├── episodes.json         # Generated episodes
    ├── trajectories.json     # Nav agent traces
    └── gen_result.json       # SR, SPL, reward
```

## C++ Plugin Changes (UnrealCV)

### PIE Control
- `vexec /action/start_pie` — Start PIE via LevelEditorSubsystem
- `vexec /action/exit_pie` — Stop PIE, clean WorldController cache
- All UCV commands work in both editor and PIE modes

### Collision Mode
- `spawn_bp_asset ... 0` — No collision repair
- `spawn_bp_asset ... 1` — Full repair (XY + ground trace)
- `spawn_bp_asset ... 2` — XY-only (no ground trace, keeps z position)

### Build Dependencies
- `UnrealCV.Build.cs` requires `LevelEditor` module for PIE control

## Files

| File | Purpose |
|------|---------|
| `loop.py` | Main co-evolution loop (PIE cycle per epoch) |
| `coding_agent.py` | LLM scene designer (keep/modify/new_scene) |
| `coding_memory.py` | Hierarchical memory with adversarial reward |
| `scene_manager.py` | Spawn/destroy/clear objects via UnrealCV |
| `difficulty.py` | Task difficulty rubric (0-10) |
| `context_manager.py` | Aggregates both agents' state |
| `feedback.py` | Format performance data for prompts |
| `checkpoint.py` | Save/restore experiment state |
| `config.py` | Configuration (env vars + CLI) |
| `visualize.py` | Plot generation |
| `ue_launcher.py` | UE startup helpers (legacy, used for one-time setup) |

## Known Limitations

1. **9B nav model ceiling** — SR drops to 0% consistently at diff > 3.5. Use 27B+ for harder tasks.
2. **NavMesh must be built in editor mode** — PIE-mode builds produce empty navmesh.
3. **UE crash on long runs** — UE may crash after 20+ PIE cycles. Use `--resume` to continue.
4. **Qwen `</think>` format** — JSON parser strips thinking blocks before extraction.
