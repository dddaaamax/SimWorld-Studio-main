#!/usr/bin/env bash
# Analyze completed experiment runs and print a report.
#
# Usage:
#   bash scripts/analyze.sh                           # all runs
#   bash scripts/analyze.sh runs/20260410_*claude*    # specific runs
#   bash scripts/analyze.sh --plot                    # plot learning curves
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

if [[ "${1:-}" == "--plot" ]]; then
    shift
    exec python -m gym_env.plot_learning_curve "$@"
else
    exec python -m gym_env.analyze_runs "$@"
fi
