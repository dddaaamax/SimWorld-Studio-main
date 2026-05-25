#!/usr/bin/env bash
# SimWorld Studio - Linux/macOS startup script
# Usage: ./start.sh
#   or with overrides: PORT=3002 UNREAL_PORT=55558 ./start.sh

set -e

export PORT="${PORT:-3002}"
export UCV_PORT="${UCV_PORT:-9001}"
export UNREAL_PORT="${UNREAL_PORT:-55557}"
export UNREAL_HOST="${UNREAL_HOST:-127.0.0.1}"
export CIRRUS_HTTP_PORT="${CIRRUS_HTTP_PORT:-8685}"
export CIRRUS_WS_PORT="${CIRRUS_WS_PORT:-8686}"

echo "Starting SimWorld Studio server..."
echo "  Studio UI  : http://localhost:${PORT}"
echo "  UE TCP     : ${UNREAL_HOST}:${UNREAL_PORT}"
echo "  UCV broker : ${UNREAL_HOST}:${UCV_PORT}"
echo "  Cirrus HTTP: ${CIRRUS_HTTP_PORT}   WS: ${CIRRUS_WS_PORT}"
echo ""

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec node "${SCRIPT_DIR}/index.js"
