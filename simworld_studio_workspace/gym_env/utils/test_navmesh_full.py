"""Full end-to-end navmesh test.

Flow:
  1. MCP: wait for editor ready
  2. MCP: place NavMeshBoundsVolume + build navmesh in editor mode
  3. MCP: start PIE
  4. UnrealCV: connect, spawn obstacles, query paths
"""
import sys, time, math

sys.path.insert(0, "simworld_studio_workspace/gym_env")
from mcp_client import MCPClient
from ucv_client import UCVClient

MCP_PORT = 55558
UCV_PORT = 9002

def mcp_exec(mcp, script, label=""):
    """Execute Python in UE editor and print logs."""
    resp = mcp.execute_python(script, timeout=30)
    logs = resp.get("result", {}).get("python_logs", []) if resp else []
    for l in logs:
        print(f"  [UE] {l}")
    return logs

def main():
    # ── 1. Wait for editor ──────────────────────────────────────────────
    print("=== Step 1: Wait for editor ===")
    mcp = MCPClient(port=MCP_PORT)
    if not mcp._wait_until_ready(timeout=120):
        print("  ERROR: editor not ready"); return
    print("  Editor ready!")

    # ── 2. Place NavMeshBoundsVolume via MCP (editor mode) ──────────────
    print("\n=== Step 2: Place NavMeshBoundsVolume in editor ===")
    setup_script = """
import unreal

# Get editor world
world = unreal.EditorLevelLibrary.get_editor_world()
if world is None:
    print('ERROR: no editor world')
else:
    print(f'Editor world: {world.get_name()}')

    # Check if a NavMeshBoundsVolume already exists
    navvols = unreal.GameplayStatics.get_all_actors_of_class(world, unreal.NavMeshBoundsVolume)
    if len(navvols) > 0:
        print(f'NavMeshBoundsVolume already exists: {navvols[0].get_name()}')
        # Resize it to cover a large area
        navvols[0].set_actor_scale3d(unreal.Vector(100, 100, 20))
        navvols[0].set_actor_location(unreal.Vector(0, 0, 0), False, False)
        print('Resized existing volume')
    else:
        # Spawn one via editor subsystem
        eas = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)
        if eas:
            vol = eas.spawn_actor_from_class(unreal.NavMeshBoundsVolume, unreal.Vector(0, 0, 0))
            if vol:
                vol.set_actor_scale3d(unreal.Vector(100, 100, 20))
                print(f'Spawned NavMeshBoundsVolume: {vol.get_name()}')
            else:
                print('ERROR: failed to spawn NavMeshBoundsVolume')
        else:
            print('ERROR: no EditorActorSubsystem')

    # Build navigation
    nav_sys = unreal.NavigationSystemV1.get_navigation_system(world)
    if nav_sys:
        print(f'NavSystem found, building...')
        nav_sys.build()
        print('NavMesh build complete')
    else:
        print('ERROR: no NavigationSystem')
"""
    mcp_exec(mcp, setup_script)

    # ── 3. Start PIE ───────────────────────────────────────────────────
    print("\n=== Step 3: Start PIE ===")
    mcp.start_pie(wait_seconds=8.0)
    print("  PIE started")

    # ── 4. Connect UnrealCV ─────────────────────────────────────────────
    print("\n=== Step 4: Connect UnrealCV ===")
    ucv = UCVClient(port=UCV_PORT, name="nav-test")
    ucv.connect()
    print(f"  Connected on port {UCV_PORT}")

    # ── 5. Check nav commands ───────────────────────────────────────────
    print("\n=== Step 5: Check nav commands ===")
    help_text = ucv.send("vget /unrealcv/help")
    nav_cmds = [l.strip() for l in help_text.split('\n') if '/nav/' in l]
    print(f"  Nav commands: {len(nav_cmds)}")
    if not nav_cmds:
        print("  ERROR: No nav commands!"); ucv.disconnect(); return

    # ── 6. Nav status ───────────────────────────────────────────────────
    print("\n=== Step 6: Nav status ===")
    print(f"  {ucv.send('vget /nav/status')}")

    # ── 7. Query path on empty scene ────────────────────────────────────
    print("\n=== Step 7: Path (0,0,0) -> (2000,0,0) [no obstacles yet] ===")
    resp = ucv.send("vget /nav/path 0 0 0 2000 0 0").strip()
    if resp == "-1":
        print("  NO PATH (navmesh may still be empty)")
    else:
        parts = resp.split("|")
        print(f"  Length: {parts[0]} cm, Waypoints: {len(parts)-1}")
        print("  PASS: Path found on empty scene!")

    # ── 8. Random points ───────────────────────────────────────────────
    print("\n=== Step 8: Random navmesh points ===")
    resp = ucv.send("vget /nav/random_points 5").strip()
    parts = resp.split("|")
    n = int(parts[0]) if parts[0].isdigit() else 0
    print(f"  Sampled {n} points")
    for p in parts[1:min(6, len(parts))]:
        print(f"    {p}")

    # ── 9. Spawn wall + rebuild ─────────────────────────────────────────
    print("\n=== Step 9: Spawn wall at x=1000 ===")
    for i in range(3):
        name = f"TestWall_{i}"
        x, y, z = 1000.0, -300.0 + i * 300.0, 0.0
        ucv.send(f"vset /objects/spawn_cube {name}")
        time.sleep(0.3)
        ucv.send(f"vset /object/{name}/location {x} {y} {z}")
        ucv.send(f"vset /object/{name}/scale 3 3 3")
        print(f"  {name} at ({x},{y},{z})")

    print("  Rebuilding navmesh...")
    ucv.send("vset /nav/build -5000 -5000 -500 5000 5000 2000")
    time.sleep(1)  # let navmesh regenerate

    # ── 10. Path through wall ───────────────────────────────────────────
    print("\n=== Step 10: Path (0,0,0) -> (2000,0,0) [wall at x=1000] ===")
    resp = ucv.send("vget /nav/path 0 0 0 2000 0 0").strip()
    if resp == "-1":
        print("  NO PATH (wall blocks completely)")
    else:
        parts = resp.split("|")
        length = float(parts[0])
        straight = 2000.0
        print(f"  Length: {length:.1f} cm  Straight: {straight:.1f} cm  Ratio: {length/straight:.2f}x")
        if length > straight * 1.1:
            print("  PASS: path detours around wall!")
        for p in parts[1:min(6, len(parts))]:
            print(f"    wp: {p}")

    # ── 11. Reachability ────────────────────────────────────────────────
    print("\n=== Step 11: Reachability tests ===")
    print(f"  (0,0) -> (2000,0): {ucv.send('vget /nav/reachable 0 0 0 2000 0 0').strip()}")
    print(f"  (0,0) -> (-2000,0): {ucv.send('vget /nav/reachable 0 0 0 -2000 0 0').strip()}")

    # ── 12. Project to navmesh ──────────────────────────────────────────
    print("\n=== Step 12: Project to navmesh ===")
    print(f"  (500,500,500) -> {ucv.send('vget /nav/project 500 500 500').strip()}")

    print("\n=== ALL TESTS COMPLETE ===")
    ucv.disconnect()

if __name__ == "__main__":
    main()
