#!/usr/bin/env bash
# Setup Python environment for SimWorld gym_env experiments.
# Usage:  source scripts/setup_env.sh
#   or:   bash scripts/setup_env.sh   (creates venv but won't activate in caller)
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
GYM_DIR="$(dirname "$SCRIPT_DIR")"
WORKSPACE_DIR="$(dirname "$GYM_DIR")"
PROJECT_ROOT="$(dirname "$WORKSPACE_DIR")"

# ── task_gen path ──────────────────────────────────────────────────
# Override with TASK_GEN_DIR env var if your task_gen repo lives elsewhere.
export TASK_GEN_DIR="${TASK_GEN_DIR:-$PROJECT_ROOT/task_gen}"

if [ ! -d "$TASK_GEN_DIR" ]; then
    echo "WARNING: task_gen not found at $TASK_GEN_DIR"
    echo "  Set TASK_GEN_DIR to point at your local task_gen clone."
fi

# ── Virtual environment ───────────────────────────────────────────
VENV_DIR="$GYM_DIR/.venv"
if [ ! -d "$VENV_DIR" ]; then
    echo "Creating virtualenv at $VENV_DIR ..."
    python3 -m venv "$VENV_DIR"
fi
source "$VENV_DIR/bin/activate"

# ── Install dependencies ─────────────────────────────────────────
pip install -q -r "$GYM_DIR/requirements.txt"

if [ -d "$TASK_GEN_DIR" ]; then
    pip install -q -e "$TASK_GEN_DIR"
    echo "Installed task_gen from $TASK_GEN_DIR"
fi

# ── PYTHONPATH ────────────────────────────────────────────────────
export PYTHONPATH="${TASK_GEN_DIR}:${WORKSPACE_DIR}:${PYTHONPATH:-}"

echo ""
echo "Environment ready."
echo "  TASK_GEN_DIR = $TASK_GEN_DIR"
echo "  PYTHONPATH   = $PYTHONPATH"
echo "  Python       = $(which python)"
echo ""
echo "Next steps:"
echo "  1. Start UE editor with UnrealCV on :9000"
echo "  2. Run:  bash scripts/run_experiment.sh --help"
