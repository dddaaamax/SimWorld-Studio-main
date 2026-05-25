#!/usr/bin/env bash
# Setup script for gym_env experiment harness.
# Run from the SimWorld-Studio-Dev root directory.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../../.." && pwd)"
WORKSPACE="$REPO_ROOT/simworld_studio_workspace"

# ── task_gen dependency ──────────────────────────────────────────────
TASK_GEN_DIR="${TASK_GEN_DIR:-$REPO_ROOT/../task_gen}"
if [ -d "$TASK_GEN_DIR" ]; then
    echo "[setup] Installing task_gen from $TASK_GEN_DIR ..."
    pip install -e "$TASK_GEN_DIR"
else
    echo "[setup] WARN: task_gen not found at $TASK_GEN_DIR"
    echo "        Set TASK_GEN_DIR env var or clone it as a sibling directory."
    echo "        e.g. git clone <task_gen_repo> $REPO_ROOT/../task_gen"
fi

# ── Python dependencies ─────────────────────────────────────────────
echo "[setup] Installing gym_env requirements ..."
pip install -r "$WORKSPACE/gym_env/requirements.txt"

# ── Optional: matplotlib for plotting ───────────────────────────────
echo "[setup] Installing optional plotting deps ..."
pip install matplotlib 2>/dev/null || echo "  matplotlib not installed (optional)"

echo ""
echo "[setup] Done. Set your API keys before running experiments:"
echo "  export ANTHROPIC_API_KEY=..."
echo "  export OPENAI_API_KEY=..."
echo "  export DASHSCOPE_API_KEY=..."
echo ""
echo "Then run experiments with:"
echo "  bash $WORKSPACE/gym_env/scripts/run_experiment.sh --help"
