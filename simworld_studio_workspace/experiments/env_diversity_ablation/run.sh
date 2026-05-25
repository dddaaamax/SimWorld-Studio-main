#!/usr/bin/env bash
# Environment Diversity Ablation Study — Full Pipeline
#
# Prerequisites:
#   - UE running with simworld_studio_projects
#   - Qwen3.5-27B served at http://132.239.95.133:8001/v1
#
# Adjust MCP_PORT / UCV_PORT to your assigned ports.

set -euo pipefail
cd "$(dirname "$0")/../.."   # → simworld_studio_workspace/

MCP_PORT=${MCP_PORT:-55557}
UCV_PORT=${UCV_PORT:-9001}

echo "============================================"
echo "Step 1: Copy maps to UE Content directory"
echo "============================================"
python -m experiments.env_diversity_ablation.prepare_maps

echo ""
echo "============================================"
echo "Step 2: Generate tasks (requires UE running)"
echo "============================================"
echo "!! Restart UE now if needed to pick up new maps !!"
echo "Press Enter when UE is ready..."
read -r

python -m experiments.env_diversity_ablation.generate_tasks \
    --mcp-port "$MCP_PORT" --ucv-port "$UCV_PORT"

echo ""
echo "============================================"
echo "Step 3: Compose ablation conditions"
echo "============================================"
python -m experiments.env_diversity_ablation.compose_conditions

echo ""
echo "============================================"
echo "Step 4: Dry run — check conditions"
echo "============================================"
python -m experiments.env_diversity_ablation.run_ablation --dry-run

echo ""
echo "Ready to run the ablation. Execute:"
echo "  python -m experiments.env_diversity_ablation.run_ablation \\"
echo "    --condition 1 5 10 15 --epochs 2 \\"
echo "    --mcp-port $MCP_PORT --ucv-port $UCV_PORT"
