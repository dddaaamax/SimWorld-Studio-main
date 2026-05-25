#!/usr/bin/env bash
#
# Build SimWorld Studio distributable package
#
# 1. Builds frontend (Vite)
# 2. Minifies backend JS files individually (esbuild)
# 3. Copies skills, config, assets
# 4. Creates pip-installable tarball
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ARENA_ROOT="$(cd "$SCRIPT_DIR/simworld_studio_workspace" && pwd)"
PKG_DIR="$SCRIPT_DIR/packaging"
SERVER_SRC="$ARENA_ROOT/web/server"
FRONTEND_SRC="$ARENA_ROOT/web"
PKG_SERVER="$PKG_DIR/simworld_arena/server"
PKG_SKILLS="$PKG_DIR/simworld_arena/skills/builtin"
PKG_CONFIG="$PKG_DIR/simworld_arena/config"
DIST_DIR="$SCRIPT_DIR/dist"

echo "============================================="
echo "  Building SimWorld Studio Package"
echo "============================================="

# ── 0. Check prerequisites ─────────────────────────
command -v node >/dev/null 2>&1 || { echo "ERROR: node not found"; exit 1; }
command -v npm  >/dev/null 2>&1 || { echo "ERROR: npm not found";  exit 1; }

# ── 1. Build frontend ──────────────────────────────
echo ""
echo "[1/5] Building frontend..."
cd "$FRONTEND_SRC"
npm install --no-audit --no-fund 2>/dev/null
npm run build 2>&1 | tail -5
echo "  Frontend built -> web/dist/"

# ── 2. Minify backend JS with esbuild ──────────────
echo ""
echo "[2/5] Minifying backend JS..."

# Install esbuild if needed
cd "$FRONTEND_SRC"
if ! npx esbuild --version >/dev/null 2>&1; then
    npm install --save-dev esbuild 2>/dev/null
fi

# Clean target
rm -rf "$PKG_SERVER"
mkdir -p "$PKG_SERVER"

# First, patch index.js to add frontend static serving
PATCHED_INDEX="/tmp/index_patched.js"
node "$SCRIPT_DIR/patch_server.js" "$SERVER_SRC/index.js" "$PATCHED_INDEX"

# Minify each server JS file individually (NOT bundled — preserves require() paths)
for jsfile in mcp-server.js skills.js scenes.js arena.js agents.js generate-thumbnails.js; do
    if [ -f "$SERVER_SRC/$jsfile" ]; then
        npx esbuild "$SERVER_SRC/$jsfile" \
            --minify \
            --platform=node \
            --target=node18 \
            --outfile="$PKG_SERVER/$jsfile" \
            2>/dev/null
        echo "  Minified: $jsfile"
    fi
done

# Minify patched index.js
npx esbuild "$PATCHED_INDEX" \
    --minify \
    --platform=node \
    --target=node18 \
    --outfile="$PKG_SERVER/index.js" \
    2>/dev/null
echo "  Minified: index.js (with frontend serving patch)"
rm -f "$PATCHED_INDEX"

echo "  Backend JS minified -> server/*.js"

# ── 3. Copy static assets ──────────────────────────
echo ""
echo "[3/5] Copying assets..."

# Frontend dist (pre-built React app)
rm -rf "$PKG_SERVER/dist"
cp -r "$FRONTEND_SRC/dist" "$PKG_SERVER/dist"
echo "  Frontend dist copied."

# Assets catalog
cp "$SERVER_SRC/assets.json" "$PKG_SERVER/"

# Package.json for npm install at runtime (express + cors only)
cat > "$PKG_SERVER/package.json" << 'PKGJSON'
{
  "name": "simworld-studio-server",
  "version": "0.1.0",
  "private": true,
  "dependencies": {
    "cors": "^2.8.5",
    "express": "^4.21.1"
  }
}
PKGJSON

# Skills
rm -rf "$PKG_SKILLS"
mkdir -p "$PKG_SKILLS"
if ls "$ARENA_ROOT/arena/skills/builtin/"*.md >/dev/null 2>&1; then
    cp "$ARENA_ROOT/arena/skills/builtin/"*.md "$PKG_SKILLS/"
    echo "  Skills copied: $(ls "$PKG_SKILLS" | wc -l) files"
else
    echo "  WARNING: No builtin skills found"
fi

# Config
rm -rf "$PKG_CONFIG"
mkdir -p "$PKG_CONFIG"
if [ -f "$ARENA_ROOT/arena/config/arena_default.yaml" ]; then
    cp "$ARENA_ROOT/arena/config/arena_default.yaml" "$PKG_CONFIG/"
    echo "  Config copied."
fi

# ── 4. Verify package structure ────────────────────
echo ""
echo "[4/5] Verifying package structure..."
echo "  Package contents:"
find "$PKG_DIR/simworld_arena" -type f | sort | while read f; do
    size=$(du -h "$f" | cut -f1)
    echo "    $size  ${f#$PKG_DIR/}"
done

# ── 5. Build pip package ───────────────────────────
echo ""
echo "[5/5] Building pip package..."
mkdir -p "$DIST_DIR"
cd "$PKG_DIR"

# Ensure build module is available
pip install --quiet build 2>/dev/null || python3 -m pip install --quiet build 2>/dev/null || true

python3 -m build --sdist --outdir "$DIST_DIR" 2>&1 | tail -5

echo ""
echo "============================================="
echo "  Build complete!"
echo ""
if ls "$DIST_DIR/"*.tar.gz >/dev/null 2>&1; then
    ls -lh "$DIST_DIR/"*.tar.gz
else
    echo "  WARNING: No tarball found in $DIST_DIR"
fi
echo "============================================="
