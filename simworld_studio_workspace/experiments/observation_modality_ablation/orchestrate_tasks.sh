#!/bin/bash
# Orchestrate ObjectNav task generation for all 17 maps across the 2
# parallel UE instances (A + B).  For each map we restart UE with that
# map as the startup level (the MCP-driven load_map timeout-hangs on
# large content), then call generate_tasks with --skip-load.
#
# Maps are partitioned by index parity:
#   A handles even indices (0, 2, 4, ...)
#   B handles odd indices  (1, 3, 5, ...)
#
# Each worker writes its own log under /data/koe/ue_logs/gen_mapNN.log
# and the task JSON to experiments/observation_modality_ablation/tasks/.
# Existing task JSONs are skipped.
#
# Usage:  ./orchestrate_tasks.sh  (from the workspace root)

set -u

WORKSPACE="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$WORKSPACE"

LAUNCH="experiments/observation_modality_ablation/launch_ue.sh"
TASKS_DIR="experiments/observation_modality_ablation/tasks"
LOG_DIR=/data/koe/ue_logs
mkdir -p "$TASKS_DIR" "$LOG_DIR"

run_one_map() {
    local instance=$1   # A or B
    local idx=$2
    local mcp ucv
    case "$instance" in
        A) mcp=55558; ucv=9002 ;;
        B) mcp=55559; ucv=9003 ;;
    esac

    local task_file="$TASKS_DIR/ablation_$(printf '%02d' "$idx").json"
    if [[ -f "$task_file" ]]; then
        echo "[orchestrate] SKIP map $idx ($task_file exists)"
        return 0
    fi

    local log="$LOG_DIR/gen_map$(printf '%02d' "$idx").log"
    echo "[orchestrate] $instance: launching UE for map $idx"
    if ! $LAUNCH "$instance" "$idx" >>"$log" 2>&1; then
        echo "[orchestrate] $instance map $idx: UE launch FAILED" | tee -a "$log"
        return 1
    fi
    # give editor a little more time to finish asset loading post-listen
    sleep 45

    echo "[orchestrate] $instance: generating tasks for map $idx"
    if ! PYTHONPATH=. python3 -u -m experiments.observation_modality_ablation.generate_tasks \
            --mcp-port "$mcp" --ucv-port "$ucv" \
            --map-index "$idx" --skip-load >>"$log" 2>&1; then
        echo "[orchestrate] $instance map $idx: generation FAILED" | tee -a "$log"
        return 1
    fi
    echo "[orchestrate] $instance: map $idx done"
    return 0
}

worker() {
    local instance=$1
    shift
    local indices=("$@")
    for idx in "${indices[@]}"; do
        run_one_map "$instance" "$idx" || {
            echo "[orchestrate] worker $instance: map $idx failed — continuing"
        }
    done
    echo "[orchestrate] worker $instance: all assigned maps done"
}

# Split 0..16 across A (even) and B (odd)
A_MAPS=(); B_MAPS=()
for i in $(seq 0 16); do
  if (( i % 2 == 0 )); then A_MAPS+=($i); else B_MAPS+=($i); fi
done

echo "[orchestrate] A handles: ${A_MAPS[*]}"
echo "[orchestrate] B handles: ${B_MAPS[*]}"

worker A "${A_MAPS[@]}" &
PID_A=$!
worker B "${B_MAPS[@]}" &
PID_B=$!

wait $PID_A
wait $PID_B

echo "[orchestrate] all workers finished"
ls -1 "$TASKS_DIR"
