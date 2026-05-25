#!/usr/bin/env bash
# SimWorld Studio — Linux/macOS startup script
# Usage: ./start.sh [--dev] [--no-build]
set -euo pipefail

DEV=false
NO_BUILD=false
for arg in "$@"; do
  case $arg in
    --dev)      DEV=true ;;
    --no-build) NO_BUILD=true ;;
  esac
done

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo ""
echo "╔══════════════════════════════════════════╗"
echo "║     SimWorld Studio — Starting Up        ║"
echo "╚══════════════════════════════════════════╝"
echo ""

# ── Config (env vars with defaults) ───────────────────────────────────────────
export PORT="${PORT:-3002}"
export UCV_PORT="${UCV_PORT:-9001}"
export UNREAL_PORT="${UNREAL_PORT:-55557}"
export CIRRUS_HTTP_PORT="${CIRRUS_HTTP_PORT:-8685}"
export CIRRUS_WS_PORT="${CIRRUS_WS_PORT:-8686}"
export LOG_LEVEL="${LOG_LEVEL:-info}"
export NODE_ENV="${NODE_ENV:-production}"
$DEV && export NODE_ENV="development"

echo "Config:"
echo "  Server port  : $PORT"
echo "  UnrealCV     : $UCV_PORT"
echo "  UnrealMCP    : $UNREAL_PORT"
echo "  Cirrus HTTP  : $CIRRUS_HTTP_PORT"
echo "  Mode         : $NODE_ENV"
echo ""

# ── Build frontend ─────────────────────────────────────────────────────────────
if ! $DEV && ! $NO_BUILD; then
  echo "Building frontend..."
  cd "$SCRIPT_DIR" && npm run build
  echo "Frontend built."
  echo ""
fi

# ── Cleanup on exit ───────────────────────────────────────────────────────────
PIDS=()
cleanup() {
  echo ""
  echo "Stopping services..."
  for pid in "${PIDS[@]}"; do
    kill "$pid" 2>/dev/null || true
  done
  echo "Stopped."
}
trap cleanup EXIT INT TERM

# ── Start backend ──────────────────────────────────────────────────────────────
echo "Starting backend server on :$PORT..."
cd "$SCRIPT_DIR/server"
node index.js &
PIDS+=($!)
sleep 2

# ── Start frontend dev server ──────────────────────────────────────────────────
if $DEV; then
  echo "Starting frontend dev server..."
  cd "$SCRIPT_DIR"
  npm run dev &
  PIDS+=($!)
  sleep 2
fi

# ── Print URLs ─────────────────────────────────────────────────────────────────
echo ""
echo "╔══════════════════════════════════════════╗"
echo "║  SimWorld Studio is running!             ║"
echo "╠══════════════════════════════════════════╣"
if $DEV; then
  echo "║  Open: http://localhost:5173             ║"
else
  echo "║  Open: http://localhost:$PORT              ║"
fi
echo "╚══════════════════════════════════════════╝"
echo ""
echo "Press Ctrl+C to stop all services."

# ── Wait for children ─────────────────────────────────────────────────────────
wait
