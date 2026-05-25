# DiverseMaps50 Navigation Experiment

PointNav + ObjectNav evaluation with Qwen3.5-27B on 65 diverse UE maps.

## Quick Start

```bash
cd simworld_studio_workspace

# Step 1: Train (50 maps, agent learns and updates memory)
EVAL_SLOT_OFFSET=0 python3 -u -m experiments.diverse50_eval.run_experiment \
    --setting pointnav --split train --parallel 4

# Step 2: Test (13 held-out maps, memory frozen, evaluation only)
EVAL_SLOT_OFFSET=0 python3 -u -m experiments.diverse50_eval.run_experiment \
    --setting pointnav --split test --parallel 4

# Run ObjectNav in parallel using slots 4-7
EVAL_SLOT_OFFSET=4 python3 -u -m experiments.diverse50_eval.run_experiment \
    --setting objectnav --split train --parallel 4

# Resume after crash
EVAL_SLOT_OFFSET=0 python3 -u -m experiments.diverse50_eval.run_experiment \
    --setting pointnav --split train --parallel 4 --resume
```

## One-time Setup

### 1. Create per-instance UE project directories

Each parallel UE instance needs its own project directory (separate `Saved/` and `Config/`
to avoid config conflicts, while sharing large assets via symlinks):

```bash
ORIG="/data/koe/simworld_studio_projects"
for i in 0 1 2 3 4 5 6 7; do
    INST="/data/koe/simworld_studio_inst_${i}"
    mkdir -p "$INST/Saved"
    # Symlink large shared directories
    for d in Content Binaries DerivedDataCache Plugins Intermediate Source; do
        [ ! -e "$INST/$d" ] && ln -s "$ORIG/$d" "$INST/$d"
    done
    # Config: own copy (not symlink) so per-instance settings don't conflict
    [ ! -d "$INST/Config" ] && cp -r "$ORIG/Config" "$INST/Config"
    # Copy .uproject
    [ ! -f "$INST/SimWorld.uproject" ] && cp "$ORIG/SimWorld.uproject" "$INST/SimWorld.uproject"
done
```

### 2. Fix UnrealCV port assignment (build the plugin)

The `unrealcv` plugin's `ServerConfig.cpp` originally used a relative path for its ini file,
which resolved to the shared engine binary directory — causing all parallel instances to
overwrite each other's port settings.

**The fix** (already applied in this repo): `ServerConfig.cpp` now uses
`FPaths::ProjectDir()` so each instance writes its own `Saved/unrealcv.ini`:

```cpp
// In: Plugins/unrealcv/Source/UnrealCV/Private/Server/ServerConfig.cpp
ConfigFile = FPaths::ConvertRelativePathToFull(
    FPaths::Combine(FPaths::ProjectDir(), TEXT("Saved/unrealcv.ini")));
```

After this change, **recompile the project**:
```bash
# In UE Editor: click the "Compile" button, or run:
/data/koe/UE_5.3.2/Engine/Build/BatchFiles/Linux/Build.sh \
    SimWorldEditor Linux Development \
    /data/koe/simworld_studio_projects/SimWorld.uproject
```

Until the recompile is done, the experiment uses a **cross-process file lock** as a
workaround to serialize UE launches so each instance reads a unique port:
- Port 9010–9013 for slots 0–3 (PointNav)
- Port 9014–9017 for slots 4–7 (ObjectNav)

### 3. Qwen tool-calling mode

Qwen3.5-27B on vLLM does **not** return OpenAI-style tool calls.
The fix: force `text_action_mode = True` in `run_experiment.py`:

```python
llm = make_llm(LLM_MODEL, ...)
llm._text_action_mode = True  # parse action name from text, no tool-call API
```

In text-action mode, the system prompt lists valid action names and the model
replies with one of them as plain text. The `OpenAICompatClient` parses this
and returns a synthetic `ToolCall` object — fully compatible with the rest of
the codebase.

## Scaling: Ghosts × UE Instances

`gym_env.batch_runner` runs **N ghost agents in ONE UE instance per process**.
Two independent knobs:

**1. Ghosts per UE — keep at 5–10**

`--n-tasks` (or `WAVE_SIZE` in this experiment) is the wave size. Above ~10
ghosts UE's actor / sensor registration becomes unreliable: some
`FusionCamSensor` components silently fail to register, positions get
corrupted under collision, and waves see mass `step_error` around step 4.
**5 is the sweet spot** validated here; 10 still works. We saw a colleague's
22-ghost run lose 19/22 episodes at step 4 with this exact pattern.

```python
# In run_experiment.py
WAVE_SIZE = 5   # ghosts per wave
```

If you call `batch_runner` directly:

```bash
python -m gym_env.batch_runner --mode batch --n-tasks 5 ...
```

**2. Multi-UE = multiple processes**

Each UE instance needs its own MCP/UCV ports **and** its own project copy.
The unrealcv plugin reads `<uproject_dir>/Saved/unrealcv.ini`, so two UEs
sharing one project will race on the `Port=` line. The one-time setup above
(`/data/koe/simworld_studio_inst_{0..7}` with symlinked `Content/`,
`Binaries/`, `Plugins/`, etc. and own copies of `Saved/` + `Config/`)
already handles this for 8 instances.

`run_experiment.py` orchestrates this via the `SLOTS` table — each slot maps
to one UE instance with its own ports + GPU + uproject:

```python
SLOTS = [
  {"mcp_port": 55558, "ucv_port": 9010, "gpu": 0, "uproject": ".../inst_0/..."},
  {"mcp_port": 55560, "ucv_port": 9011, "gpu": 1, "uproject": ".../inst_1/..."},
  ...
]
```

`--parallel N` picks `N` slots starting at `EVAL_SLOT_OFFSET`. PointNav
defaults to slots 0–3, ObjectNav to slots 4–7 (set
`EVAL_SLOT_OFFSET=4`). To call `batch_runner` standalone for one UE:

```bash
UNREAL_MCP_PORT=55558 UNREALCV_PORT=9010 \
  python -m gym_env.batch_runner --mode batch --n-tasks 5 ...
```

**3. LLM endpoint distribution**

`EVAL_LLM_URLS` is a comma-separated list. UE slots are pinned to endpoints
in pairs (`slot_idx // 2`) so each endpoint sees one stable prefix-cache
zone:

```bash
EVAL_LLM_URLS="http://host:8000/v1,http://host:8002/v1,http://host:8003/v1,http://other:8007/v1" \
  python -m experiments.diverse50_eval.run_experiment ...
```

**Recommended baseline**: 4 UEs × 5 ghosts × 4 LLM endpoints = 20 concurrent
ghosts, ~5 concurrent requests per endpoint. Both well under the failure
thresholds we've measured.

---

## Architecture

```
run_experiment.py
  ├── Phase 1 (train): 50 maps, writable memory, Qwen learns strategies
  └── Phase 2 (test):  13 held-out maps, memory frozen

Each phase:
  ├── 4 parallel UE slots (inst_0…inst_3 or inst_4…inst_7)
  │   Each slot serialises UE launch under a cross-process flock:
  │     write unrealcv.ini → spawn UE → wait for "Port: N" → unlock
  │   Then runs episodes:
  │     boot UE → start PIE → connect UCVClient → run ghost waves
  │     └── gym_env/batch_runner.run_wave()  [existing code, not modified]
  │           10 ghost agents × 2 waves = 20 tasks / map
  │           Qwen3.5-27B @ http://132.239.95.133:8001/v1 (text-action mode)
  │           hierarchical memory (writable in train, read-only in test)
  │           RGB first-person observations
  └── writes running_summary.json after each map
```

## Dataset

Pre-generated JSONL files (no UE needed at dataset-generation time):

```
datasets/diverse50/
  train_pointnav.jsonl   # 1348 tasks, 50 maps  → train phase
  test_pointnav.jsonl    #  409 tasks, 13 maps  → test phase
  train_objectnav.jsonl  # 1348 tasks, 50 maps
  test_objectnav.jsonl   #  409 tasks, 13 maps
```

- **PointNav**: goal = (x, y) coordinates, GT path pre-computed
- **ObjectNav**: goal = LLM-generated spatial description (e.g. "the red fire
  hydrant beside the stone wall"), same GT path as PointNav counterpart
- 20 tasks per map; train/test split is template hold-out + 80/20 random

## Memory

```
results/diverse50_eval/
  memory_pointnav.json   # updated after each train episode, frozen at test
  memory_objectnav.json
```

Train uses `HierarchicalMemory` (L1 episode buffer → L2 patterns → L3 skills).
Test loads the trained memory as read-only.

## Output

```
results/diverse50_eval/pointnav_train_<timestamp>/
  meta.json                 # experiment config snapshot
  running_summary.json      # live SR/SPL/nDTW (updated after each map)
  <map_name>/
    map_summary.json        # per-map SR, SPL, SoftSPL, nDTW, avg_steps
    w0/                     # wave 0 (ghost agents 0–9)
      ep_<id>_GhostAgent_0/
        summary.json        # per-episode metrics
        log.jsonl           # per-step obs + actions + rewards
    w1/                     # wave 1 (ghost agents 10–19)
```

## Key Configuration (`run_experiment.py`)

| Constant | Default | Notes |
|---|---|---|
| `LLM_MODEL_ID` | `Qwen3.5-27B` | Model served by vLLM |
| `LLM_BASE_URL` | `http://132.239.95.133:8001/v1` | vLLM OpenAI-compat endpoint |
| `MEMORY_BACKEND` | `hierarchical` | `none` / `text` / `hierarchical` |
| `MAX_STEPS` | `60` | Steps per episode |
| `WAVE_SIZE` | `10` | Ghost agents per wave (2 waves = 20 tasks/map) |

## Troubleshooting

**UE fails to start / MCP bind fail**
```bash
# Kill stale UE processes and free ports
pkill -f UnrealEditor; sleep 5
for p in 55558 55560 55574 55564 55576 55568 55570 55572; do
    fuser -k ${p}/tcp 2>/dev/null
done
```

**Port conflict (two UE instances get same UnrealCV port)**  
The cross-process `flock` in `run_experiment.py` serialises launches.
If broken, verify `results/diverse50_eval/` has no stale lock file.

**Agents take 1 step then stop (no_tool_call)**  
Qwen on vLLM doesn't return OpenAI tool calls.
Make sure `llm._text_action_mode = True` is set in `run_experiment.py`.

**Check live progress**
```bash
tail -f /tmp/eval_pn_train.log | grep -E "wave|MAP DONE|SR=|EXCEPTION"
cat results/diverse50_eval/pointnav_train_*/running_summary.json | python3 -m json.tool
```
