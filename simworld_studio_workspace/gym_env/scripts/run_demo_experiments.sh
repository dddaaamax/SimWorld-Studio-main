#!/usr/bin/env bash
# Demo: run 3 experiment settings one at a time.
#
#   1) PointNav  - Single Agent  (with trajectory recording)
#   2) PointNav  - Batch Agents  (ghost-mode, 3 concurrent)
#   3) ObjectNav - Single Agent  (with trajectory recording)
#
# Usage:
#   bash scripts/run_demo_experiments.sh 1     # run experiment 1 only
#   bash scripts/run_demo_experiments.sh 2     # run experiment 2 only
#   bash scripts/run_demo_experiments.sh 3     # run experiment 3 only
#   bash scripts/run_demo_experiments.sh       # run all 3 sequentially
#
# Env vars:
#   TASK_GEN_DIR   path to task_gen repo  (default: ../../task_gen)
#   UCV_PORT       UnrealCV port          (default: 9000)
#   MCP_PORT       UE MCP port            (default: 55557)
#   MODEL          LLM to use             (default: claude)
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
GYM_DIR="$(dirname "$SCRIPT_DIR")"
WORKSPACE_DIR="$(dirname "$GYM_DIR")"
PROJECT_ROOT="$(dirname "$WORKSPACE_DIR")"

export TASK_GEN_DIR="${TASK_GEN_DIR:-$PROJECT_ROOT/task_gen}"
export PYTHONPATH="${TASK_GEN_DIR}:${WORKSPACE_DIR}:${PYTHONPATH:-}"

if [ -f "$GYM_DIR/.venv/bin/activate" ]; then
    source "$GYM_DIR/.venv/bin/activate"
fi

cd "$WORKSPACE_DIR"

UCV_PORT="${UCV_PORT:-9000}"
MCP_PORT="${MCP_PORT:-55557}"
MODEL="${MODEL:-claude}"
SCENE_GRAPH="${SCENE_GRAPH:-$PROJECT_ROOT/test_map_scene_graph.json}"

WHICH="${1:-all}"

# ─────────────────────────────────────────────────────────────────
# Experiment 1: PointNav - Single Agent
# ─────────────────────────────────────────────────────────────────
run_pointnav_single() {
    echo "=============================================="
    echo " Experiment 1: PointNav - Single Agent"
    echo "=============================================="
    python -m gym_env.runner \
        --model "$MODEL" \
        --task pointnav \
        --scene-graph "$SCENE_GRAPH" \
        --nav-min-cm 1000 \
        --nav-max-cm 3000 \
        --max-steps 40 \
        --seed 42 \
        --record-trajectory \
        --memory none \
        --ucv-port "$UCV_PORT" \
        --mcp-port "$MCP_PORT" \
        --run-name "demo_pointnav_single" \
        --log-level INFO
}

# ─────────────────────────────────────────────────────────────────
# Experiment 2: PointNav - Batch Agents (ghost-mode, 3 concurrent)
# ─────────────────────────────────────────────────────────────────
run_pointnav_batch() {
    echo "=============================================="
    echo " Experiment 2: PointNav - Batch Agents (3x)"
    echo "=============================================="
    python -m gym_env.batch_runner \
        --mode batch \
        --n-tasks 3 \
        --wave-size 3 \
        --model "$MODEL" \
        --task pointnav \
        --scene-graph "$SCENE_GRAPH" \
        --nav-min-cm 1000 \
        --nav-max-cm 3000 \
        --max-steps 40 \
        --seed 42 \
        --ucv-port "$UCV_PORT" \
        --mcp-port "$MCP_PORT" \
        --run-name "demo_pointnav_batch" \
        --log-level INFO
}

# ─────────────────────────────────────────────────────────────────
# Experiment 3: ObjectNav - Single Agent
# ─────────────────────────────────────────────────────────────────
run_objectnav_single() {
    echo "=============================================="
    echo " Experiment 3: ObjectNav - Single Agent"
    echo "=============================================="
    python -m gym_env.runner \
        --model "$MODEL" \
        --task objectnav \
        --target-filter "Building" \
        --object-category "building" \
        --scene-graph "$SCENE_GRAPH" \
        --nav-min-cm 800 \
        --nav-max-cm 4000 \
        --max-steps 40 \
        --seed 42 \
        --record-trajectory \
        --memory none \
        --ucv-port "$UCV_PORT" \
        --mcp-port "$MCP_PORT" \
        --run-name "demo_objectnav_single" \
        --log-level INFO
}

# ─────────────────────────────────────────────────────────────────

case "$WHICH" in
    1) run_pointnav_single ;;
    2) run_pointnav_batch ;;
    3) run_objectnav_single ;;
    all)
        run_pointnav_single
        echo ""
        run_pointnav_batch
        echo ""
        run_objectnav_single
        ;;
    *)
        echo "Usage: $0 {1|2|3|all}"
        echo "  1 = PointNav Single Agent"
        echo "  2 = PointNav Batch Agents"
        echo "  3 = ObjectNav Single Agent"
        exit 1
        ;;
esac
