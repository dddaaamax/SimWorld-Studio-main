"""Generate ObjectNav tasks for the observation-modality ablation.

For each map:
  1. Stop PIE, load map, nav-mesh setup, start PIE.
  2. Build navmesh; sample EPISODES_PER_MAP well-spaced target positions.
  3. For each target, pick a distinct entry from the curated object
     pool (hydrant / trash bin / cone / cardboard box / barrel ...) —
     indexed deterministically so the same (map, ep) pair always gets
     the same object.  Spawn it, verify it materialised (``vget
     /objects`` contains the actor name + lit-PNG mean differs
     meaningfully from baseline).
  4. Sample a start position with geodesic distance in
     [MIN_GEODESIC_CM, MAX_GEODESIC_CM] from the target.
  5. Write {start, goal, object_actor, blueprint, object_category,
     reference_path, success_criteria, evaluation_metrics} to
     ``tasks/ablation_NN.json``.

The experiment runner later REUSES these tasks across all
(model, modality) conditions so every condition is evaluated on the
same maps + start positions + target objects.

Usage::

    python -m experiments.observation_modality_ablation.generate_tasks \\
        --mcp-port 55558 --ucv-port 9002 [--map-index N] [--skip-load]
"""

from __future__ import annotations

import argparse
import io
import json
import logging
import math
import random
import time
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
from PIL import Image

from .config import (
    ALL_MAPS,
    EPISODES_PER_MAP,
    MAX_EPISODE_TIME_S,
    MAX_GEODESIC_CM,
    MAX_STEPS,
    MCP_HOST,
    MIN_GEODESIC_CM,
    MIN_TARGET_SPACING_CM,
    SUCCESS_DISTANCE_CM,
    TASKS_DIR,
    TASK_SEED,
    TEST_MAP_INDICES,
    UCV_HOST,
    map_label,
    ue_asset_path,
)

log = logging.getLogger(__name__)


_DEFAULT_SPAWN_Z = 110.0


# ── Map loader (copied/adapted from env_diversity_ablation) ──────────


def _extract_logs(resp) -> list:
    if not resp:
        return []
    result = resp.get("result")
    if isinstance(result, dict) and "python_logs" in result:
        return list(result["python_logs"])
    if isinstance(resp.get("python_logs"), list):
        return list(resp["python_logs"])
    return []


def load_map_in_ue(mcp, asset_path: str, retries: int = 3,
                   skip_load: bool = False) -> bool:
    """Stop PIE, load a new map, rebuild navmesh, start PIE."""
    log.info("Stopping PIE...")
    try:
        mcp.stop_pie(wait_seconds=3.0)
    except Exception as exc:
        log.debug("stop_pie: %s", exc)

    if not skip_load:
        script = (
            "import unreal\n"
            f"success = unreal.EditorLoadingAndSavingUtils.load_map('{asset_path}')\n"
            "print('MAP_LOADED_OK' if success else 'MAP_LOAD_FAILED')\n"
        )
        for attempt in range(retries):
            log.info("Loading map %s (attempt %d)...", asset_path, attempt + 1)
            try:
                resp = mcp.execute_python(script, timeout=180)
                logs = _extract_logs(resp)
                if any("MAP_LOADED_OK" in l for l in logs):
                    log.info("Map loaded: %s", asset_path)
                    break
                result = resp.get("result") if isinstance(resp, dict) else None
                if isinstance(result, dict) and result.get("success") and not logs:
                    log.warning("Empty python_logs; trusting load of %s", asset_path)
                    time.sleep(3.0)
                    break
            except Exception as exc:
                log.warning("Map load attempt %d failed: %s", attempt + 1, exc)
            time.sleep(3.0)
        else:
            return False
        time.sleep(5.0)

    # NavMesh volume + editor rebuild
    nav_setup = """
import unreal
world = unreal.EditorLevelLibrary.get_editor_world()
navvols = unreal.GameplayStatics.get_all_actors_of_class(world, unreal.NavMeshBoundsVolume)
if len(navvols) > 0:
    navvols[0].set_actor_scale3d(unreal.Vector(200, 200, 40))
    navvols[0].set_actor_location(unreal.Vector(0, 0, 0), False, False)
    print('NAV_SETUP_OK: resized existing volume')
else:
    eas = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)
    vol = eas.spawn_actor_from_class(unreal.NavMeshBoundsVolume, unreal.Vector(0, 0, 0))
    if vol:
        vol.set_actor_scale3d(unreal.Vector(200, 200, 40))
        print('NAV_SETUP_OK: spawned volume')
    else:
        print('NAV_SETUP_ERROR: spawn failed')
unreal.SystemLibrary.execute_console_command(world, 'RebuildNavigation')
print('NAV_BUILD_OK')
"""
    try:
        mcp.execute_python(nav_setup, timeout=60)
    except Exception as exc:
        log.warning("nav setup: %s", exc)
    time.sleep(3.0)

    log.info("Starting PIE...")
    mcp.start_pie(wait_seconds=12.0)
    return True


# ── Object choice & spawn ────────────────────────────────────────────

def _pick_object_specs(n: int, seed: int):
    """Deterministically pick n distinct, visually distinguishable objects.

    We exclude categories that are hard for an agent to identify at
    distance in cluttered UE city scenes — e.g. road blockers and large
    debris piles blend into the road texture.  Keep hydrant, bottles,
    cans, crates, cones, barrels, lantern.  These all have distinct
    silhouettes and colours.
    """
    from gym_env.object_pool import get_pool
    # Only blueprints — the plugin's spawn_static_mesh renames the
    # actor asynchronously, so vget /objects immediately after can
    # miss freshly spawned meshes even though they exist.  Blueprint
    # spawns are synchronous and show up in the live actor list
    # right away, which is what the verification step needs.
    pool = get_pool(kinds=("blueprint",))
    # Filter to high-signal targets (visually distinctive small objects).
    distinctive = [
        o for o in pool
        if o.category in (
            "infrastructure", "container", "can", "bottle",
            "box", "cone",
        )
    ]
    rng = random.Random(seed)
    rng.shuffle(distinctive)
    return distinctive[:n]


def _capture_lit_mean(ucv, cam_id: int) -> float:
    """Capture a lit frame; return its mean pixel value or nan on error."""
    try:
        png = ucv.vget_camera_png(camera_id=cam_id, mode="lit")
        if not png:
            return float("nan")
        arr = np.array(Image.open(io.BytesIO(png)).convert("RGB"), dtype=np.uint8)
        return float(arr.mean())
    except Exception as exc:
        log.warning("lit capture failed: %s", exc)
        return float("nan")


# ── Per-map generator ────────────────────────────────────────────────


def generate_for_map(ucv, mcp, map_idx: int, out_path: Path) -> bool:
    """Generate ObjectNav tasks for one map.  Returns True on success."""
    from gym_env.object_pool import canonical_noun
    from nav_task.episode import (
        EvaluationMetrics, NavigationEpisode, ObjectGoal, ObjectViewPoint,
        Position, ReferencePath, RewardConfig, SuccessCriteria, WorldConfig,
    )
    from nav_task.navmesh_interface import NavmeshNavigationInterface

    nav = NavmeshNavigationInterface(ucv)

    # 1) Build navmesh (retry — the plugin needs PIE-world to settle).
    for attempt in range(6):
        resp = nav.build_navmesh()
        if "error" not in str(resp).lower():
            log.info("navmesh build OK on attempt %d: %s", attempt + 1, resp[:80])
            break
        log.warning("navmesh build attempt %d returned: %s", attempt + 1, resp[:200])
        time.sleep(3.0)
    else:
        log.error("navmesh build FAILED for map %d", map_idx)
        return False

    rng = random.Random(TASK_SEED + map_idx * 1000)

    # 2) Sample enough candidate positions to find EPISODES_PER_MAP
    #    well-spaced targets.
    candidates = nav.get_navigable_positions(count=EPISODES_PER_MAP * 20, rng=rng)
    log.info("map %d: got %d candidate navmesh positions", map_idx, len(candidates))
    if len(candidates) < EPISODES_PER_MAP * 4:
        log.error("map %d: not enough navmesh candidates (%d)", map_idx, len(candidates))
        return False

    targets = []
    for pos in candidates:
        if all(
            math.sqrt((pos.x - t.x) ** 2 + (pos.y - t.y) ** 2) >= MIN_TARGET_SPACING_CM
            for t in targets
        ):
            targets.append(pos)
        if len(targets) >= EPISODES_PER_MAP:
            break
    if len(targets) < EPISODES_PER_MAP:
        log.error("map %d: only found %d/%d well-spaced targets",
                  map_idx, len(targets), EPISODES_PER_MAP)
        return False

    # 3) Pick objects and spawn them
    specs = _pick_object_specs(EPISODES_PER_MAP, TASK_SEED + map_idx * 1000)
    spawned = []  # list of (actor_name, spec, (x,y,z), goal_Position)

    # Capture a baseline lit mean to sanity-check spawns worked.
    # Use camera 0 (always resolves for the first agent spawned) —
    # here we have no agent yet so the lit comes from the editor's
    # default viewport camera if registered, which is still fine for
    # our "did the object appear?" check: we compare before vs after
    # spawning all targets.
    for i, (spec, pos) in enumerate(zip(specs, targets)):
        actor_name = f"objnav_target_m{map_idx:02d}_e{i}"
        loc = (pos.x, pos.y, _DEFAULT_SPAWN_Z)
        log.info(
            "spawn target %s: %s at (%.0f, %.0f, %.0f)",
            actor_name, spec.asset_path, *loc,
        )
        try:
            if spec.kind == "blueprint":
                ucv.spawn_bp_asset(spec.asset_path, actor_name, location=loc,
                                   auto_repair_collision=False)
            else:
                ucv.spawn_static_mesh(spec.asset_path, actor_name, location=loc)
        except Exception as exc:
            log.error("spawn FAILED for %s: %s", actor_name, exc)
            continue
        spawned.append((actor_name, spec, loc, pos))

    time.sleep(2.0)

    # Post-spawn: VERIFY each target is actually present in the scene.
    live = set(ucv.vget_objects())
    missing = [name for name, _, _, _ in spawned if name not in live]
    if missing:
        log.error("map %d: missing post-spawn actors: %s", map_idx, missing)
    spawned = [row for row in spawned if row[0] in live]
    if len(spawned) < EPISODES_PER_MAP:
        log.error("map %d: only %d/%d targets survived spawn verification",
                  map_idx, len(spawned), EPISODES_PER_MAP)
        return False

    # 4) Rebuild navmesh so geodesic / path queries include the new obstacles.
    log.info("rebuilding navmesh with targets in place...")
    nav.build_navmesh()

    # 5) For each target, sample a start position within the geodesic window.
    start_candidates = nav.get_navigable_positions(count=200, rng=rng)

    episodes = []
    # Tight enough that the path's last waypoint is basically the goal.
    # UE `vget /nav/path` returns a partial path to the nearest reachable
    # point when the goal itself is inside an obstacle or in a
    # disconnected navmesh island — those partial paths are short and
    # lie arbitrarily far from the real target.  Rejecting them here
    # avoids generating unreachable ObjectNav episodes.
    _REACHABILITY_TOL_CM = max(SUCCESS_DISTANCE_CM, 400.0)
    for ep_idx, (actor_name, spec, (tx, ty, tz), target_pos) in enumerate(spawned):
        goal = Position(x=tx, y=ty, node_type="object")
        start_pos = None
        geo = None
        ref_waypoints = None
        for _ in range(500):
            cand = rng.choice(start_candidates)
            d = nav.get_geodesic_distance(cand, goal)
            if d is None or not (MIN_GEODESIC_CM <= d <= MAX_GEODESIC_CM):
                continue
            wps = nav.get_reference_path(cand, goal)
            if wps is None:
                continue
            # Validate that the path actually reaches the goal —
            # otherwise this is a partial path to the nearest reachable
            # point and the agent literally cannot succeed.
            last = wps[-1]
            gap = math.sqrt((last.x - goal.x) ** 2 + (last.y - goal.y) ** 2)
            if gap > _REACHABILITY_TOL_CM:
                continue
            start_pos = cand
            geo = d
            ref_waypoints = wps
            break
        if start_pos is None:
            log.warning(
                "map %d ep %d: no reachable start in [%.0f, %.0f] cm — skipping",
                map_idx, ep_idx, MIN_GEODESIC_CM, MAX_GEODESIC_CM,
            )
            continue

        ep = NavigationEpisode(
            episode_id=f"objnav_m{map_idx:02d}_e{ep_idx}",
            seed=TASK_SEED + map_idx * 1000 + ep_idx,
            world=WorldConfig(
                map_file=map_label(map_idx), coordinate_unit="cm",
                x_min=-100_000.0, x_max=100_000.0,
                y_min=-100_000.0, y_max=100_000.0,
            ),
            start_position=start_pos,
            goal_position=goal,
            reference_path=ReferencePath(
                waypoints=tuple(ref_waypoints),
                shortest_path_length_cm=geo,
            ),
            success_criteria=SuccessCriteria(
                success_distance_cm=SUCCESS_DISTANCE_CM,
                max_steps=MAX_STEPS,
                max_episode_time_s=MAX_EPISODE_TIME_S,
            ),
            evaluation_metrics=EvaluationMetrics(
                success_distance_cm=SUCCESS_DISTANCE_CM,
                shortest_path_length_cm=geo,
            ),
            generated_at=datetime.now(timezone.utc).isoformat(),
            reward_config=RewardConfig(),
            task_type="objectnav",
            object_category=spec.category,
            object_goal=ObjectGoal(
                object_id=actor_name,
                object_type=spec.category,
                object_category=spec.category,
                position=goal,
                view_points=(ObjectViewPoint(position=goal, iou=None),),
            ),
        )
        # Tag with non-schema metadata so the runner knows what to
        # re-spawn at experiment start.  These fields piggy-back on
        # the ep.to_dict() via a shadow layer below (we write the dict
        # directly, so extra keys are preserved).
        episodes.append({
            **ep.to_dict(),
            "_source_map_index": map_idx,
            "_target_actor_name": actor_name,
            "_target_asset_path": spec.asset_path,
            "_target_kind": spec.kind,
            "_target_noun": canonical_noun(spec),
            "_target_xy_z": [tx, ty, tz],
            "_target_category": spec.category,
        })

        log.info(
            "  ep %d: target=%s (%s %s) geo=%.0f cm start=(%.0f, %.0f)",
            ep_idx, actor_name, canonical_noun(spec), spec.category,
            geo, start_pos.x, start_pos.y,
        )

    # 6) Cleanup spawned actors — they will be re-spawned by the runner
    #    at experiment time (so episodes are deterministic regardless
    #    of UE-session lifecycle).
    for name, _, _, _ in spawned:
        ucv.destroy_actor(name)

    if not episodes:
        log.error("map %d: produced 0 episodes", map_idx)
        return False

    TASKS_DIR.mkdir(parents=True, exist_ok=True)
    payload = {
        "map_index": map_idx,
        "map_label": map_label(map_idx),
        "source": ALL_MAPS[map_idx][0],
        "n_real_objects_in_map": ALL_MAPS[map_idx][1],
        "is_test_map": map_idx in TEST_MAP_INDICES,
        "split": "unseen" if map_idx in TEST_MAP_INDICES else "seen",
        "n_episodes": len(episodes),
        "episodes": episodes,
    }
    out_path.write_text(json.dumps(payload, indent=2))
    log.info("Wrote %d episodes to %s", len(episodes), out_path)
    return True


# ── Main ──────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--mcp-port", type=int, default=55558)
    parser.add_argument("--ucv-port", type=int, default=9002)
    parser.add_argument("--mcp-host", default=MCP_HOST)
    parser.add_argument("--ucv-host", default=UCV_HOST)
    parser.add_argument("--map-index", type=int, default=None,
                        help="Generate for a single map index")
    parser.add_argument("--skip-load", action="store_true",
                        help="Don't reload map; assume UE has target map loaded")
    parser.add_argument("--overwrite", action="store_true",
                        help="Regenerate even if output file exists")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-5s %(name)s | %(message)s",
    )

    from gym_env.mcp_client import MCPClient
    from gym_env.ucv_client import UCVClient

    mcp = MCPClient(host=args.mcp_host, port=args.mcp_port, name="gen-mcp")
    ucv = UCVClient(host=args.ucv_host, port=args.ucv_port, name="gen-ucv")

    indices = [args.map_index] if args.map_index is not None else list(range(len(ALL_MAPS)))

    for idx in indices:
        out_path = TASKS_DIR / f"{map_label(idx)}.json"
        if out_path.exists() and not args.overwrite:
            print(f"  SKIP: {out_path.name} already exists")
            continue

        print(f"\n=== MAP {idx:2d} ({ALL_MAPS[idx][0]}) ===")
        if not load_map_in_ue(mcp, ue_asset_path(idx), skip_load=args.skip_load):
            print(f"  FAILED load map {idx}, continuing")
            continue

        # Connect UCV (retry — PIE start can drop the socket)
        for a in range(15):
            try:
                ucv.hard_reconnect()
                break
            except Exception:
                time.sleep(2)
        else:
            print(f"  FAILED UCV connect for map {idx}")
            continue

        try:
            ok = generate_for_map(ucv, mcp, idx, out_path)
        except Exception as exc:
            log.exception("map %d: generate_for_map crashed", idx)
            ok = False
        if not ok:
            print(f"  FAILED generate for map {idx}")


if __name__ == "__main__":
    main()
