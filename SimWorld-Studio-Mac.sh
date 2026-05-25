#!/usr/bin/env bash
# SimWorld Studio - Mac launcher (web-only, no Unreal Engine backend)
#
# Brings up just the React frontend + Node API on http://localhost:3002.
# UE / UnrealCV / MCP / Pixel Streaming are NOT started. The frontend will
# show "ueConnected: false" everywhere - viewport stays blank, scene-spawn
# tools fail, but the UI, asset drawer JSON, scenes, skills, agent panel
# scaffolding, and all read-only API surfaces are usable for development.
#
# Usage:
#   ./SimWorld-Studio-Mac.sh                # build frontend + start backend
#   ./SimWorld-Studio-Mac.sh --no-build     # skip rebuild (use existing dist/)
#   ./SimWorld-Studio-Mac.sh --dev          # vite dev server on :5173 + backend
#
# Env overrides:
#   PORT=3002    backend port

set -eo pipefail

DEV=false
NO_BUILD=false
for arg in "$@"; do
  case $arg in
    --dev)      DEV=true ;;
    --no-build) NO_BUILD=true ;;
    -h|--help)
      sed -n '2,18p' "$0"; exit 0 ;;
    *) echo "Unknown option: $arg"; exit 1 ;;
  esac
done

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WEB_DIR="$SCRIPT_DIR/simworld_studio_workspace/web"
SERVER_DIR="$WEB_DIR/server"

if [ ! -d "$WEB_DIR" ]; then
  echo "ERROR: web dir not found at $WEB_DIR" >&2
  exit 1
fi

command -v node >/dev/null || { echo "ERROR: node not on PATH (brew install node)"; exit 1; }
command -v npm  >/dev/null || { echo "ERROR: npm not on PATH"; exit 1; }

export PORT="${PORT:-3002}"
# Point UE/UCV at unreachable ports so probes fail fast and stay quiet.
export UNREAL_HOST=127.0.0.1
export UNREAL_PORT="${UNREAL_PORT:-1}"
export UCV_PORT="${UCV_PORT:-1}"
export NODE_ENV="${NODE_ENV:-production}"
$DEV && export NODE_ENV=development

echo ""
echo "SimWorld Studio (Mac, no-UE mode)"
echo "  backend  : http://localhost:$PORT"
$DEV && echo "  frontend : http://localhost:5173 (vite dev)"
echo "  UE / MCP : disabled (web stack only)"
echo ""

if [ ! -d "$SERVER_DIR/node_modules" ]; then
  echo "Installing server deps..."
  (cd "$SERVER_DIR" && npm install --no-audit --no-fund)
fi
if [ ! -d "$WEB_DIR/node_modules" ]; then
  echo "Installing frontend deps..."
  (cd "$WEB_DIR" && npm install --no-audit --no-fund)
fi

if ! $DEV && ! $NO_BUILD; then
  if [ ! -d "$WEB_DIR/dist" ]; then
    echo "Building frontend..."
    (cd "$WEB_DIR" && npm run build)
  else
    echo "Using existing dist/ (delete dist/ to force rebuild)."
  fi
fi

PIDS=()
cleanup() {
  echo ""
  echo "Stopping..."
  for pid in "${PIDS[@]}"; do kill "$pid" 2>/dev/null || true; done
}
trap cleanup EXIT INT TERM

echo "Starting backend on port $PORT ..."
( cd "$SERVER_DIR" && node index.js ) &
PIDS+=($!)

if $DEV; then
  sleep 1
  echo "Starting vite dev server..."
  ( cd "$WEB_DIR" && npm run dev ) &
  PIDS+=($!)
fi

echo ""
if $DEV; then
  echo "-> open http://localhost:5173"
else
  echo "-> open http://localhost:$PORT"
fi
echo "Ctrl+C to stop."

wait
