#!/bin/bash
# Retry the maps that got clobbered by the 03:48 vLLM hang.
# Uses instance B (port 9003 / MCP 55559).  Resume logic inside
# run_map_all_configs skips episodes that already have summary.json.

set -u
WORKSPACE="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$WORKSPACE"

LAUNCH="experiments/observation_modality_ablation/launch_ue.sh"
LOG_DIR=/data/koe/ue_logs

for idx in 15 13 14; do
    log="$LOG_DIR/retry_map$(printf '%02d' "$idx")_B.log"
    echo "[retry] B: launching UE for map $idx" | tee -a "$log"
    $LAUNCH B "$idx" >>"$log" 2>&1 || { echo "[retry] B map $idx: launch failed" | tee -a "$log"; continue; }
    sleep 45
    echo "[retry] B: running configs on map $idx" | tee -a "$log"
    PYTHONPATH=. python3 -u -m experiments.observation_modality_ablation.run_map_all_configs \
        --map-index "$idx" --mcp-port 55559 --ucv-port 9003 >>"$log" 2>&1 \
        || { echo "[retry] B map $idx: run failed" | tee -a "$log"; continue; }
    echo "[retry] B: map $idx done" | tee -a "$log"
done

echo "[retry] B: all retries attempted"
