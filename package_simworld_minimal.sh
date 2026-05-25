#!/bin/bash
# Package a minimal SimWorld Studio UE Editor distribution
# Strips debug symbols, source code, unnecessary plugins/content
# to create the smallest possible working package.

set -e

UE_ROOT="/data/murray/ue/UE_5.3.2"
PROJECT_ROOT="/data/murray/simworld_projects"
STAGING="/data/murray/SimWorld-Studio-Minimal"
ARCHIVE_DIR="/data/murray"

echo "=== SimWorld Studio Minimal Package Builder ==="
echo ""

# Clean previous staging
if [ -d "$STAGING" ]; then
    echo "Removing previous staging directory..."
    rm -rf "$STAGING"
fi

mkdir -p "$STAGING"

###############################################################################
# 1. Engine Binaries (core .so files — no debug symbols)
###############################################################################
echo "[1/8] Copying Engine binaries (stripping debug symbols)..."
mkdir -p "$STAGING/Engine/Binaries/Linux"

# Copy all .so files except debug/sym/DebugGame
rsync -a \
    --exclude='*.debug' \
    --exclude='*.sym' \
    --exclude='*DebugGame*' \
    "$UE_ROOT/Engine/Binaries/Linux/" \
    "$STAGING/Engine/Binaries/Linux/"

echo "  Engine binaries: $(du -sh "$STAGING/Engine/Binaries/Linux/" | cut -f1)"

###############################################################################
# 2. Engine ThirdParty binaries (runtime dependencies)
###############################################################################
echo "[2/8] Copying ThirdParty binaries..."
mkdir -p "$STAGING/Engine/Binaries/ThirdParty"

# Copy all ThirdParty except known unnecessary ones
rsync -a \
    --exclude='DotNet/' \
    --exclude='Mono/' \
    --exclude='OpenXR/' \
    --exclude='MsQuic/' \
    "$UE_ROOT/Engine/Binaries/ThirdParty/" \
    "$STAGING/Engine/Binaries/ThirdParty/"

echo "  ThirdParty: $(du -sh "$STAGING/Engine/Binaries/ThirdParty/" | cut -f1)"

###############################################################################
# 3. Engine Config & Shaders (essential)
###############################################################################
echo "[3/8] Copying Engine Config & Shaders..."
rsync -a "$UE_ROOT/Engine/Config/" "$STAGING/Engine/Config/"
rsync -a "$UE_ROOT/Engine/Shaders/" "$STAGING/Engine/Shaders/"

echo "  Config: $(du -sh "$STAGING/Engine/Config/" | cut -f1)"
echo "  Shaders: $(du -sh "$STAGING/Engine/Shaders/" | cut -f1)"

###############################################################################
# 4. Engine Content (selective — skip localization, VR, tutorials)
###############################################################################
echo "[4/8] Copying Engine Content (selective)..."
mkdir -p "$STAGING/Engine/Content"

rsync -a \
    --exclude='Localization/' \
    --exclude='Internationalization/' \
    --exclude='VREditor/' \
    --exclude='StarterContent/' \
    --exclude='Tutorial/' \
    --exclude='FbxEditorAutomation/' \
    --exclude='MapTemplates/' \
    --exclude='MobileResources/' \
    "$UE_ROOT/Engine/Content/" \
    "$STAGING/Engine/Content/"

echo "  Content: $(du -sh "$STAGING/Engine/Content/" | cut -f1)"

###############################################################################
# 5. Engine Plugins (keep all but strip Source/Intermediate/debug)
###############################################################################
echo "[5/8] Copying Engine Plugins (stripped)..."
mkdir -p "$STAGING/Engine/Plugins"

# Copy all plugins but exclude heavy unnecessary parts
rsync -a \
    --exclude='Source/' \
    --exclude='Intermediate/' \
    --exclude='Documentation/' \
    --exclude='*.debug' \
    --exclude='*.sym' \
    --exclude='*DebugGame*' \
    --exclude='Extras/' \
    --exclude='VirtualProduction/' \
    --exclude='Tests/' \
    --exclude='Bridge/' \
    "$UE_ROOT/Engine/Plugins/" \
    "$STAGING/Engine/Plugins/"

echo "  Plugins: $(du -sh "$STAGING/Engine/Plugins/" | cut -f1)"

###############################################################################
# 6. Engine Programs (CrashReportClient, etc — minimal)
###############################################################################
echo "[6/8] Copying essential Engine programs..."
# UE needs some program binaries
if [ -d "$UE_ROOT/Engine/Binaries/Linux/CrashReportClient" ]; then
    cp -a "$UE_ROOT/Engine/Binaries/Linux/CrashReportClient" "$STAGING/Engine/Binaries/Linux/" 2>/dev/null || true
fi

###############################################################################
# 7. Project files (minimal content)
###############################################################################
echo "[7/8] Copying project files (minimal)..."

PROJECT_STAGING="$STAGING/Project"
mkdir -p "$PROJECT_STAGING"

# Copy the uproject file
cp "$PROJECT_ROOT/gym_citynav.uproject" "$PROJECT_STAGING/"

# Copy project Config
rsync -a "$PROJECT_ROOT/Config/" "$PROJECT_STAGING/Config/"

# Copy project Binaries (compiled .so modules)
mkdir -p "$PROJECT_STAGING/Binaries/Linux"
cp "$PROJECT_ROOT/Binaries/Linux/"*.so "$PROJECT_STAGING/Binaries/Linux/"

# Copy ONLY the UnrealMCP plugin (essential for Studio)
mkdir -p "$PROJECT_STAGING/Plugins"
rsync -a \
    --exclude='Intermediate/' \
    --exclude='Source/' \
    --exclude='*.debug' \
    "$PROJECT_ROOT/Plugins/UnrealMCP/" \
    "$PROJECT_STAGING/Plugins/UnrealMCP/"

# Copy minimal Content: CityDatabase + Empty map only
mkdir -p "$PROJECT_STAGING/Content/Maps"
mkdir -p "$PROJECT_STAGING/Content/CityDatabase"

# Empty map
cp "$PROJECT_ROOT/Content/Maps/Empty.umap" "$PROJECT_STAGING/Content/Maps/"
cp "$PROJECT_ROOT/Content/Maps/EmptyMap.umap" "$PROJECT_STAGING/Content/Maps/" 2>/dev/null || true

# CityDatabase (buildings, meshes, materials) — skip MyBuilding (1.5GB)
rsync -a \
    --exclude='MyBuilding/' \
    "$PROJECT_ROOT/Content/CityDatabase/" \
    "$PROJECT_STAGING/Content/CityDatabase/"

# Additional content categories (crowds, avatars, characters, etc.)
for CONTENT_DIR in CitySampleCrowd Human_Avatar Characters Robot_Dog TrafficSystem Agent; do
    if [ -d "$PROJECT_ROOT/Content/$CONTENT_DIR" ]; then
        echo "  Copying $CONTENT_DIR..."
        rsync -a "$PROJECT_ROOT/Content/$CONTENT_DIR/" "$PROJECT_STAGING/Content/$CONTENT_DIR/"
    fi
done

# Tree and vehicle asset sources (referenced by blueprints)
for CONTENT_DIR in EuropeanHornbeam Scooters ScooterAssets Industrial_Carts GasStation Camping_Pack; do
    if [ -d "$PROJECT_ROOT/Content/$CONTENT_DIR" ]; then
        echo "  Copying $CONTENT_DIR..."
        rsync -a "$PROJECT_ROOT/Content/$CONTENT_DIR/" "$PROJECT_STAGING/Content/$CONTENT_DIR/"
    fi
done

# Copy essential engine-level content referenced by the project
# (DefaultGameModeBP, basic BPs)
for f in DefaultGameModeBP.uasset BP_Normal.uasset BP_Target.uasset; do
    if [ -f "$PROJECT_ROOT/Content/$f" ]; then
        cp "$PROJECT_ROOT/Content/$f" "$PROJECT_STAGING/Content/"
    fi
done

# Copy Source directory (has the module build files needed)
rsync -a "$PROJECT_ROOT/Source/" "$PROJECT_STAGING/Source/"

echo "  Project: $(du -sh "$PROJECT_STAGING/" | cut -f1)"

###############################################################################
# 8. Create launch script
###############################################################################
echo "[8/8] Creating launch script..."

cat > "$STAGING/SimWorld-Studio.sh" << 'LAUNCH_EOF'
#!/bin/bash
# SimWorld Studio - Minimal Launch Script
# Usage: ./SimWorld-Studio.sh [--port PORT] [--render-offscreen]

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENGINE_DIR="$SCRIPT_DIR/Engine"
PROJECT_DIR="$SCRIPT_DIR/Project"
PROJECT_FILE="$PROJECT_DIR/gym_citynav.uproject"
UE_EDITOR="$ENGINE_DIR/Binaries/Linux/UnrealEditor"

# Default settings
MCP_PORT=55559
RENDER_OFFSCREEN=""
RESOLUTION="-ResX=1280 -ResY=720"
PIXEL_STREAMING_ARGS=""

# Parse arguments
while [[ $# -gt 0 ]]; do
    case $1 in
        --port)
            MCP_PORT="$2"
            shift 2
            ;;
        --render-offscreen)
            RENDER_OFFSCREEN="-RenderOffScreen"
            shift
            ;;
        --pixel-streaming)
            PIXEL_STREAMING_ARGS="-PixelStreamingIP=127.0.0.1 -PixelStreamingPort=8586"
            shift
            ;;
        --res)
            RESOLUTION="-ResX=$2 -ResY=$3"
            shift 3
            ;;
        --help)
            echo "SimWorld Studio Launcher"
            echo ""
            echo "Usage: $0 [OPTIONS]"
            echo ""
            echo "Options:"
            echo "  --port PORT           MCP TCP port (default: 55559)"
            echo "  --render-offscreen    Run without display window"
            echo "  --pixel-streaming     Enable Pixel Streaming on port 8586"
            echo "  --res WIDTH HEIGHT    Set resolution (default: 1280 720)"
            echo "  --help                Show this help"
            exit 0
            ;;
        *)
            echo "Unknown option: $1"
            exit 1
            ;;
    esac
done

# Check if editor exists
if [ ! -f "$UE_EDITOR" ]; then
    echo "ERROR: UnrealEditor not found at $UE_EDITOR"
    exit 1
fi

# Check if project exists
if [ ! -f "$PROJECT_FILE" ]; then
    echo "ERROR: Project file not found at $PROJECT_FILE"
    exit 1
fi

echo "=== SimWorld Studio ==="
echo "  Engine: $UE_EDITOR"
echo "  Project: $PROJECT_FILE"
echo "  MCP Port: $MCP_PORT"
echo "  Map: /Game/Maps/Empty"
echo ""

# Launch UE Editor
exec "$UE_EDITOR" "$PROJECT_FILE" \
    /Game/Maps/Empty.umap \
    -MCPPort=$MCP_PORT \
    -Unattended \
    -NOSPLASH \
    -NOSOUND \
    -Messaging \
    $RESOLUTION \
    $RENDER_OFFSCREEN \
    $PIXEL_STREAMING_ARGS \
    -log \
    "$@"
LAUNCH_EOF

chmod +x "$STAGING/SimWorld-Studio.sh"

###############################################################################
# Summary
###############################################################################
echo ""
echo "=== Package Summary ==="
echo ""
du -sh "$STAGING/Engine/Binaries/Linux/" | awk '{print "  Engine Binaries:  " $1}'
du -sh "$STAGING/Engine/Binaries/ThirdParty/" | awk '{print "  ThirdParty:       " $1}'
du -sh "$STAGING/Engine/Config/" | awk '{print "  Engine Config:    " $1}'
du -sh "$STAGING/Engine/Shaders/" | awk '{print "  Engine Shaders:   " $1}'
du -sh "$STAGING/Engine/Content/" | awk '{print "  Engine Content:   " $1}'
du -sh "$STAGING/Engine/Plugins/" | awk '{print "  Engine Plugins:   " $1}'
du -sh "$STAGING/Project/" | awk '{print "  Project:          " $1}'
echo "  ---"
du -sh "$STAGING/" | awk '{print "  TOTAL:            " $1}'
echo ""
echo "Staging directory: $STAGING"
echo ""
echo "To compress: tar czf SimWorld-Studio-Minimal.tar.gz -C /data/murray SimWorld-Studio-Minimal"
