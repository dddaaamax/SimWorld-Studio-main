"""End-to-end ghost agent spawn test.

Usage:
    python -m gym_env.utils.test_ghost_spawn --ucv-port 9002

Spawns 3 ghost agents, tests hide + collision commands, prints every
response from UE so we can see which commands succeed / fail.
"""

from __future__ import annotations

import argparse
import sys
import time

def main():
    p = argparse.ArgumentParser()
    p.add_argument("--ucv-port", type=int, default=9002)
    p.add_argument("--n-agents", type=int, default=3)
    args = p.parse_args()

    from gym_env.ucv_client import UCVClient

    ucv = UCVClient(host="127.0.0.1", port=args.ucv_port, name="ghost-test")
    ucv.connect()
    print(f"[OK] Connected to UnrealCV on port {args.ucv_port}")

    # Cleanup any leftover agents
    actors = ucv.send("vget /objects")
    leftovers = [a for a in actors.split()
                 if "Ghost" in a or "TestGhost" in a or "GymNav" in a]
    for a in leftovers:
        try:
            ucv.send(f"vset /object/{a}/destroy")
            print(f"[CLEANUP] destroyed {a}")
        except Exception:
            pass

    BP = "/Game/TrafficSystem/Pedestrian/Base_User_Agent.Base_User_Agent_C"
    # Spawn positions spread apart so they're visible
    positions = [
        (0, 0, 110),
        (500, 0, 110),
        (0, 500, 110),
    ]

    agent_names = []
    for i in range(args.n_agents):
        name = f"TestGhost_{i}"
        x, y, z = positions[i % len(positions)]
        print(f"\n{'='*60}")
        print(f"SPAWNING {name} at ({x}, {y}, {z})")
        print(f"{'='*60}")

        # Spawn with location
        ucv.spawn_bp_asset(BP, name, location=(x, y, z))
        print(f"[OK] spawn_bp_asset done for {name}")

        # Verify it exists
        actors = ucv.send("vget /objects")
        if name in actors:
            print(f"[OK] {name} found in scene")
        else:
            # Check for UE-renamed variant
            matches = [a for a in actors.split() if "TestGhost" in a]
            print(f"[WARN] {name} not found by exact name. Matches: {matches}")

        agent_names.append(name)

    print(f"\n{'='*60}")
    print(f"ALL {len(agent_names)} AGENTS SPAWNED — TESTING GHOST COMMANDS")
    print(f"{'='*60}")
    print("Check UE viewport: you should see all agents VISIBLE right now.")
    time.sleep(3)

    # Now apply ghost mode to each agent, one command at a time
    for name in agent_names:
        print(f"\n--- Enabling ghost mode on {name} ---")
        ghost_cmds = [
            (f"vset /object/{name}/collision false", "disable collision"),
            (f"vset /object/{name}/hide", "hide actor"),
            (f"vset /object/{name}/collision_channel 8", "set channel to 8 (ghost)"),
            (f"vset /object/{name}/collision_response 8 ignore", "ignore other ghosts"),
            (f"vset /object/{name}/collision_response 2 ignore", "ignore pawns"),
            (f"vset /object/{name}/collision true", "re-enable collision"),
        ]
        for cmd, desc in ghost_cmds:
            try:
                resp = ucv.send(cmd)
                print(f"  [OK] {desc}: {cmd} -> {resp!r}")
            except Exception as e:
                print(f"  [FAIL] {desc}: {cmd} -> {e}")

        # Teleport to episode position
        x, y, z = positions[agent_names.index(name) % len(positions)]
        try:
            ucv.send(f"vset /object/{name}/collision false")
            ucv.send(f"vset /object/{name}/location {x} {y} {z}")
            ucv.send(f"vset /object/{name}/collision true")
            print(f"  [OK] teleported to ({x}, {y}, {z})")
        except Exception as e:
            print(f"  [FAIL] teleport: {e}")

    print(f"\n{'='*60}")
    print("GHOST MODE APPLIED — CHECK UE VIEWPORT")
    print("Agents should be INVISIBLE now.")
    print(f"{'='*60}")

    # Verify locations
    for name in agent_names:
        try:
            loc = ucv.send(f"vget /object/{name}/location")
            print(f"  {name} location: {loc}")
        except Exception as e:
            print(f"  {name} location query failed: {e}")

    print("\nWaiting 30s so you can inspect the viewport... (Ctrl+C to exit)")
    try:
        time.sleep(30)
    except KeyboardInterrupt:
        pass

    # Cleanup
    print("\nCleaning up...")
    for name in agent_names:
        try:
            ucv.send(f"vset /object/{name}/destroy")
            print(f"  destroyed {name}")
        except Exception:
            pass
    ucv.disconnect()
    print("Done.")


if __name__ == "__main__":
    main()
