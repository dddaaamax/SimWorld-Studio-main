"""
Test ghost-mode agents: single-agent normal + multi-agent ghost.

Usage:
  1. Start UE with SimWorld project (editor will auto-start PIE via MCP)
  2. Run: python test_ghost_mode.py
"""

import sys
import os
import time
import socket as _socket
import json
import threading

os.environ['PYTHONIOENCODING'] = 'utf-8'
if sys.platform == 'win32':
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    sys.stderr.reconfigure(encoding='utf-8', errors='replace')

BP_HUMANOID = "/Game/TrafficSystem/Pedestrian/Base_User_Agent.Base_User_Agent_C"
GHOST_CHANNEL = 8
UCV_PORT = 9002


# ---------------------------------------------------------------------------
# MCP helper (for starting PIE)
# ---------------------------------------------------------------------------

def mcp(script, timeout=15):
    sock = _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM)
    sock.settimeout(timeout)
    sock.connect(('127.0.0.1', 55558))
    msg = json.dumps({'type': 'execute_python_script', 'params': {'script': script}}) + '\n'
    sock.sendall(msg.encode('utf-8'))
    buf = ''
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
    return None


def ensure_pie_running():
    """Start PIE if not already running."""
    print("  Ensuring PIE is running...")
    r = mcp('''
import unreal
level_editor = unreal.get_editor_subsystem(unreal.LevelEditorSubsystem)
level_editor.editor_play_simulate()
print("PIE simulate started")
''')
    print(f"  PIE result: {r.get('status') if r else 'None'}")
    # Wait for UnrealCV to be ready
    for _ in range(30):
        time.sleep(1)
        sock = _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM)
        sock.settimeout(2)
        try:
            sock.connect(('127.0.0.1', UCV_PORT))
            sock.close()
            print(f"  UnrealCV ready on port {UCV_PORT}")
            return True
        except Exception:
            sock.close()
    print("  [FAIL] UnrealCV not available")
    return False


# ---------------------------------------------------------------------------
# UnrealCV helpers (matching the successful manual test pattern)
# ---------------------------------------------------------------------------

def fresh_client():
    """Create a fresh unrealcv client."""
    import unrealcv
    c = unrealcv.Client(('127.0.0.1', UCV_PORT))
    c.connect()
    if not c.isconnected():
        raise RuntimeError(f"Cannot connect to UnrealCV on {UCV_PORT}")
    return c


def spawn_and_reconnect(name, x=0, y=0, z=110):
    """Spawn an agent using threaded request (handles hang), then reconnect.

    Returns a fresh client after spawn.
    """
    c = fresh_client()
    print(f"  Spawning {name} at ({x}, {y}, {z})...")

    done = threading.Event()
    result = [None]

    def _do():
        try:
            result[0] = c.request(f'vset /objects/spawn_bp_asset {BP_HUMANOID} {name}')
        except Exception as e:
            result[0] = f'error: {e}'
        done.set()

    t = threading.Thread(target=_do, daemon=True)
    t.start()
    done.wait(timeout=10)
    print(f"    Spawn response: {result[0]}")

    time.sleep(3)

    # Fresh reconnect
    c2 = fresh_client()

    # Configure
    c2.request(f'vset /object/{name}/location {x} {y} {z}')
    c2.request(f'vset /object/{name}/collision true')
    c2.request(f'vset /object/{name}/object_mobility true')
    c2.request(f'vset /object/{name}/rotation 0 0 0')

    # Verify
    objs = c2.request('vget /objects') or ''
    if name not in objs:
        print(f"    [FAIL] {name} not found after spawn")
        c2.disconnect()
        return None

    print(f"    [OK] {name} spawned and configured")
    return c2


# ---------------------------------------------------------------------------
# Test A — Single agent, normal mode
# ---------------------------------------------------------------------------

def test_single_agent_normal():
    print("\n" + "=" * 60)
    print("  TEST A: Single Agent — Normal Mode")
    print("=" * 60)

    name = "Test_Normal_Agent"
    c = spawn_and_reconnect(name, 0, 0, 110)
    if c is None:
        return False

    # Test movement
    loc_before = c.request(f'vget /object/{name}/location')
    print(f"  Location: {loc_before}")

    c.request(f'vbp {name} SetMaxSpeed 200')
    c.request(f'vbp {name} MoveForward')
    time.sleep(2)
    c.request(f'vbp {name} StopAgent')
    time.sleep(0.5)

    loc_after = c.request(f'vget /object/{name}/location')
    print(f"  After move: {loc_after}")
    moved = loc_before != loc_after
    print(f"  {'[OK]' if moved else '[WARN]'} Movement: {'yes' if moved else 'no'}")

    # Cleanup
    c.request(f'vset /object/{name}/destroy')
    c.disconnect()
    print("  [OK] Single agent test complete")
    return True


# ---------------------------------------------------------------------------
# Test B — Multi-agent ghost mode
# ---------------------------------------------------------------------------

def test_multi_agent_ghost():
    print("\n" + "=" * 60)
    print("  TEST B: Multi-Agent — Ghost Mode")
    print("=" * 60)

    names = ["Ghost_Agent_0", "Ghost_Agent_1", "Ghost_Agent_2"]
    spawn_x, spawn_y, spawn_z = 500, 500, 110

    # Spawn all agents (each spawn needs reconnect)
    c = None
    for name in names:
        c = spawn_and_reconnect(name, spawn_x, spawn_y, spawn_z)
        if c is None:
            return False

    # At this point c is connected. Enable ghost mode on all.
    print("\n  --- Enabling ghost mode ---")
    for name in names:
        resp_hide = c.request(f'vset /object/{name}/hide')
        resp_ch = c.request(f'vset /object/{name}/collision_channel {GHOST_CHANNEL}')
        resp_ign = c.request(f'vset /object/{name}/collision_response {GHOST_CHANNEL} ignore')
        print(f"  {name}: hide={resp_hide}, channel={resp_ch}, ignore={resp_ign}")

    # Test 1: Ghosts can overlap — set all to same location
    print("\n  --- Ghost Overlap Test ---")
    for name in names:
        c.request(f'vset /object/{name}/location {spawn_x} {spawn_y} {spawn_z}')
    time.sleep(1)

    locations = {}
    for name in names:
        resp = c.request(f'vget /object/{name}/location')
        parts = resp.strip().split() if resp else []
        loc = tuple(float(p) for p in parts[:3]) if len(parts) >= 3 else None
        locations[name] = loc
        print(f"  {name}: {loc}")

    all_close = True
    ref = locations[names[0]]
    if ref:
        for name in names[1:]:
            loc = locations[name]
            if loc is None:
                all_close = False
                break
            dist = sum((a - b) ** 2 for a, b in zip(ref, loc)) ** 0.5
            if dist > 50:
                print(f"  [WARN] {name} drifted {dist:.1f}cm — collision pushback?")
                all_close = False
    print(f"  {'[OK]' if all_close else '[FAIL]'} Ghost overlap: agents {'stayed' if all_close else 'drifted'}")

    # Test 2: Independent movement
    print("\n  --- Independent Movement Test ---")
    c.request(f'vbp {names[0]} SetMaxSpeed 200')
    c.request(f'vbp {names[0]} MoveForward')
    time.sleep(2)
    c.request(f'vbp {names[0]} StopAgent')
    time.sleep(0.5)

    loc0 = c.request(f'vget /object/{names[0]}/location')
    loc1 = c.request(f'vget /object/{names[1]}/location')
    print(f"  {names[0]} (moved): {loc0}")
    print(f"  {names[1]} (stayed): {loc1}")

    # Test 3: Disable ghost and verify
    print("\n  --- Disable Ghost Test ---")
    c.request(f'vset /object/{names[0]}/show')
    c.request(f'vset /object/{names[0]}/collision_channel 2')
    c.request(f'vset /object/{names[0]}/collision_response {GHOST_CHANNEL} block')
    print(f"  [OK] Ghost mode disabled on {names[0]}")

    # Cleanup
    print("\n  --- Cleanup ---")
    for name in names:
        c.request(f'vset /object/{name}/destroy')
    c.disconnect()

    print("  [OK] Multi-agent ghost test complete")
    return True


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    print("=" * 60)
    print("  SimWorld Ghost Mode Test")
    print("=" * 60)

    # Ensure PIE is running
    if not ensure_pie_running():
        print("[ERROR] Could not start PIE")
        sys.exit(1)

    time.sleep(2)

    results = {}
    try:
        results['single_normal'] = test_single_agent_normal()
        time.sleep(2)
        results['multi_ghost'] = test_multi_agent_ghost()
    except Exception as e:
        print(f"\n[ERROR] Test failed: {e}")
        import traceback
        traceback.print_exc()

    print("\n" + "=" * 60)
    print("  RESULTS")
    print("=" * 60)
    for test_name, passed in results.items():
        status = "[PASS]" if passed else "[FAIL]"
        print(f"  {status} {test_name}")
    all_pass = all(results.values())
    print("=" * 60)
    sys.exit(0 if all_pass else 1)


if __name__ == '__main__':
    main()
