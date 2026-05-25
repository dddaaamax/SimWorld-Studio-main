# gym_env — SimWorld Embodied Agent Experiment Harness

Gym-style Python environment for running reproducible LLM navigation
experiments in Unreal Engine.  Talks UE directly via UnrealCV + MCP —
no JS server needed.

## Quick Start (Windows)

```bash
# 1. Install dependencies (one-time)
pip install -e C:\path\to\task_gen
pip install -r simworld_studio_workspace\gym_env\requirements.txt

# 2. Launch UE (set UnrealCV port in unrealcv.ini next to UnrealEditor.exe)
#    Port 9001 avoids VS Code conflicts on 9000
"C:\Program Files\Epic Games\UE_5.3\Engine\Binaries\Win64\UnrealEditor.exe" ^
    "E:\UE\SimWorld\SimWorld.uproject" /Game/Maps/Empty ^
    -MCPPort=55557 -NOSPLASH -NOSOUND -log

# 3. Run a single PointNav episode with Qwen-VL
cd simworld_studio_workspace
set PYTHONPATH=C:\path\to\task_gen
python -m gym_env.runner ^
    --model qwen ^
    --model-id "Qwen/Qwen3-VL-30B-A3B-Instruct" ^
    --base-url "http://your-gpu-server:8000/v1" ^
    --api-key EMPTY ^
    --ucv-port 9001 ^
    --max-steps 20 ^
    --record-trajectory
```

---

## Supported Tasks

| Task | Agent Sees | Goal | Difficulty |
|------|-----------|------|------------|
| **PointNav** | GPS + compass + (distance, bearing) to goal + optional RGB | Navigate to (x,y) coordinate | Low — pure numeric navigation |
| **ObjectNav** | GPS + compass + category ID + **RGB required** | Find and reach a named object | High — must visually explore |

---

## Supported Models

| `--model` flag | Backend | Vision | Tool Calls | Notes |
|---|---|---|---|---|
| `claude` | Anthropic SDK | Yes | Yes | Best quality; needs `ANTHROPIC_API_KEY` |
| `claude-sdk` | Claude Code CLI | No | Yes | Free via local Claude Code; slow (~14s/step) |
| `gpt` | OpenAI SDK | Yes | Yes | Needs `OPENAI_API_KEY` |
| `gemini` | OpenAI-compat | Yes | Yes | Needs `GEMINI_API_KEY` |
| `qwen` | OpenAI-compat | Yes | Fallback* | Any vLLM/OpenAI-compat endpoint |

*Qwen text-action fallback: if the server doesn't support `--enable-auto-tool-choice`,
the client auto-switches to a text-based action mode with strict if-else prompting.
Thinking models (e.g. `Qwen3-VL-32B-Thinking`) automatically get a larger token budget
(512 vs 32) to accommodate `<think>...</think>` reasoning blocks.

---

## UE Setup

**Do NOT run** `SimWorld-Studio.bat` or the JS web server — those add
unnecessary health-check polling.

### UnrealCV Port Configuration

Edit `unrealcv.ini` next to `UnrealEditor.exe`:

```ini
[UnrealCV.Core]
Port=9001
Width=640
Height=480
FOV=90
EnableInput=True
EnableRightEye=False
```

### Launch UE

```bash
# Windows
"C:\Program Files\Epic Games\UE_5.3\Engine\Binaries\Win64\UnrealEditor.exe" ^
    "E:\UE\SimWorld\SimWorld.uproject" ^
    /Game/Maps/Empty ^
    -MCPPort=55557 -NOSPLASH -NOSOUND -log

# The runner auto-starts PIE mode via MCP on first env.reset().
# Pass --no-start-pie if PIE is already running.
```

### Requirements

- UnrealCV plugin listening on configured port (default 9001)
- UE MCP TCP server on port 55557
- `Base_User_Agent` Blueprint with working FusionCamSensor
  (verified: `EnableController True` must be called after spawn)

---

## Running Experiments

### Single episode (smoke test)

```bash
python -m gym_env.runner \
    --model qwen \
    --model-id "Qwen/Qwen3-VL-30B-A3B-Instruct" \
    --base-url "http://132.239.95.133:8000/v1" \
    --api-key EMPTY \
    --ucv-port 9001 \
    --task pointnav \
    --max-steps 20 \
    --seed 42
```

### Multi-episode comparison (no-memory vs with-memory)

```bash
# Baseline: no memory, 30 episodes
python -m gym_env.runner \
    --model qwen \
    --model-id "Qwen/Qwen3-VL-30B-A3B-Instruct" \
    --base-url "http://132.239.95.133:8000/v1" \
    --api-key EMPTY \
    --ucv-port 9001 \
    --n-episodes 30 \
    --memory none \
    --max-steps 20 \
    --record-trajectory \
    --seed 300 \
    --run-name qwen_no_memory

# With memory: text-based lesson accumulation
python -m gym_env.runner \
    --model qwen \
    --model-id "Qwen/Qwen3-VL-30B-A3B-Instruct" \
    --base-url "http://132.239.95.133:8000/v1" \
    --api-key EMPTY \
    --ucv-port 9001 \
    --n-episodes 30 \
    --memory text \
    --max-steps 20 \
    --record-trajectory \
    --seed 300 \
    --run-name qwen_with_memory
```

### Claude comparison

```bash
python -m gym_env.runner \
    --model claude-sdk \
    --ucv-port 9001 \
    --n-episodes 5 \
    --max-steps 12 \
    --record-trajectory \
    --seed 42
```

---

## Key CLI Flags

| Flag | Default | Description |
|------|---------|-------------|
| `--model` | `claude` | Model backend: claude / claude-sdk / gpt / gemini / qwen |
| `--model-id` | _(auto)_ | Override model ID sent to the endpoint |
| `--base-url` | _(auto)_ | Override API base URL (for vLLM etc.) |
| `--api-key` | _(env var)_ | API key (or `EMPTY` for no-auth endpoints) |
| `--ucv-port` | `9001` | UnrealCV TCP port |
| `--n-episodes` | `1` | Episodes to run sequentially |
| `--memory` | `none` | Memory: none / text / mem0 / strategy / hierarchical |
| `--eval-mode` | `train` | `train` (read-write memory) or `test` (read-only) |
| `--episodes-file` | _(none)_ | Load pre-generated episodes from JSON (skips navmesh) |
| `--task` | `pointnav` | Task: pointnav / objectnav |
| `--target-distance` | `2000` | PointNav goal distance (cm) |
| `--max-steps` | `40` | Steps before episode is truncated |
| `--record-trajectory` | off | Save annotated PNG frame per step |
| `--no-rgb` | off | Skip camera capture (text-only obs) |
| `--seed` | `42` | RNG seed (increments per episode) |
| `--run-name` | _(auto)_ | Custom name for the run directory |

---

## Output

Each episode produces a directory under `runs/`:

```
runs/<timestamp>_<run_name>/
    meta.json           # config snapshot, model, episode ID, git SHA
    episode.jsonl       # per-step: action, reward, obs (bearing, distance), info
    llm_raw.jsonl       # per-step: full LLM response, reasoning text, token usage
    summary.json        # final: SR, SPL, SoftSPL, path_length, cumulative_reward
    run.log             # Python logging
    frames/             # (--record-trajectory) annotated PNG per step
        step_0000.png   #   240x320 RGB + black bar with action/reward/distance
        step_0001.png
        ...
```

---

## Analysis

```bash
# Per-step trace of specific runs
python -m gym_env.analyze_runs runs/*qwen_no_memory*

# Plot learning curves for an experiment
python -m gym_env.plot_learning_curve \
    --no-memory experiments/exp06/.../no_memory \
    --with-memory experiments/exp06/.../with_memory \
    --output experiments/exp06/comparison.png
```

---

## Observation Space

| Key | Type | Shape | Source |
|-----|------|-------|--------|
| `rgb` | uint8 | (240, 320, 3) | `vget /camera/0/lit png` (agent first-person) |
| `gps` | float32 | (2,) | displacement from spawn in cm |
| `compass` | float32 | (1,) | yaw in radians |
| `pointgoal_with_gps_compass` | float32 | (2,) | (distance_cm, bearing_rad) to goal |
| `objectgoal` | int32 | (1,) | category ID (ObjectNav only) |

## Action Space

4 discrete actions (Habitat-aligned):

| Action | UE Command | Effect |
|--------|-----------|--------|
| `MOVE_FORWARD` | `vbp {agent} StepForward 2 0` | Walk forward ~350cm |
| `TURN_LEFT` | `vbp {agent} TurnAround 1 -30 -1` | Rotate left 30 deg |
| `TURN_RIGHT` | `vbp {agent} TurnAround 1 30 1` | Rotate right 30 deg |
| `STOP` | `vbp {agent} StopAgent` | Declare goal reached |

## Reward

```
r_t = (d_{t-1} - d_t)     # Euclidean distance shaping
    - 0.01                  # step cost
    + 2.5 (once)            # success bonus when d_goal < 200cm
```

## Metrics (Anderson et al. 2018)

| Metric | Formula | Meaning |
|--------|---------|---------|
| **SR** | 1 if final d_goal < 200cm | Success Rate |
| **SPL** | SR * l_i / max(p_i, l_i) | Success weighted by Path Length |
| **SoftSPL** | progress * l_i / max(p_i, l_i) | Partial credit for getting closer |

---

## Task Generation (NavMesh-validated)

Two pipelines for generating navigation episodes:

### Offline (Scene Graph A*)

Uses a 2D occupancy grid built from `scene_graph.json`. No UE required.

```python
from nav_task.scene_graph_interface import SceneGraphNavigationInterface

sg = SceneGraphNavigationInterface(
    "scene_graph.json",
    resolution_cm=500,
    agent_radius_cm=80,
    background_classes=frozenset({
        "StaticMeshActor", "Floor_C",
        "NavMeshBoundsVolume", "RecastNavMesh",
    }),
)
positions = sg.get_navigable_positions()  # grid cells outside obstacles
path = sg.get_reference_path(start, goal) # A* waypoints
geo = sg.get_geodesic_distance(start, goal)
```

### Online (UE NavMesh)

Uses UE's Recast/Detour navmesh for true polygon-mesh shortest paths.
Requires PIE running + NavigationHandler in UnrealCV plugin.

```python
from nav_task.navmesh_interface import NavmeshNavigationInterface

nav = NavmeshNavigationInterface("scene_graph.json", ucv_client)
nav.build_navmesh(padding_cm=500)         # vset /nav/build
path = nav.get_reference_path(start, goal) # vget /nav/path
reachable = nav.is_reachable(start, goal)  # vget /nav/reachable
```

### Episode Generation

```python
from gym_env.episode_builder import (
    sample_pointnav_episode_navmesh,
    sample_objectnav_episode_navmesh,
)

# PointNav: random start/goal with navmesh-validated reachability
result = sample_pointnav_episode_navmesh(
    ucv, "scene_graph.json",
    min_geodesic_cm=2000,  # minimum path distance
    max_geodesic_cm=8000,  # maximum path distance
)

# ObjectNav: navigate to a specific object
result = sample_objectnav_episode_navmesh(
    ucv, "scene_graph.json",
    target_filter=lambda n: "Tree" in n,
    object_category="tree",
    object_description="a large green tree with spreading branches",
)
```

Both return a dict with:

| Field | Description |
|-------|-------------|
| `episode` | `NavigationEpisode` with start, goal, GT reference path |
| `start_heading_deg` | Random initial heading (0-360) |
| `difficulty` | `{distance_m, detour_ratio, heading_offset_deg, difficulty_score}` |
| `gt_path_waypoints` | List of (x, y) from navmesh path |
| `prompt` | (ObjectNav only) Agent prompt with direction + object description |

### Difficulty Score

Composite 0-1 score based on:
- **Distance** (40%): geodesic path length, normalised to 0-100m
- **Detour ratio** (35%): geodesic / euclidean — higher means more obstacle avoidance
- **Heading offset** (25%): angle between start heading and target direction

### Scene Graph Generation

Generate `scene_graph.json` from the current UE scene via MCP:

```python
from gym_env.mcp_client import MCPClient
mcp = MCPClient(port=55557)
with open("scripts/query_actors_2d.py") as f:
    script = f"SAVE_PATH = 'scene_graph.json'\n" + f.read()
mcp.execute_python(script)
```

### UnrealCV Navigation Commands

| Command | Description |
|---------|-------------|
| `vset /nav/build minX minY minZ maxX maxY maxZ` | Build navmesh in bounding box |
| `vset /nav/build_from_actor name [padding]` | Build from actor bounds |
| `vget /nav/path x1 y1 z1 x2 y2 z2` | Query path (returns `length\|x,y,z\|...` or `-1`) |
| `vget /nav/reachable x1 y1 z1 x2 y2 z2` | Lightweight reachability test |
| `vget /nav/status` | NavMesh readiness JSON |
| `vget /nav/project x y z` | Project point onto navmesh |
| `vget /nav/random_points N` | Sample N random navigable points |
| `vget /nav/random_reachable x y z radius N` | Sample N points reachable from origin |
| `vget /nav/poly_centers minX minY minZ maxX maxY maxZ` | NavMesh polygon centers in box |
| `vset /nav/fix_blueprints /Game/CityDatabase` | Fix Building BP NavModifier extents |

---

## Architecture

```
Python Experiment Runner
    |
    +-- episode_builder -----> NavigationEpisode (from task_gen)
    |
    +-- SimWorldNavEnv
    |       |-- UCVClient --------TCP:9001-------> UnrealCV (PIE)
    |       |-- MCPClient --------TCP:55557------> UE Python (edit mode)
    |       |-- EuclideanNavigationInterface       (reward + measures)
    |       +-- ObservationBuilder                 (RGB + GPS + compass)
    |
    +-- LLMClient (claude / gpt / gemini / qwen)
    |
    +-- EpisodeLogger (JSONL + PNG + summary)
```

## Ghost Mode (Batch Runner)

The `batch_runner` can run multiple episodes concurrently using ghost agents
in a single UE instance.

Ghost agents:
- Are **hidden** from all cameras (`SetActorHiddenInGame`)
- Use collision channel 8 (`GhostAgent` / `ECC_GameTraceChannel1`)
- **Ignore** each other (channel 8) and normal Pawns (channel 2)
- **Still collide** with buildings, terrain, vehicles, and objects

```bash
# 6 tasks, 3 concurrent ghost agents per wave
python -m gym_env.batch_runner --mode batch \
    --n-tasks 6 --wave-size 3 \
    --model qwen \
    --model-id "Qwen/Qwen3-VL-32B-Thinking" \
    --base-url http://gpu-server:8000/v1 \
    --api-key EMPTY \
    --memory strategy \
    --max-steps 40
```

Each wave spawns N agents, runs them in parallel (sequential LLM calls),
then destroys them before the next wave. Requires the `collision_channel`,
`collision_response`, and `hide`/`show` commands in the UnrealCV plugin.

---

## Pre-generated Episode Sets

Generate a fixed, deterministic set of task episodes offline (no UE
required), then load them at batch-run time to skip runtime navmesh
building and episode sampling entirely.

### Generate + split

```bash
# Generate 30 PointNav episodes (seed=42), split into 22 train + 8 test
cd simworld_studio_workspace
python -m nav_task \
    --map ../SimWorld/simworld/data/roads.json \
    --seed 42 --n-episodes 30 \
    --min-path-length 1000 --max-path-length 4000 \
    --split 22,8 \
    --train-out tasks/pointnav_train.json \
    --test-out tasks/pointnav_test.json
```

Output files contain a JSON envelope with metadata (`split`, `seed`,
`map_file`, `index_range`) and an `episodes` array. Each episode
includes baked-in `reference_path` and `shortest_path_length_cm`, so
SPL/SoftSPL metrics do not require live navmesh queries.

### Load at batch-run time

```bash
# Training: episodes from file, memory accumulates
python -m gym_env.batch_runner --mode batch \
    --episodes-file tasks/pointnav_train.json \
    --n-tasks 22 \
    --eval-mode train \
    --memory strategy \
    --model qwen ...

# Evaluation: frozen memory, no new inserts
python -m gym_env.batch_runner --mode batch \
    --episodes-file tasks/pointnav_test.json \
    --n-tasks 8 \
    --eval-mode test \
    --memory strategy \
    --model qwen ...
```

When `--episodes-file` is set, the runner skips `vset /nav/build` and
all `sample_pointnav_episode*` calls — episodes are deserialized
directly from the file.

### nav_task CLI flags

| Flag | Default | Description |
|------|---------|-------------|
| `--map` | _(required)_ | Path to `roads.json` |
| `--seed` | `42` | Master RNG seed |
| `--n-episodes` | `1` | Total episodes to generate |
| `--output` | `-` (stdout) | Output path (ignored when `--split` is set) |
| `--split` | _(none)_ | `N_TRAIN,N_TEST` — deterministic slice split |
| `--train-out` | _(none)_ | Output path for training split |
| `--test-out` | _(none)_ | Output path for test split |
| `--min-path-length` | `1000` | Minimum geodesic path length (cm) |
| `--max-path-length` | _(none)_ | Maximum geodesic path length (cm) |
| `--max-retries` | `50` | Resampling attempts per episode |
| `--task` | `pointnav` | `pointnav` or `objectnav` |

---

## Train / Test Memory Mode

The `--eval-mode` flag controls whether the memory backend is writable:

| Mode | `insert()` | `query()` | Use case |
|------|-----------|-----------|----------|
| `train` (default) | yes | yes | Accumulate experience across episodes |
| `test` | **no-op** | yes | Frozen evaluation — read training memories, write nothing |

In `test` mode the memory is wrapped in `ReadOnlyMemory`, which
silences `insert()` while forwarding `query()` and `reset()`. This
ensures evaluation is deterministic and does not contaminate the
training memory store.

```bash
# Typical workflow:
# 1. Train with memory on the training split
python -m gym_env.batch_runner \
    --episodes-file tasks/train.json --n-tasks 22 \
    --eval-mode train --memory strategy ...

# 2. Evaluate with frozen memory on the test split
python -m gym_env.batch_runner \
    --episodes-file tasks/test.json --n-tasks 8 \
    --eval-mode test --memory strategy ...
```

---

## Known Issues

- **unrealcv cp1252 crash**: Windows Chinese locale causes the unrealcv
  receive thread to crash on socket errors. Fixed by `gym_env/__init__.py`
  which forces stdout to UTF-8 and monkey-patches `SocketMessage.ReceivePayload`.

- **UE idle socket reset**: UE drops UnrealCV connections after ~10s idle
  (during LLM inference). Fixed by `hard_reconnect` on any send failure.

- **VS Code port conflict**: VS Code may listen on port 9000. Configure
  UnrealCV to use 9001 via `unrealcv.ini`. All runners now default to 9001.

- **Spawn socket timeout**: UnrealCV's `spawn_bp_asset` command triggers a
  TCP socket reset (UE drops the connection while loading the BP). The client
  sleeps 2s then reconnects. First spawn takes ~90s (BP loading); subsequent
  spawns ~90s each due to the socket timeout cycle. Subsequent `env.reset()`
  calls reuse the existing actor (instant).

- **NavMesh rebuild on spawn**: Each spawned actor triggers an automatic
  navmesh rebuild. The `batch_runner` mitigates this by spawning all agents
  with collision disabled, then enabling collision in one batch (single rebuild).

- **Thinking model token budget**: Models like `Qwen3-VL-*-Thinking` emit
  `<think>...</think>` reasoning tokens before the action name. The text-action
  fallback mode automatically allocates 512 tokens (vs 32 for non-thinking models)
  to ensure the action name fits in the output.
