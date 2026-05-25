#!/usr/bin/env bash
# Run a batch experiment across multiple UE instances.
#
# Usage:
#   bash scripts/run_batch.sh --models claude,qwen --ucv-ports 9000,9001 --parallel 2
#   bash scripts/run_batch.sh --help
#
# Env vars:
#   TASK_GEN_DIR   path to task_gen repo  (default: ../../task_gen)
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

exec python -m gym_env.batch "$@"
