#!/usr/bin/env bash
# Run epoch-based navigation experiment with train/test split and WandB.
#
# Usage:
#   bash scripts/run_epoch_experiment.sh                             # defaults
#   bash scripts/run_epoch_experiment.sh --model claude --memory mem0 --epochs 5
#   bash scripts/run_epoch_experiment.sh --help
#
# Env vars:
#   TASK_GEN_DIR   path to task_gen repo  (default: ../../task_gen)
#   UCV_PORT       UnrealCV port          (default: 9001)
#   MCP_PORT       UE MCP port            (default: 55561)
#   WANDB_API_KEY  WandB API key
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
GYM_DIR="$(dirname "$SCRIPT_DIR")"
WORKSPACE_DIR="$(dirname "$GYM_DIR")"
PROJECT_ROOT="$(dirname "$WORKSPACE_DIR")"

export TASK_GEN_DIR="${TASK_GEN_DIR:-/data/koe/task_gen}"
export PYTHONPATH="${TASK_GEN_DIR}:${WORKSPACE_DIR}:${PYTHONPATH:-}"

# Activate venv if it exists
if [ -f "$GYM_DIR/.venv/bin/activate" ]; then
    source "$GYM_DIR/.venv/bin/activate"
fi

cd "$WORKSPACE_DIR"

exec python -m gym_env.epoch_runner \
    --ucv-port "${UCV_PORT:-9001}" \
    --mcp-port "${MCP_PORT:-55561}" \
    "$@"
