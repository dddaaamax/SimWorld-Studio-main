"""Step 2: Generate PointNav tasks for each map via NavMesh sampling in UE.

Requires a running UE instance. For each of the 17 maps:
  1. Stop PIE
  2. Load the map via MCP execute_python
  3. Start PIE
  4. Build NavMesh, sample N episodes
  5. Save episodes to tasks/<map_label>.json

Usage:
    python -m experiments.env_diversity_ablation.generate_tasks [--map-index IDX]

Pass --map-index to generate for a single map (useful for retries).
Omit to generate for all 17 maps.
"""

from __future__ import annotations

import argparse
import json
import logging
import time
from pathlib import Path

from .config import (
    ALL_MAPS,
    TASKS_DIR,
    TASKS_PER_MAP,
    TEST_TASKS_PER_MAP,
    TEST_MAP_INDICES,
    MIN_GEODESIC_CM,
    MAX_GEODESIC_CM,
    TASK_SEED,
    MCP_HOST,
    MCP_PORT,
    UCV_HOST,
    UCV_PORT,
    ue_asset_path,
    map_label,
)

log = logging.getLogger(__name__)


def load_map_in_ue(mcp, asset_path: str, retries: int = 3, skip_load: bool = False) -> bool:
    """Stop PIE, load a new map, start PIE."""
    # Stop PIE first
    log.info("Stopping PIE...")
    mcp.stop_pie(wait_seconds=3.0)

    if skip_load:
        # Assume UE was launched with the target map already. Verify and skip.
        verify_script = (
            "import unreal\n"
            "w = unreal.EditorLevelLibrary.get_editor_world()\n"
            "print('CURRENT_MAP:' + (w.get_path_name() if w else 'NONE'))\n"
        )
        try:
            resp = mcp.execute_python(verify_script, timeout=30)
            logs = _extract_logs(resp)
            log.info("skip_load active; current map logs: %s", logs)
        except Exception as exc:
            log.warning("skip_load verify failed: %s", exc)
        time.sleep(3.0)
        _setup_navmesh_and_pie(mcp)
        return True

    # Load map
    script = (
        "import unreal\n"
        f"success = unreal.EditorLoadingAndSavingUtils.load_map('{asset_path}')\n"
        "if success:\n"
        "    print('MAP_LOADED_OK')\n"
        "else:\n"
        "    print('MAP_LOAD_FAILED')\n"
    )
    for attempt in range(retries):
        log.info("Loading map %s (attempt %d)...", asset_path, attempt + 1)
        try:
            resp = mcp.execute_python(script, timeout=180)
            logs = _extract_logs(resp)
            if any("MAP_LOADED_OK" in l for l in logs):
                log.info("Map loaded: %s", asset_path)
                break
            # MCP log capture is unreliable on secondary instances — python_logs
            # can come back empty even when the script ran successfully (UE's
            # SimWorld_2.log vs SimWorld.log resolution race). If the script
            # returned success and the log is empty, give UE time to settle and
            # trust the load; otherwise retry.
            result = resp.get("result") if isinstance(resp, dict) else None
            script_ok = bool(isinstance(result, dict) and result.get("success"))
            if script_ok and not logs:
                log.warning("Empty python_logs but success=true; trusting load of %s", asset_path)
                time.sleep(3.0)
                break
            log.warning("Map load response: %s", logs)
        except Exception as exc:
            log.warning("Map load attempt %d failed: %s", attempt + 1, exc)
        time.sleep(3.0)
    else:
        log.error("Failed to load map %s after %d attempts", asset_path, retries)
        return False

    # Wait for editor to settle after map load
    time.sleep(5.0)

    _setup_navmesh_and_pie(mcp)
    return True


def _setup_navmesh_and_pie(mcp):
    # Spawn NavMeshBoundsVolume + build navmesh in editor mode
    log.info("Spawning NavMeshBoundsVolume and building navmesh in editor...")
    nav_setup_script = """
import unreal

world = unreal.EditorLevelLibrary.get_editor_world()
if world is None:
    print('NAV_SETUP_ERROR: no editor world')
else:
    navvols = unreal.GameplayStatics.get_all_actors_of_class(world, unreal.NavMeshBoundsVolume)
    if len(navvols) > 0:
        navvols[0].set_actor_scale3d(unreal.Vector(200, 200, 40))
        navvols[0].set_actor_location(unreal.Vector(0, 0, 0), False, False)
        print('NAV_SETUP_OK: resized existing volume')
    else:
        eas = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)
        if eas:
            vol = eas.spawn_actor_from_class(unreal.NavMeshBoundsVolume, unreal.Vector(0, 0, 0))
            if vol:
                vol.set_actor_scale3d(unreal.Vector(200, 200, 40))
                print('NAV_SETUP_OK: spawned volume')
            else:
                print('NAV_SETUP_ERROR: spawn failed')
        else:
            print('NAV_SETUP_ERROR: no EditorActorSubsystem')

    unreal.SystemLibrary.execute_console_command(world, 'RebuildNavigation')
    print('NAV_BUILD_OK')
"""
    try:
        resp = mcp.execute_python(nav_setup_script, timeout=60)
        nav_logs = _extract_logs(resp)
        for l in nav_logs:
            log.info("  nav setup: %s", l)
        if not any("NAV_SETUP_OK" in l for l in nav_logs):
            log.warning("NavMeshBoundsVolume setup may have failed")
        if not any("NAV_BUILD_OK" in l for l in nav_logs):
            log.warning("Editor navmesh build may have failed")
    except Exception as exc:
        log.warning("Nav setup failed: %s", exc)

    time.sleep(3.0)

    # Start PIE
    log.info("Starting PIE...")
    mcp.start_pie(wait_seconds=12.0)


def generate_for_map(
    ucv, mcp, map_idx: int, n_tasks: int
) -> list:
    """Build navmesh and sample PointNav episodes for one map."""
    from nav_task.navmesh_interface import NavmeshNavigationInterface
    from gym_env.episode_builder import sample_pointnav_episode_navmesh

    nav = NavmeshNavigationInterface(ucv)

    # Build navmesh with retries
    for attempt in range(6):
        resp = nav.build_navmesh()
        log.info("navmesh build attempt %d: %s", attempt + 1, resp)
        if "error" not in str(resp).lower():
            break
        time.sleep(3.0)
    else:
        raise RuntimeError(f"navmesh build failed for map {map_idx}")

    # Sample episodes
    episodes = []
    failures = 0
    for i in range(n_tasks):
        seed = TASK_SEED + map_idx * 1000 + i
        try:
            result = sample_pointnav_episode_navmesh(
                ucv,
                seed=seed,
                idx=i,
                min_geodesic_cm=MIN_GEODESIC_CM,
                max_geodesic_cm=MAX_GEODESIC_CM,
                build_navmesh=False,
                nav_interface=nav,
                max_steps=60,
            )
            episodes.append({
                "episode": result["episode"].to_dict(),
                "difficulty": result["difficulty"],
                "gt_path_waypoints": result["gt_path_waypoints"],
            })
            log.info(
                "  [%d/%d] ep %s: geo=%.0f diff=%.2f",
                i + 1, n_tasks,
                result["episode"].episode_id,
                result["difficulty"]["distance_m"] * 100,
                result["difficulty"]["difficulty_score"],
            )
        except Exception as exc:
            log.warning("  [%d/%d] sampling failed: %s", i + 1, n_tasks, exc)
            failures += 1

    log.info("Map %d: sampled %d/%d episodes (%d failures)",
             map_idx, len(episodes), n_tasks, failures)
    return episodes


def save_tasks(map_idx: int, episodes: list):
    """Save sampled episodes to a JSON file."""
    TASKS_DIR.mkdir(parents=True, exist_ok=True)
    label = map_label(map_idx)
    out_path = TASKS_DIR / f"{label}.json"

    payload = {
        "map_index": map_idx,
        "map_label": label,
        "source": ALL_MAPS[map_idx][0],
        "n_objects": ALL_MAPS[map_idx][1],
        "n_episodes": len(episodes),
        "is_test_map": map_idx in TEST_MAP_INDICES,
        "episodes": [e["episode"] for e in episodes],
        "difficulties": [e["difficulty"] for e in episodes],
    }
    out_path.write_text(json.dumps(payload, indent=2))
    log.info("Saved %d episodes to %s", len(episodes), out_path)


def _extract_logs(resp) -> list:
    if not resp:
        return []
    result = resp.get("result")
    if isinstance(result, dict) and "python_logs" in result:
        return list(result["python_logs"])
    if isinstance(resp.get("python_logs"), list):
        return list(resp["python_logs"])
    return []


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--map-index", type=int, default=None,
                        help="Generate for a single map index (0-16)")
    parser.add_argument("--skip-load", action="store_true",
                        help="Skip load_map (assume UE launched with target map)")
    parser.add_argument("--ucv-host", default=UCV_HOST)
    parser.add_argument("--ucv-port", type=int, default=UCV_PORT)
    parser.add_argument("--mcp-host", default=MCP_HOST)
    parser.add_argument("--mcp-port", type=int, default=MCP_PORT)
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-5s %(name)s | %(message)s",
    )

    from gym_env.mcp_client import MCPClient
    from gym_env.ucv_client import UCVClient

    mcp = MCPClient(host=args.mcp_host, port=args.mcp_port)
    ucv = UCVClient(host=args.ucv_host, port=args.ucv_port)

    if args.map_index is not None:
        indices = [args.map_index]
    else:
        indices = list(range(len(ALL_MAPS)))

    for idx in indices:
        name, n_objs = ALL_MAPS[idx]
        is_test = idx in TEST_MAP_INDICES
        n_tasks = TEST_TASKS_PER_MAP if is_test else TASKS_PER_MAP
        role = "TEST" if is_test else "TRAIN"

        print(f"\n{'='*60}")
        print(f"[{role}] Map {idx:2d}: {name} ({n_objs} objs) — sampling {n_tasks} tasks")
        print(f"{'='*60}")

        out_path = TASKS_DIR / f"{map_label(idx)}.json"
        if out_path.exists():
            print(f"  SKIP: {out_path.name} already exists")
            continue

        asset = ue_asset_path(idx)
        if not load_map_in_ue(mcp, asset, skip_load=args.skip_load):
            print(f"  FAILED to load map {idx}, skipping")
            continue

        # Connect UCV (may need retries after PIE start)
        for attempt in range(10):
            try:
                ucv.connect()
                break
            except Exception:
                time.sleep(2)
        else:
            print(f"  FAILED to connect UCV for map {idx}, skipping")
            continue

        episodes = generate_for_map(ucv, mcp, idx, n_tasks)
        if episodes:
            save_tasks(idx, episodes)
        else:
            print(f"  WARNING: no episodes generated for map {idx}")

    print("\nDone! Tasks saved to", TASKS_DIR)


if __name__ == "__main__":
    main()
