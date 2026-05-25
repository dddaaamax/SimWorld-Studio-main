#!/bin/bash
# Wait for A's orchestrate_runs to finish (map 16 last), then re-run
# map 14 which was aborted by the 03:48 vLLM hang.  Resume skips any
# episodes already produced by retry_b's parallel attempt.

set -u
WORKSPACE="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$WORKSPACE"

LAUNCH="experiments/observation_modality_ablation/launch_ue.sh"
LOG_DIR=/data/koe/ue_logs
LOG="$LOG_DIR/retry_map14_A.log"

# Wait until A's Python worker (run_map_all_configs on port 55558)
# exits — that's the signal the primary orchestrate_runs is done
# with map 16 and instance A is idle.
echo "[retry_a] waiting for A's run_map_all_configs to exit..." | tee -a "$LOG"
while pgrep -f "run_map_all_configs.*--mcp-port 55558" >/dev/null; do
    sleep 30
done
echo "[retry_a] A worker exited; killing UE A and launching on map 14" | tee -a "$LOG"

pkill -9 -f "UnrealEditor.*MCPPort=55558" 2>/dev/null
sleep 5

if ! $LAUNCH A 14 >>"$LOG" 2>&1; then
    echo "[retry_a] A map 14: launch failed" | tee -a "$LOG"; exit 1
fi
sleep 45
PYTHONPATH=. python3 -u -m experiments.observation_modality_ablation.run_map_all_configs \
    --map-index 14 --mcp-port 55558 --ucv-port 9002 >>"$LOG" 2>&1 \
    && echo "[retry_a] A map 14: done" | tee -a "$LOG" \
    || echo "[retry_a] A map 14: run failed" | tee -a "$LOG"
