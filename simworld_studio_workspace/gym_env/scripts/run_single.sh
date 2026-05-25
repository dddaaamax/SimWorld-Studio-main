#!/usr/bin/env bash
# Run a single navigation episode.
#
# Usage:
#   bash simworld_studio_workspace/gym_env/scripts/run_single.sh [OPTIONS]
#
# Examples:
#   # Claude with vision, 30 steps
#   bash scripts/run_single.sh --model claude --max-steps 30
#
#   # Qwen via local vLLM, text memory
#   bash scripts/run_single.sh \
#       --model qwen \
#       --base-url http://localhost:8000/v1 \
#       --memory text --n-episodes 5

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
GYM_DIR="$(dirname "$SCRIPT_DIR")"
WORKSPACE_DIR="$(dirname "$GYM_DIR")"
PROJECT_DIR="$(dirname "$WORKSPACE_DIR")"

# Ensure task_gen is on PYTHONPATH
TASK_GEN_DIR="${TASK_GEN_DIR:-$PROJECT_DIR/../task_gen}"
export PYTHONPATH="${TASK_GEN_DIR}:${PYTHONPATH:-}"

cd "$WORKSPACE_DIR"
exec python3 -m gym_env.runner "$@"
