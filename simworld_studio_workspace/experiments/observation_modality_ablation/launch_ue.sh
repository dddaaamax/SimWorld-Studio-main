#!/bin/bash
# Launch a SimWorld UE instance headless with a specific ablation map
# as the startup level.  This sidesteps the flaky MCP-driven load_map
# path — UE is always born with the target map already loaded.
#
# Usage:
#     ./launch_ue.sh <A|B> <map_index>
#         A → /data/koe/SimWorld-Internal,   GPU 1, ports 55558/9002
#         B → /data/koe/SimWorld-Internal-B, GPU 2, ports 55559/9003
#
# Waits up to 180s for UnrealCV to start listening, then returns 0 on
# success; non-zero + exits UE otherwise.

set -euo pipefail

INSTANCE="${1:-}"
MAP_IDX="${2:-}"
if [[ -z "$INSTANCE" || -z "$MAP_IDX" ]]; then
  echo "Usage: $0 <A|B> <map_index 0..16>" >&2
  exit 2
fi

case "$INSTANCE" in
  A) PROJECT=/data/koe/SimWorld-Internal;    GPU=1; MCP=55558; UCV=9002 ;;
  B) PROJECT=/data/koe/SimWorld-Internal-B;  GPU=2; MCP=55559; UCV=9003 ;;
  *) echo "Unknown instance $INSTANCE" >&2; exit 2 ;;
esac

MAP_ASSET=$(printf "/Game/AblationMaps/ablation_%02d.umap" "$MAP_IDX")
LOG_FILE="/data/koe/ue_logs/ue_${INSTANCE}.log"

# Kill any existing UE on this port — a stale process holds the socket
# so the new one silently crashes with "port in use" at the unrealcv
# layer.  We match on the MCP port which is unique per instance.
pkill -9 -f "UnrealEditor.*MCPPort=${MCP}" 2>/dev/null || true
sleep 3

: > "$LOG_FILE"

echo "[launch_ue] instance=$INSTANCE map_idx=$MAP_IDX asset=$MAP_ASSET gpu=$GPU mcp=$MCP ucv=$UCV"
cd "$PROJECT"
CUDA_VISIBLE_DEVICES=$GPU \
VK_ICD_FILENAMES=/etc/vulkan/icd.d/nvidia_icd.json \
  /data/koe/UE_5.3.2/Engine/Binaries/Linux/UnrealEditor \
  "$PROJECT/SimWorld.uproject" \
  "$MAP_ASSET" \
  -MCPPort=$MCP -cvport=$UCV \
  -NOSPLASH -NOSOUND \
  -ResX=1280 -ResY=720 \
  -RenderOffScreen -FPSMAX=15 \
  -log \
  > "$LOG_FILE" 2>&1 &

UE_PID=$!
echo "[launch_ue] UE PID=$UE_PID"

# Wait up to 300s for UnrealCV listen line — cold asset loading for
# large maps can exceed 3 min on first touch.
#
# IMPORTANT: UE's -log output balloons to many MB during early init
# (thousands of "PNG CRC error" warnings).  A plain `grep -q pattern
# "$LOG_FILE"` scans the whole file on every poll, which pushes each
# iteration into seconds and lets the nominal 180-300s timeout blow
# past 15 min.  We poll by TCP port instead — much cheaper and more
# accurate.
for i in $(seq 1 300); do
  if ss -ltn 2>/dev/null | awk -v p=":${UCV}$" '$4 ~ p {found=1} END {exit !found}'; then
    echo "[launch_ue] UnrealCV listening on :$UCV after ${i}s"
    for j in $(seq 1 60); do
      if ss -ltn 2>/dev/null | awk -v p=":${MCP}$" '$4 ~ p {found=1} END {exit !found}'; then
        echo "[launch_ue] MCP listening on :$MCP after additional ${j}s"
        exit 0
      fi
      sleep 1
    done
    echo "[launch_ue] MCP did not announce listen; proceeding anyway"
    exit 0
  fi
  # Only check for crash markers in the last N kb of the log — cheap.
  if tail -c 65536 "$LOG_FILE" 2>/dev/null | grep -q "Fatal error"; then
    echo "[launch_ue] UE crashed with fatal error — aborting"
    tail -20 "$LOG_FILE" >&2
    exit 1
  fi
  sleep 1
done

echo "[launch_ue] timed out waiting for UnrealCV on :$UCV"
tail -20 "$LOG_FILE" >&2
exit 1
