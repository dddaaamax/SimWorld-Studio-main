#!/usr/bin/env bash
# Run a single or multi-episode navigation experiment.
#
# Usage:
#   bash scripts/run_experiment.sh                          # defaults
#   bash scripts/run_experiment.sh --model qwen --n-episodes 10 --memory text
#   bash scripts/run_experiment.sh --help                   # show all flags
#
# Env vars:
#   TASK_GEN_DIR   path to task_gen repo  (default: ../../task_gen)
#   UCV_HOST       UnrealCV host          (default: 127.0.0.1)
#   UCV_PORT       UnrealCV port          (default: 9000)
#   MCP_PORT       UE MCP port            (default: 55557)
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
GYM_DIR="$(dirname "$SCRIPT_DIR")"
WORKSPACE_DIR="$(dirname "$GYM_DIR")"
PROJECT_ROOT="$(dirname "$WORKSPACE_DIR")"

export TASK_GEN_DIR="${TASK_GEN_DIR:-$PROJECT_ROOT/task_gen}"
export PYTHONPATH="${TASK_GEN_DIR}:${WORKSPACE_DIR}:${PYTHONPATH:-}"

# Activate venv if it exists
if [ -f "$GYM_DIR/.venv/bin/activate" ]; then
    source "$GYM_DIR/.venv/bin/activate"
fi

cd "$WORKSPACE_DIR"

exec python -m gym_env.runner \
    --ucv-host "${UCV_HOST:-127.0.0.1}" \
    --ucv-port "${UCV_PORT:-9000}" \
    --mcp-port "${MCP_PORT:-55557}" \
    "$@"
