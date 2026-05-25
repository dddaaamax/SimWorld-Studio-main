#!/bin/bash
# SimWorld Studio - Launch Script
# Usage: ./SimWorld-Studio.sh [--gpu INDEX] [--render-offscreen] [--port PORT]

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENGINE_DIR="$SCRIPT_DIR/Engine"
PROJECT_DIR="$SCRIPT_DIR/gym_citynav"
PROJECT_FILE="$PROJECT_DIR/gym_citynav.uproject"
UE_EDITOR="$ENGINE_DIR/Binaries/Linux/UnrealEditor"

# Default settings
MCP_PORT=55559
GPU_INDEX=""
RENDER_OFFSCREEN=""
RESOLUTION="-ResX=1280 -ResY=720"
PIXEL_STREAMING_ARGS=""
FPSMAX=""

# Parse arguments
while [[ $# -gt 0 ]]; do
    case $1 in
        --port)
            MCP_PORT="$2"
            shift 2
            ;;
        --gpu)
            GPU_INDEX="$2"
            shift 2
            ;;
        --render-offscreen)
            RENDER_OFFSCREEN="-RenderOffScreen"
            # In headless server mode, throttle the editor to save GPU.
            # Interactive editing should NOT have this — it makes Slate UI laggy.
            FPSMAX="-FPSMAX=15"
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
        --help|-h)
            echo "SimWorld Studio Launcher"
            echo ""
            echo "Usage: $0 [OPTIONS]"
            echo ""
            echo "Options:"
            echo "  --gpu INDEX           GPU index to use (default: 0). Required on multi-GPU systems."
            echo "  --render-offscreen    Run without display window (headless mode)"
            echo "  --port PORT           MCP TCP port (default: 55559)"
            echo "  --pixel-streaming     Enable Pixel Streaming on port 8586"
            echo "  --res WIDTH HEIGHT    Set resolution (default: 1280 720)"
            echo "  --help                Show this help"
            echo ""
            echo "Examples:"
            echo "  $0 --gpu 0 --render-offscreen          # Headless on GPU 0"
            echo "  $0 --gpu 1 --render-offscreen --port 55560  # GPU 1, custom port"
            exit 0
            ;;
        *)
            echo "Unknown option: $1 (use --help for usage)"
            exit 1
            ;;
    esac
done

# Check if editor exists
if [ ! -f "$UE_EDITOR" ]; then
    echo "ERROR: UnrealEditor not found at $UE_EDITOR"
    echo "Make sure you extracted the SimWorld-Studio-Minimal archive correctly."
    exit 1
fi

# Check if project exists
if [ ! -f "$PROJECT_FILE" ]; then
    echo "ERROR: Project file not found at $PROJECT_FILE"
    exit 1
fi

# --- GPU isolation for multi-GPU systems ---
# On multi-GPU servers, Vulkan can crash trying to enumerate all GPUs.
# We isolate to a single GPU using CUDA_VISIBLE_DEVICES and VK_DRIVER_FILES.
if [ -n "$GPU_INDEX" ]; then
    export CUDA_VISIBLE_DEVICES="$GPU_INDEX"

    # Try to find the Vulkan ICD file for GPU isolation
    # This prevents Vulkan from enumerating all GPUs (which causes crashes)
    NVIDIA_ICD="/usr/share/vulkan/icd.d/nvidia_icd.json"
    if [ -f "$NVIDIA_ICD" ]; then
        export VK_ICD_FILENAMES="$NVIDIA_ICD"
    fi

    # Also set the UE graphics adapter flag
    GPU_ADAPTER="-graphicsadapter=$GPU_INDEX"
else
    # Default to GPU 0 on multi-GPU systems to avoid Vulkan enumeration issues
    GPU_COUNT=$(nvidia-smi --query-gpu=count --format=csv,noheader 2>/dev/null | head -1 || echo "1")
    if [ "$GPU_COUNT" -gt 1 ] 2>/dev/null; then
        echo "WARNING: Multi-GPU system detected ($GPU_COUNT GPUs)."
        echo "  Defaulting to GPU 0. Use --gpu INDEX to select a specific GPU."
        echo ""
        export CUDA_VISIBLE_DEVICES="0"
        GPU_ADAPTER="-graphicsadapter=0"

        NVIDIA_ICD="/usr/share/vulkan/icd.d/nvidia_icd.json"
        if [ -f "$NVIDIA_ICD" ]; then
            export VK_ICD_FILENAMES="$NVIDIA_ICD"
        fi
    else
        GPU_ADAPTER=""
    fi
fi

echo "=== SimWorld Studio ==="
echo "  Engine:   $UE_EDITOR"
echo "  Project:  $PROJECT_FILE"
echo "  MCP Port: $MCP_PORT"
echo "  GPU:      ${GPU_INDEX:-auto}"
echo "  Map:      /Game/Maps/Empty"
echo ""

# Launch UE Editor
exec "$UE_EDITOR" "$PROJECT_FILE" \
    /Game/Maps/Empty.umap \
    -MCPPort=$MCP_PORT \
    -NOSPLASH \
    -NOSOUND \
    $RESOLUTION \
    $FPSMAX \
    $GPU_ADAPTER \
    $RENDER_OFFSCREEN \
    $PIXEL_STREAMING_ARGS \
    -log \
    "$@"
