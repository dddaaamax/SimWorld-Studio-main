#!/bin/bash
# Drive the full ablation: for each of 17 maps, launch UE with that
# map pre-loaded, then run all (model × modality) combos on it.  Two
# UE instances (A + B) run in parallel; they split maps by index
# parity so both are busy throughout.
#
# Resume-safe: run_modality skips episodes whose summary.json already
# exists on disk.  This script is also restartable — if an earlier
# run died halfway through map K, rerunning the script will pick up
# where it left off once the UE launch completes.
#
# Usage:  ./orchestrate_runs.sh  (from the workspace root)

set -u

WORKSPACE="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$WORKSPACE"

LAUNCH="experiments/observation_modality_ablation/launch_ue.sh"
LOG_DIR=/data/koe/ue_logs
mkdir -p "$LOG_DIR"

run_one_map() {
    local instance=$1
    local idx=$2
    local mcp ucv
    case "$instance" in
        A) mcp=55558; ucv=9002 ;;
        B) mcp=55559; ucv=9003 ;;
    esac
    local log="$LOG_DIR/run_map$(printf '%02d' "$idx")_$instance.log"

    echo "[orchestrate] $instance: launching UE for map $idx"
    if ! $LAUNCH "$instance" "$idx" >>"$log" 2>&1; then
        echo "[orchestrate] $instance map $idx: launch failed" | tee -a "$log"
        return 1
    fi
    sleep 45  # let asset streaming settle

    echo "[orchestrate] $instance: running all (model,modality) combos on map $idx"
    if ! PYTHONPATH=. python3 -u -m experiments.observation_modality_ablation.run_map_all_configs \
            --map-index "$idx" --mcp-port "$mcp" --ucv-port "$ucv" >>"$log" 2>&1; then
        echo "[orchestrate] $instance map $idx: run failed" | tee -a "$log"
        return 1
    fi
    echo "[orchestrate] $instance map $idx: done"
}

worker() {
    local instance=$1; shift
    local indices=("$@")
    for idx in "${indices[@]}"; do
        run_one_map "$instance" "$idx" || true
    done
    echo "[orchestrate] worker $instance: all assigned maps done"
}

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

wait $PID_A $PID_B
echo "[orchestrate] all workers finished"
