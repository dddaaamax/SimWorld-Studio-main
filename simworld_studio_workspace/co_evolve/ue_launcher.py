"""UE launcher: correct startup sequence for co-evolution experiments.

CRITICAL ORDER:
  1. Start UE Editor (loads agent_test map)
  2. Wait for MCP port
  3. IN EDITOR MODE (before PIE):
     a. Spawn ground plane (StaticMeshActor for walkable surface)
     b. Spawn NavMeshBoundsVolume (defines navmesh coverage area)
     c. Build NavMesh (nav_sys.build())
  4. Start PIE (editor_play_simulate)
  5. Wait for UnrealCV
  6. Verify NavMesh works (random_points returns non-zero)

Why this order matters:
  - NavMesh must be built in EDITOR mode. Building in PIE mode produces
    empty navmesh (size 0) because PIE's game world doesn't inherit
    editor-built nav data properly when built after PIE starts.
  - NavMeshBoundsVolume must exist BEFORE build. Without it, navmesh
    has no bounds to cover and produces size 0.
  - Ground plane must exist BEFORE build. NavMesh needs walkable
    geometry to generate navigation polygons.
  - PIE must NOT be restarted during experiment. Restarting PIE
    breaks UnrealCV's single-client TCP listener.
"""
from __future__ import annotations

import logging
import subprocess
import time
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

import os

UE_EDITOR = os.environ.get("UE_EDITOR", "")
UE_PROJECT = os.environ.get("UE_PROJECT", "")
UCV_PORT = int(os.environ.get("UCV_PORT", "9002"))
MCP_PORT = int(os.environ.get("MCP_PORT", "55558"))


def wait_for_port(port: int, timeout: float = 200.0) -> bool:
    """Wait until a TCP port accepts connections."""
    import socket
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            s = socket.socket()
            s.settimeout(2)
            s.connect(("127.0.0.1", port))
            s.close()
            return True
        except Exception:
            time.sleep(5)
    return False


def setup_editor_scene(mcp_port: int = MCP_PORT):
    """In EDITOR mode (before PIE): spawn ground, nav volume, build navmesh.

    This must be called BEFORE start_pie().
    """
    from gym_env.mcp_client import MCPClient
    mcp = MCPClient(port=mcp_port, timeout=20)

    # Step 1: Use the map's existing Floor for NavMesh (no extra ground needed)
    log.info("Using existing map floor for NavMesh (skipping ground plane spawn)")

    # Step 2: Spawn NavMeshBoundsVolume
    log.info("Spawning NavMeshBoundsVolume in editor...")
    volume_script = '''
import unreal
loc = unreal.Vector(0, 0, 0)
rot = unreal.Rotator(0, 0, 0)
vol = unreal.EditorLevelLibrary.spawn_actor_from_class(
    unreal.NavMeshBoundsVolume, loc, rot)
if vol:
    vol.set_actor_scale3d(unreal.Vector(100, 100, 10))
    print('VOLUME_OK')
else:
    print('VOLUME_FAILED')
'''
    resp = mcp.execute_python(volume_script, timeout=15)
    _log_mcp(resp, "NavVolume")

    time.sleep(2)

    # Step 3: Build NavMesh
    log.info("Building NavMesh in editor...")
    build_script = '''
import unreal
world = unreal.get_editor_subsystem(unreal.UnrealEditorSubsystem).get_editor_world()
nav_sys = unreal.NavigationSystemV1.get_navigation_system(world)
if nav_sys:
    # UE5 Python does not expose Build(); use console command instead.
    unreal.SystemLibrary.execute_console_command(world, 'RebuildNavigation')
    print('NAVMESH_BUILT')
else:
    print('NO_NAV_SYS')
'''
    resp = mcp.execute_python(build_script, timeout=15)
    _log_mcp(resp, "NavMesh")

    time.sleep(3)
    log.info("Editor scene setup complete")


def start_pie(mcp_port: int = MCP_PORT):
    """Start Play-In-Editor. Call AFTER setup_editor_scene()."""
    from gym_env.mcp_client import MCPClient
    mcp = MCPClient(port=mcp_port, timeout=20)

    log.info("Starting PIE...")
    mcp.execute_python(
        'import unreal; '
        'unreal.get_editor_subsystem(unreal.LevelEditorSubsystem)'
        '.editor_play_simulate(); print("PIE_OK")',
        timeout=15,
    )
    time.sleep(15)
    log.info("PIE started")


def verify_navmesh(ucv_port: int = UCV_PORT) -> bool:
    """Verify NavMesh works after PIE start. Returns True if OK."""
    from gym_env.ucv_client import UCVClient
    ucv = UCVClient(host="127.0.0.1", port=ucv_port, name="verify")
    ucv.connect()

    status = ucv.send("vget /nav/status")
    log.info("Nav status: %s", status)

    pts = ucv.send("vget /nav/random_points 5")
    parts = pts.split("|")
    non_zero = [p for p in parts[1:] if p != "0.00,0.00,0.00"]
    ok = len(non_zero) > 0
    log.info("NavMesh verify: %d/%d non-zero points — %s",
             len(non_zero), len(parts) - 1, "OK" if ok else "FAILED")

    ucv.disconnect()
    return ok


def full_startup(
    ucv_port: int = UCV_PORT,
    mcp_port: int = MCP_PORT,
    start_ue: bool = True,
) -> bool:
    """Complete startup sequence. Returns True if everything works.

    1. Start UE Editor
    2. Wait for MCP
    3. Setup editor scene (ground + nav volume + navmesh build)
    4. Start PIE
    5. Wait for UnrealCV
    6. Verify NavMesh

    Call this ONCE at the beginning of an experiment.
    DO NOT call again during the experiment.
    """
    import os

    if start_ue:
        # Clean stale state
        if UE_PROJECT:
            logs_dir = Path(UE_PROJECT).parent / "Saved" / "Logs"
            if logs_dir.exists():
                import shutil
                shutil.rmtree(logs_dir, ignore_errors=True)

        log.info("Starting UE Editor...")
        subprocess.Popen(
            [UE_EDITOR, UE_PROJECT],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )

    # Wait for MCP
    log.info("Waiting for MCP port %d...", mcp_port)
    if not wait_for_port(mcp_port, timeout=200):
        log.error("MCP port not available")
        return False
    log.info("MCP ready")

    # Setup scene in editor mode
    setup_editor_scene(mcp_port)

    # Start PIE
    start_pie(mcp_port)

    # Wait for UnrealCV
    log.info("Waiting for UnrealCV port %d...", ucv_port)
    if not wait_for_port(ucv_port, timeout=60):
        log.error("UnrealCV port not available after PIE start")
        return False

    # Verify
    ok = verify_navmesh(ucv_port)
    if ok:
        log.info("=== STARTUP COMPLETE — ready for co-evolution ===")
    else:
        log.error("=== STARTUP FAILED — NavMesh not working ===")
    return ok


def _log_mcp(resp: dict, label: str):
    logs = resp.get("result", {}).get("python_logs", [])
    for l in logs:
        l = l.strip()
        if l:
            log.info("[%s] %s", label, l)


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-5s %(name)s | %(message)s",
    )
    ok = full_startup()
    print(f"\nStartup {'OK' if ok else 'FAILED'}")
