# Navigation Experiments

End-to-end guide for running LLM navigation experiments in SimWorld.

**No web server needed** — experiments talk directly to UE via UnrealCV + MCP.

---

## Prerequisites

1. **Unreal Engine** with SimWorld project loaded (editor open, not PIE)
2. **UnrealCV plugin** listening on `127.0.0.1:9001`
3. **UE MCP TCP server** on `127.0.0.1:55557`
4. **task_gen** repo cloned alongside this project (or set `TASK_GEN_DIR`)
5. **Python 3.10+** with dependencies installed

```bash
# Linux — one-time setup
cd simworld_studio_workspace/gym_env
source scripts/setup_env.sh

# Windows
pip install -e ../task_gen
pip install -r simworld_studio_workspace/gym_env/requirements.txt
```

API keys (set whichever model you use):
```bash
export ANTHROPIC_API_KEY=...     # --model claude
export DASHSCOPE_API_KEY=...     # --model qwen
export OPENAI_API_KEY=...        # --model gpt
```

---

## How It Works

```
1. Open UE Editor (map already loaded with buildings/props)
        │
2. Runner connects UnrealCV (:9001) + MCP (:55557)
        │
3. Runner calls MCP → start PIE (Play-In-Editor)
        │
4. Runner spawns humanoid agent via UnrealCV (Base_User_Agent BP)
        │
5. Episode builder samples start/goal positions
        │
6. Agent is teleported to start position
        │
7. LLM loop: observe (RGB + GPS) → LLM decides action → execute → reward
        │
8. Episode ends: STOP action, max steps reached, or LLM gives up
        │
9. Metrics logged: SR, SPL, SoftSPL, cumulative reward
        │
       (repeat for N episodes if --n-episodes > 1)
```

The scene (buildings, roads, props) must already exist in the UE level.
The runner only spawns the **navigation agent** — it does NOT build the map.

---

## Running Experiments

### 1. Smoke test (no LLM, scripted actions)

Verify UE connection works before spending API credits:

```bash
# Linux
bash gym_env/scripts/run_smoke_test.sh

# Windows
cd simworld_studio_workspace
python -m gym_env.smoke_test --steps 8
```

### 2. Single episode

```bash
bash gym_env/scripts/run_experiment.sh \
    --model claude \
    --task pointnav \
    --target-distance 2000 \
    --max-steps 30
```

### 3. Multi-episode with memory (the main experiment)

```bash
# Baseline: no memory
bash gym_env/scripts/run_experiment.sh \
    --model qwen \
    --n-episodes 20 \
    --memory none \
    --max-steps 40 \
    --run-name qwen_no_memory

# Treatment: text memory enabled
bash gym_env/scripts/run_experiment.sh \
    --model qwen \
    --n-episodes 20 \
    --memory text \
    --max-steps 40 \
    --run-name qwen_with_memory
```

### 4. Custom LLM endpoint (e.g. local vLLM on GPU server)

```bash
bash gym_env/scripts/run_experiment.sh \
    --model qwen \
    --model-id "Qwen/Qwen3-VL-30B-A3B-Instruct" \
    --base-url "http://gpu-server:8000/v1" \
    --api-key "token-abc123" \
    --n-episodes 10 \
    --memory text
```

### 5. Batch with ghost agents (single UE instance)

Run multiple tasks concurrently using ghost-mode agents. Ghost agents
are invisible to each other, pass through each other (zero collision),
but still collide with buildings and objects normally.

```bash
# 6 tasks, 3 concurrent ghost agents per wave, local vLLM
python -m gym_env.batch_runner --mode batch \
    --n-tasks 6 --wave-size 3 \
    --model qwen \
    --model-id "Qwen/Qwen3-VL-32B-Thinking" \
    --base-url http://gpu-server:8000/v1 \
    --api-key EMPTY \
    --max-steps 40

# With memory + WandB
python -m gym_env.batch_runner --mode batch \
    --n-tasks 6 --wave-size 3 \
    --model qwen --memory strategy \
    --base-url http://gpu-server:8000/v1

# Single task with trajectory recording (normal agent, not ghost)
python -m gym_env.batch_runner --mode single \
    --n-tasks 1 --model claude --max-steps 40
```

**Batch mode** spawns N ghost agents per wave in one UE instance — no
need for multiple UE instances. Each ghost agent has its own camera,
episode, and reward tracking. Supports `--memory` for experience
accumulation and `--wandb-project` for experiment tracking.

**Single mode** uses a normal (non-ghost) agent with full trajectory +
frame saving under `runs/`.

| | Batch | Single |
|---|---|---|
| Agent type | Ghost (hidden, pass-through) | Normal (visible, full collision) |
| Concurrency | wave_size agents per UE instance | 1 agent |
| Trajectory | Not saved | Saved (frames/ + JSONL) |
| RGB capture | On | On |
| Memory | Supported (--memory) | Supported (--memory) |
| WandB | Supported (--wandb-project) | Not yet |
| Use case | Fast evaluation over many tasks | Debugging / recording |

### 6. Legacy batch (parallel UE instances)

For running different models in parallel, each on its own UE instance:

```bash
bash gym_env/scripts/run_batch.sh \
    --models claude,qwen \
    --ucv-ports 9000,9001 \
    --parallel 2
```

---

## Analyzing Results

```bash
# Print step-by-step trace for all runs
bash gym_env/scripts/analyze.sh

# Filter specific runs
bash gym_env/scripts/analyze.sh runs/20260410_*qwen*

# Plot memory vs no-memory learning curves
bash gym_env/scripts/analyze.sh --plot
```

---

## Experiment Index

| ID | Description | Model | Episodes | Memory | Key Result |
|----|-------------|-------|----------|--------|------------|
| exp01 | Claude SDK baseline | claude-sdk | individual | none | Text-only validation |
| exp02 | Qwen no-memory 5ep | qwen | 5 per batch | none | Baseline performance |
| exp04 | PointNav 20v20 | qwen | 20 vs 20 | none vs text | Memory impact study |
| exp05 | PointNav 30ep RGB | qwen | 30 vs 30 | none vs text | Extended memory study |

---

## Output Files

Each run produces `runs/<timestamp>_<name>/`:

| File | Content |
|------|---------|
| `meta.json` | Config snapshot, model name, git SHA |
| `episode.jsonl` | Per-step: action, reward, distance, position |
| `llm_raw.jsonl` | Full LLM API responses, token usage |
| `summary.json` | Final SR / SPL / SoftSPL / cumulative reward |
| `run.log` | Python logging output |
| `frames/` | PNG screenshots per step (if `--record-trajectory`) |

---

## Key Metrics

- **SR** (Success Rate): 1 if agent reached goal within success distance, else 0
- **SPL** (Success weighted by Path Length): SR × (shortest_path / actual_path)
- **SoftSPL**: Continuous version — rewards partial progress even on failure
- **Cumulative Reward**: Sum of per-step rewards (distance reduction − step cost)

---

## Troubleshooting

| Problem | Fix |
|---------|-----|
| `Connection refused :9000` | UE not running or UnrealCV plugin not loaded |
| `PIE start failed` | MCP port wrong, or UE editor not open |
| `error Invalid sensor id` | Agent not spawned yet; wait or use `--no-start-pie` |
| `UE crashes on first capture` | Known issue — the 5s sleep after first spawn is required |
| `No actors matched filter` | Wrong `--target-filter` for ObjectNav; check actor names with `vget /objects` |
