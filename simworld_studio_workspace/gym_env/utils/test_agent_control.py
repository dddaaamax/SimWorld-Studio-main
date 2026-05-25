"""
Test script for SimWorld agent control.
Flow:
  1. Connect to MCP (port 55558) to start PIE mode via Python script
  2. Connect to UnrealCV (port 9000) to spawn and control agents
  3. Run movement/rotation/path tests

Usage:
  1. Start UE with SimWorld project
  2. Wait for UE to fully load
  3. Run: python test_agent_control.py
"""

import socket
import json
import time
import sys
import os
import struct

# Fix Windows console encoding
os.environ['PYTHONIOENCODING'] = 'utf-8'
if sys.platform == 'win32':
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    sys.stderr.reconfigure(encoding='utf-8', errors='replace')


# ---------------------------------------------------------------------------
# MCP TCP client (port 55558) — for executing UE Python scripts
# ---------------------------------------------------------------------------

def mcp_command(cmd_type, params, host='127.0.0.1', port=55558, timeout=30):
    """Send a command to UE via MCP TCP protocol."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(timeout)
    sock.connect((host, port))
    msg = json.dumps({"type": cmd_type, "params": params}) + "\n"
    sock.sendall(msg.encode('utf-8'))

    buf = ""
    while True:
        chunk = sock.recv(4096).decode('utf-8', errors='replace')
        if not chunk:
            break
        buf += chunk
        try:
            result = json.loads(buf)
            sock.close()
            return result
        except json.JSONDecodeError:
            continue
    sock.close()
    return json.loads(buf) if buf.strip() else None


def run_ue_python(script, timeout=30):
    """Execute a Python script in UE via MCP."""
    print(f"  [MCP] Running Python script...")
    result = mcp_command("execute_python_script", {"script": script}, timeout=timeout)
    if result:
        status = result.get("status", "unknown")
        logs = result.get("result", {}).get("python_logs", []) if isinstance(result.get("result"), dict) else []
        print(f"  [MCP] Status: {status}")
        for log in logs:
            print(f"  [MCP] {log.strip()}")
    return result


# ---------------------------------------------------------------------------
# UnrealCV client wrapper with auto-reconnect
# ---------------------------------------------------------------------------

class UCVClient:
    """UnrealCV client with auto-reconnect support."""

    def __init__(self, host='127.0.0.1', port=9000):
        self.host = host
        self.port = port
        self.client = None

    def connect(self):
        import unrealcv
        self.client = unrealcv.Client((self.host, self.port))
        self.client.connect()
        if not self.client.isconnected():
            raise RuntimeError(f"Cannot connect to UnrealCV at {self.host}:{self.port}")
        print(f"[OK] Connected to UnrealCV at {self.host}:{self.port}")

    def ensure_connected(self):
        if self.client and self.client.isconnected():
            return
        print("  [UCV] Reconnecting...")
        for attempt in range(10):
            time.sleep(2)
            try:
                import unrealcv
                self.client = unrealcv.Client((self.host, self.port))
                self.client.connect()
                if self.client.isconnected():
                    print(f"  [UCV] Reconnected (attempt {attempt+1})")
                    return
            except Exception as e:
                if attempt < 9:
                    continue
                raise RuntimeError(f"Failed to reconnect after 10 attempts: {e}")

    def send(self, cmd):
        """Send command with auto-reconnect."""
        print(f"  >> {cmd}")
        self.ensure_connected()
        try:
            resp = self.client.request(cmd, timeout=-1)
        except Exception as e:
            print(f"  << ERROR: {e}")
            # Try reconnect and retry once
            self.ensure_connected()
            try:
                resp = self.client.request(cmd, timeout=-1)
            except Exception as e2:
                print(f"  << ERROR (retry): {e2}")
                return None
        resp_str = str(resp) if resp is not None else "None"
        short = resp_str[:200] if len(resp_str) > 200 else resp_str
        print(f"  << {short}")
        return resp_str

    def disconnect(self):
        if self.client:
            try:
                self.client.disconnect()
            except Exception:
                pass


# ---------------------------------------------------------------------------
# Blueprint paths
# ---------------------------------------------------------------------------

BP_HUMANOID = "/Game/TrafficSystem/Pedestrian/Base_User_Agent.Base_User_Agent_C"
BP_PEDESTRIAN = "/Game/TrafficSystem/Pedestrian/Base_Pedestrian.Base_Pedestrian_C"


# ---------------------------------------------------------------------------
# Test functions
# ---------------------------------------------------------------------------

def test_start_pie():
    """Start PIE (Play In Editor) mode via MCP Python script."""
    print("\n=== STEP 0: Start PIE Mode ===")

    # Check if PIE is already running
    result = run_ue_python("""
import unreal
world = unreal.EditorLevelLibrary.get_editor_world()
print(f"Current world: {world.get_name() if world else 'None'}")
print(f"World type: {world.get_class().get_name() if world else 'None'}")
# Check if we're in PIE
is_playing = unreal.EditorLevelLibrary.get_game_world() is not None
print(f"PIE active: {is_playing}")
""")

    # Start PIE if not already running
    print("\n  Starting PIE...")
    result = run_ue_python("""
import unreal

# Request PIE start
level_editor = unreal.get_editor_subsystem(unreal.LevelEditorSubsystem)
if level_editor:
    # Try to start PIE
    try:
        level_editor.editor_play_simulate()
        print("PIE started via editor_play_simulate")
    except Exception as e:
        print(f"editor_play_simulate failed: {e}")
        try:
            # Alternative: use EditorPlaySettings
            play_settings = unreal.EditorPerProjectUserSettings()
            print(f"Play settings available")
        except Exception as e2:
            print(f"Alternative also failed: {e2}")
else:
    print("LevelEditorSubsystem not available")
""", timeout=15)

    time.sleep(5)  # Wait for PIE to initialize
    return result


def test_spawn_agents(ucv):
    """Spawn humanoid and pedestrian agents via UnrealCV."""
    print("\n=== TEST: Spawn Agents ===")

    # Init character loading
    print("\n  Initializing character compilation...")
    ucv.send("vrun Editor.AsyncSkinnedAssetCompilation 2")
    time.sleep(2)

    name_h = "TestAgent_Humanoid_0"
    print(f"\n  Spawning humanoid: {name_h}")
    ucv.send(f"vset /objects/spawn_bp_asset {BP_HUMANOID} {name_h}")
    time.sleep(3)
    ucv.ensure_connected()  # Spawn may drop connection
    ucv.send(f"vset /object/{name_h}/location 0 0 110")
    time.sleep(0.5)
    ucv.send(f"vset /object/{name_h}/rotation 0 0 0")
    ucv.send(f"vset /object/{name_h}/collision true")
    ucv.send(f"vset /object/{name_h}/object_mobility true")
    time.sleep(1)

    name_p = "TestAgent_Pedestrian_0"
    print(f"\n  Spawning pedestrian: {name_p}")
    ucv.ensure_connected()
    ucv.send(f"vset /objects/spawn_bp_asset {BP_PEDESTRIAN} {name_p}")
    time.sleep(3)
    ucv.ensure_connected()
    ucv.send(f"vset /object/{name_p}/location 500 0 110")
    time.sleep(0.5)
    ucv.send(f"vset /object/{name_p}/rotation 0 0 0")
    ucv.send(f"vset /object/{name_p}/collision true")
    ucv.send(f"vset /object/{name_p}/object_mobility true")
    time.sleep(1)

    # Verify
    print("\n  Checking spawned objects...")
    ucv.ensure_connected()
    resp = ucv.send("vget /objects")
    for name in [name_h, name_p]:
        if resp and name in resp:
            print(f"  [OK] {name} found in scene")
        else:
            print(f"  [FAIL] {name} NOT found")

    return name_h, name_p


def test_get_state(ucv, name):
    """Get agent position and rotation."""
    print(f"\n=== TEST: Get State ({name}) ===")
    loc = ucv.send(f"vget /object/{name}/location")
    rot = ucv.send(f"vget /object/{name}/rotation")
    return loc, rot


def test_movement(ucv, name):
    """Test humanoid movement."""
    print(f"\n=== TEST: Movement ({name}) ===")

    ucv.send(f"vbp {name} SetMaxSpeed 200")
    time.sleep(0.5)

    loc_before = ucv.send(f"vget /object/{name}/location")
    print(f"  Before: {loc_before}")

    print("  Moving forward 3s...")
    ucv.send(f"vbp {name} MoveForward")
    time.sleep(3)
    ucv.send(f"vbp {name} StopAgent")
    time.sleep(0.5)

    loc_after = ucv.send(f"vget /object/{name}/location")
    print(f"  After: {loc_after}")
    print(f"  {'[OK] Moved!' if loc_before != loc_after else '[WARN] No movement'}")


def test_rotation(ucv, name):
    """Test rotation."""
    print(f"\n=== TEST: Rotation ({name}) ===")

    rot_before = ucv.send(f"vget /object/{name}/rotation")
    ucv.send(f"vbp {name} TurnAround 1 90 1")
    time.sleep(2)
    rot_after = ucv.send(f"vget /object/{name}/rotation")
    print(f"  {'[OK] Rotated!' if rot_before != rot_after else '[WARN] No rotation'}")


def test_path(ucv, name):
    """Test path following."""
    print(f"\n=== TEST: Path Follow ({name}) ===")

    loc_before = ucv.send(f"vget /object/{name}/location")
    path = "300,300;600,0;300,-300"
    ucv.send(f"vbp {name} SetPath {path}")
    time.sleep(0.5)
    ucv.send(f"vbp {name} FollowPath")
    time.sleep(5)
    ucv.send(f"vbp {name} StopAgent")
    loc_after = ucv.send(f"vget /object/{name}/location")
    print(f"  {'[OK] Path followed!' if loc_before != loc_after else '[WARN] No movement'}")


def test_pedestrian(ucv, name):
    """Test pedestrian-specific commands."""
    print(f"\n=== TEST: Pedestrian ({name}) ===")

    ucv.send(f"vbp {name} SetMaxSpeed 150")
    loc_before = ucv.send(f"vget /object/{name}/location")

    ucv.send(f"vbp {name} MoveForward")
    time.sleep(3)
    ucv.send(f"vbp {name} StopPedestrian")
    time.sleep(0.5)

    loc_after = ucv.send(f"vget /object/{name}/location")
    print(f"  {'[OK] Pedestrian moved!' if loc_before != loc_after else '[WARN] No movement'}")

    ucv.send(f"vbp {name} Rotate_Angle 1 45 1")
    time.sleep(2)


def test_actions(ucv, name):
    """Test humanoid actions."""
    print(f"\n=== TEST: Actions ({name}) ===")
    ucv.send(f"vbp {name} SitDown")
    time.sleep(2)
    ucv.send(f"vbp {name} StandUp")
    time.sleep(2)
    print("  [OK] Actions executed")


def test_cleanup(ucv, names):
    """Clean up."""
    print(f"\n=== Cleanup ===")
    for name in names:
        ucv.send(f"vset /object/{name}/destroy")
        time.sleep(0.5)
    ucv.send("vset /action/clean_garbage")
    print("  [OK] Done")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    print("=" * 60)
    print("  SimWorld Agent Control Test")
    print("=" * 60)

    # Step 0: Start PIE via MCP
    test_start_pie()

    # Wait for PIE to be ready and UnrealCV to reconnect (PIE restarts UnrealCV)
    print("\n  Waiting for UnrealCV after PIE start...")
    time.sleep(5)

    # Step 1: Connect to UnrealCV
    ucv = UCVClient()
    try:
        ucv.connect()
    except Exception as e:
        print(f"\n[ERROR] Cannot connect to UnrealCV: {e}")
        print("Make sure UE is running with UnrealCV plugin and PIE mode.")
        sys.exit(1)

    try:
        name_h, name_p = test_spawn_agents(ucv)
        time.sleep(2)
        ucv.ensure_connected()

        test_get_state(ucv, name_h)
        test_get_state(ucv, name_p)

        test_movement(ucv, name_h)
        test_rotation(ucv, name_h)
        test_path(ucv, name_h)

        ucv.ensure_connected()
        test_pedestrian(ucv, name_p)

        ucv.ensure_connected()
        test_actions(ucv, name_h)

        ucv.ensure_connected()
        test_cleanup(ucv, [name_h, name_p])

        print("\n" + "=" * 60)
        print("  ALL TESTS COMPLETE")
        print("=" * 60)

    except KeyboardInterrupt:
        print("\n[INTERRUPTED]")
        try:
            test_cleanup(ucv, ["TestAgent_Humanoid_0", "TestAgent_Pedestrian_0"])
        except Exception:
            pass
    except Exception as e:
        print(f"\n[ERROR] {e}")
        import traceback
        traceback.print_exc()
    finally:
        ucv.disconnect()


if __name__ == '__main__':
    main()
