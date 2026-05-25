"""Builds NavigationEpisode objects from a live UE scene.

Two pipelines:

  * **Legacy** (no navmesh): ``sample_pointnav_episode`` / ``sample_objectnav_episode``
    Random goal placement, no reachability check.

  * **NavMesh-validated**: ``sample_pointnav_episode_navmesh`` / ``sample_objectnav_episode_navmesh``
    Grid-based position sampling + UE navmesh geodesic validation.
    Produces GT reference paths that avoid obstacles.

Both produce :class:`NavigationEpisode` JSON-ready for ``env.reset(episode)``.
"""

from __future__ import annotations

import logging
import math
import random
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from nav_task.episode import (
    EvaluationMetrics,
    NavigationEpisode,
    ObjectGoal,
    ObjectViewPoint,
    Position,
    ReferencePath,
    RewardConfig,
    SuccessCriteria,
    WorldConfig,
)

from .ucv_client import UCVClient

log = logging.getLogger(__name__)

_DEFAULT_SPAWN_Z = 110.0


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _new_episode_id(seed: int, idx: int = 0) -> str:
    return f"nav_ep_{seed}_{idx:03d}"


def _make_world(map_label: str = "live_ue_scene") -> WorldConfig:
    return WorldConfig(
        map_file=map_label,
        coordinate_unit="cm",
        x_min=-100_000.0, x_max=100_000.0,
        y_min=-100_000.0, y_max=100_000.0,
    )


def _euclidean(x1, y1, x2, y2) -> float:
    return math.sqrt((x2 - x1) ** 2 + (y2 - y1) ** 2)


def _angle_to_target(sx, sy, gx, gy) -> float:
    """Return angle (degrees) from start to goal in UE coordinate system."""
    return math.degrees(math.atan2(gy - sy, gx - sx))


def _angle_diff(a, b) -> float:
    """Smallest signed angle difference in degrees."""
    d = (a - b + 180) % 360 - 180
    return abs(d)


def compute_difficulty(
    euclidean_cm: float,
    geodesic_cm: float,
    start_heading_deg: float,
    target_angle_deg: float,
) -> Dict[str, float]:
    """Compute task difficulty metrics.

    Returns dict with:
        distance_m: geodesic distance in metres
        detour_ratio: geodesic / euclidean (1.0 = straight, >1 = detour)
        heading_offset_deg: angle between start heading and target direction
        difficulty_score: composite 0-1 score (higher = harder)
    """
    detour_ratio = geodesic_cm / euclidean_cm if euclidean_cm > 0 else 1.0
    heading_offset = _angle_diff(start_heading_deg, target_angle_deg)

    # Composite: weighted sum of normalised factors
    # distance: 0-10000cm → 0-1
    d_norm = min(geodesic_cm / 10000.0, 1.0)
    # detour: 1.0-2.0 → 0-1
    det_norm = min((detour_ratio - 1.0) / 1.0, 1.0)
    # heading: 0-180 → 0-1
    h_norm = heading_offset / 180.0

    score = 0.4 * d_norm + 0.35 * det_norm + 0.25 * h_norm

    return {
        "distance_m": geodesic_cm / 100.0,
        "detour_ratio": round(detour_ratio, 3),
        "heading_offset_deg": round(heading_offset, 1),
        "difficulty_score": round(score, 3),
    }


# ---------------------------------------------------------------------------
# Scene snapshot
# ---------------------------------------------------------------------------

def snapshot_scene(ucv: UCVClient) -> List[Tuple[str, Tuple[float, float, float]]]:
    """List all UE actors and their locations."""
    names = ucv.vget_objects()
    out: List[Tuple[str, Tuple[float, float, float]]] = []
    for name in names:
        try:
            loc = ucv.vget_location(name)
            out.append((name, loc))
        except Exception as exc:
            log.debug("scene snapshot: skipped %s (%s)", name, exc)
    log.info("snapshot_scene: %d actors", len(out))
    return out


def filter_actors(
    actors: Sequence[Tuple[str, Tuple[float, float, float]]],
    name_filter: Optional[Callable[[str], bool]],
) -> List[Tuple[str, Tuple[float, float, float]]]:
    if name_filter is None:
        return list(actors)
    return [a for a in actors if name_filter(a[0])]


# ---------------------------------------------------------------------------
# NavMesh-validated PointNav
# ---------------------------------------------------------------------------

def sample_pointnav_episode_navmesh(
    ucv: UCVClient,
    *,
    seed: int = 42,
    idx: int = 0,
    min_geodesic_cm: float = 1000.0,
    max_geodesic_cm: float = 5000.0,
    success_distance_cm: float = 200.0,
    max_steps: int = 60,
    max_episode_time_s: float = 300.0,
    max_sampling_attempts: int = 200,
    reward_config: Optional[RewardConfig] = None,
    build_navmesh: bool = True,
    nav_interface=None,
    sample_count: int = 500,
    bounds: Optional[Tuple[float, float, float, float]] = None,
) -> dict:
    """Sample a PointNav episode — all sampling from UE navmesh, no scene graph.

    Returns a dict containing:
        episode: NavigationEpisode
        start_heading_deg: random initial heading (0-360)
        difficulty: dict with distance_m, detour_ratio, heading_offset_deg, score
        gt_path_waypoints: list of (x, y) tuples from navmesh
    """
    from nav_task.navmesh_interface import NavmeshNavigationInterface

    if nav_interface is None:
        nav = NavmeshNavigationInterface(ucv)
        if build_navmesh:
            resp = nav.build_navmesh()
            log.info("navmesh build: %s", resp)
    else:
        nav = nav_interface

    rng = random.Random(seed + idx)
    positions = nav.get_navigable_positions(count=sample_count, rng=rng)
    rc = reward_config or RewardConfig()

    if len(positions) < 2:
        raise RuntimeError(
            f"NavMesh returned only {len(positions)} navigable points. "
            "Is navmesh built? Check vget /nav/status."
        )

    # If bounds are specified, filter positions to that area first.
    # bounds = (x_min, y_min, x_max, y_max)
    pre_filter_count = len(positions)
    if bounds is not None:
        bx_min, by_min, bx_max, by_max = bounds
        positions = [
            p for p in positions
            if bx_min <= p.x <= bx_max and by_min <= p.y <= by_max
        ]
        log.info(
            "bounds filter: %d -> %d positions (bounds [%.0f,%.0f]-[%.0f,%.0f])",
            pre_filter_count, len(positions), bx_min, by_min, bx_max, by_max,
        )
        if len(positions) < 2:
            raise RuntimeError(
                f"After bounds filtering, only {len(positions)} positions remain. "
                f"Bounds [{bx_min},{by_min}]-[{bx_max},{by_max}] may be too tight."
            )

    # Filter out positions in the outer 10% of the (remaining) bounding box.
    xs = [p.x for p in positions]
    ys = [p.y for p in positions]
    x_min, x_max = min(xs), max(xs)
    y_min, y_max = min(ys), max(ys)
    margin_x = (x_max - x_min) * 0.10
    margin_y = (y_max - y_min) * 0.10
    positions = [
        p for p in positions
        if (x_min + margin_x <= p.x <= x_max - margin_x
            and y_min + margin_y <= p.y <= y_max - margin_y)
    ]
    log.info(
        "navmesh edge filter: %d -> %d positions (margin %.0fx%.0f cm)",
        len(xs), len(positions), margin_x, margin_y,
    )
    if len(positions) < 2:
        raise RuntimeError(
            "After edge filtering, fewer than 2 positions remain. "
            "Navmesh may be too small or fragmented."
        )

    for attempt in range(max_sampling_attempts):
        start_pos = rng.choice(positions)
        goal_pos = rng.choice(positions)
        if start_pos == goal_pos:
            continue

        eucl = _euclidean(start_pos.x, start_pos.y, goal_pos.x, goal_pos.y)
        if eucl < min_geodesic_cm * 0.5 or eucl > max_geodesic_cm * 2.0:
            continue

        geo = nav.get_geodesic_distance(start_pos, goal_pos)
        if geo is None:
            continue
        if not (min_geodesic_cm <= geo <= max_geodesic_cm):
            continue

        ref_waypoints = nav.get_reference_path(start_pos, goal_pos)
        if ref_waypoints is None:
            continue

        # Random start heading
        start_heading_deg = rng.uniform(0, 360)
        target_angle_deg = _angle_to_target(
            start_pos.x, start_pos.y, goal_pos.x, goal_pos.y)

        difficulty = compute_difficulty(eucl, geo, start_heading_deg, target_angle_deg)

        ref_path = ReferencePath(
            waypoints=tuple(ref_waypoints),
            shortest_path_length_cm=geo,
        )
        ep = NavigationEpisode(
            episode_id=_new_episode_id(seed, idx),
            seed=seed,
            world=_make_world("live_ue_scene"),
            start_position=start_pos,
            goal_position=goal_pos,
            reference_path=ref_path,
            success_criteria=SuccessCriteria(
                success_distance_cm=success_distance_cm,
                max_steps=max_steps,
                max_episode_time_s=max_episode_time_s,
            ),
            evaluation_metrics=EvaluationMetrics(
                success_distance_cm=success_distance_cm,
                shortest_path_length_cm=geo,
            ),
            generated_at=_now_iso(),
            reward_config=rc,
            task_type="pointnav",
        )

        gt_wps = [(p.x, p.y) for p in ref_waypoints]

        log.info(
            "pointnav_navmesh ep %s: start=(%.0f,%.0f) goal=(%.0f,%.0f) "
            "geo=%.0f eucl=%.0f ratio=%.2f heading=%.0f° difficulty=%.2f (attempt %d)",
            ep.episode_id, start_pos.x, start_pos.y,
            goal_pos.x, goal_pos.y, geo, eucl,
            difficulty["detour_ratio"], start_heading_deg,
            difficulty["difficulty_score"], attempt + 1,
        )

        return {
            "episode": ep,
            "start_heading_deg": round(start_heading_deg, 1),
            "difficulty": difficulty,
            "gt_path_waypoints": gt_wps,
        }

    raise RuntimeError(
        f"Failed to sample a valid navmesh pointnav episode after "
        f"{max_sampling_attempts} attempts"
    )


# ---------------------------------------------------------------------------
# NavMesh-validated ObjectNav
# ---------------------------------------------------------------------------

def sample_objectnav_episode_navmesh(
    ucv: UCVClient,
    *,
    seed: int = 42,
    idx: int = 0,
    target_filter: Callable[[str], bool],
    object_category: str,
    object_description: str = "",
    success_distance_cm: float = 300.0,
    max_steps: int = 60,
    max_episode_time_s: float = 300.0,
    min_geodesic_cm: float = 800.0,
    max_geodesic_cm: float = 8000.0,
    max_sampling_attempts: int = 100,
    reward_config: Optional[RewardConfig] = None,
    build_navmesh: bool = True,
    nav_interface=None,
    sample_count: int = 500,
) -> dict:
    """Sample an ObjectNav episode — all sampling from UE navmesh, no scene graph.

    The agent must navigate to a specific object. Prompt includes rough
    direction + detailed object description. Success requires reaching
    within success_distance_cm of the object.

    Returns dict with: episode, start_heading_deg, difficulty, gt_path_waypoints,
        target_actor_name, object_description, prompt
    """
    from nav_task.navmesh_interface import NavmeshNavigationInterface

    if nav_interface is None:
        nav = NavmeshNavigationInterface(ucv)
        if build_navmesh:
            nav.build_navmesh()
    else:
        nav = nav_interface

    rng = random.Random(seed + idx)
    positions = nav.get_navigable_positions(count=sample_count, rng=rng)

    if len(positions) < 2:
        raise RuntimeError(
            f"NavMesh returned only {len(positions)} navigable points."
        )

    # Find target objects in the scene
    actors = snapshot_scene(ucv)
    candidates = filter_actors(actors, target_filter)
    if not candidates:
        raise RuntimeError(
            f"objectnav: no actors matched filter; scene has {len(actors)} actors"
        )

    rc = reward_config or RewardConfig()

    for attempt in range(max_sampling_attempts):
        # Pick a random target object
        target_name, target_loc = rng.choice(candidates)
        gx, gy, _gz = target_loc
        goal_pos = Position(x=gx, y=gy, node_type="object")

        # Pick a random start from navigable positions
        start_pos = rng.choice(positions)

        eucl = _euclidean(start_pos.x, start_pos.y, gx, gy)
        if eucl < min_geodesic_cm * 0.5 or eucl > max_geodesic_cm * 2.0:
            continue

        # Navmesh validation
        geo = nav.get_geodesic_distance(start_pos, goal_pos)
        if geo is None:
            continue
        if not (min_geodesic_cm <= geo <= max_geodesic_cm):
            continue

        ref_waypoints = nav.get_reference_path(start_pos, goal_pos)
        if ref_waypoints is None:
            continue

        # Random start heading
        start_heading_deg = rng.uniform(0, 360)
        target_angle_deg = _angle_to_target(
            start_pos.x, start_pos.y, gx, gy)
        difficulty = compute_difficulty(eucl, geo, start_heading_deg, target_angle_deg)

        # Build directional hint
        angle = target_angle_deg % 360
        if angle < 0:
            angle += 360
        directions = ["east", "northeast", "north", "northwest",
                      "west", "southwest", "south", "southeast"]
        dir_idx = int((angle + 22.5) / 45) % 8
        rough_dir = directions[dir_idx]

        desc = object_description or f"a {object_category}"
        prompt = (
            f"Find and navigate to {desc}. "
            f"It is roughly to the {rough_dir} of your starting position, "
            f"approximately {int(eucl / 100)}m away. "
            f"You must get within {success_distance_cm / 100:.1f}m of the object."
        )

        obj_goal = ObjectGoal(
            object_id=target_name,
            object_type=target_name,
            object_category=object_category,
            position=goal_pos,
            view_points=(ObjectViewPoint(position=goal_pos, iou=None),),
        )

        ref_path = ReferencePath(
            waypoints=tuple(ref_waypoints),
            shortest_path_length_cm=geo,
        )
        ep = NavigationEpisode(
            episode_id=_new_episode_id(seed, idx),
            seed=seed,
            world=_make_world("live_ue_scene"),
            start_position=start_pos,
            goal_position=goal_pos,
            reference_path=ref_path,
            success_criteria=SuccessCriteria(
                success_distance_cm=success_distance_cm,
                max_steps=max_steps,
                max_episode_time_s=max_episode_time_s,
            ),
            evaluation_metrics=EvaluationMetrics(
                success_distance_cm=success_distance_cm,
                shortest_path_length_cm=geo,
            ),
            generated_at=_now_iso(),
            reward_config=rc,
            task_type="objectnav",
            object_category=object_category,
            object_goal=obj_goal,
        )

        gt_wps = [(p.x, p.y) for p in ref_waypoints]

        log.info(
            "objectnav_navmesh ep %s: target=%s start=(%.0f,%.0f) goal=(%.0f,%.0f) "
            "geo=%.0f difficulty=%.2f (attempt %d)",
            ep.episode_id, target_name, start_pos.x, start_pos.y,
            gx, gy, geo, difficulty["difficulty_score"], attempt + 1,
        )

        return {
            "episode": ep,
            "start_heading_deg": round(start_heading_deg, 1),
            "difficulty": difficulty,
            "gt_path_waypoints": gt_wps,
            "target_actor_name": target_name,
            "object_description": desc,
            "prompt": prompt,
        }

    raise RuntimeError(
        f"Failed to sample objectnav episode after {max_sampling_attempts} attempts"
    )


# ---------------------------------------------------------------------------
# ObjectNav search — spawn a batch of small objects, one episode per target
# ---------------------------------------------------------------------------

def sample_objectnav_search_batch(
    ucv,
    *,
    base_seed: int,
    n_targets: int,
    describer,                      # TargetDescriber instance
    nav_interface=None,
    min_target_spacing_cm: float = 500.0,
    min_geodesic_cm: float = 1500.0,
    max_geodesic_cm: float = 6000.0,
    success_distance_cm: float = 200.0,
    max_steps: int = 40,
    max_episode_time_s: float = 300.0,
    max_sampling_attempts: int = 500,
    reward_config=None,
    build_navmesh: bool = True,
    navmesh_padding_cm: float = 500.0,
) -> tuple:
    """Spawn ``n_targets`` small objects and build one episode per target.

    The returned tuple is ``(episodes, spawned_actors, descriptions)``:
        * ``episodes`` — list of NavigationEpisode, one per target.
        * ``spawned_actors`` — list of (actor_name, (x, y, z)) tuples
          for cleanup.
        * ``descriptions`` — list of TargetDescription objects, matched
          by index to ``episodes``.

    The caller is expected to pass the episodes to the runner one at a
    time (each constitutes a full navigation episode).  The small
    objects remain in the scene across episodes (single spawn + single
    navmesh rebuild) — between episodes the agent simply teleports to
    a fresh start point and searches for the next target.
    """
    from nav_task.navmesh_interface import NavmeshNavigationInterface
    from .object_pool import get_pool, canonical_noun

    if nav_interface is None:
        nav_interface = NavmeshNavigationInterface(ucv)
        if build_navmesh:
            nav_interface.build_navmesh(padding_cm=navmesh_padding_cm)

    rng = random.Random(base_seed)
    pool = get_pool()
    if len(pool) < n_targets:
        raise ValueError(
            f"object pool has {len(pool)} specs but {n_targets} targets requested"
        )
    chosen_specs = rng.sample(pool, n_targets)

    # Sample candidate positions on the navmesh and pick N that are
    # pairwise at least ``min_target_spacing_cm`` apart.
    cand_positions = nav_interface.get_navigable_positions(
        count=n_targets * 8, rng=rng,
    )
    selected: List[Position] = []
    for pos in cand_positions:
        if all(
            math.sqrt((pos.x - s.x) ** 2 + (pos.y - s.y) ** 2)
            >= min_target_spacing_cm
            for s in selected
        ):
            selected.append(pos)
        if len(selected) >= n_targets:
            break
    if len(selected) < n_targets:
        raise RuntimeError(
            f"Could not find {n_targets} well-spaced spawn points "
            f"(got {len(selected)})"
        )

    # Spawn each target.
    spawned: List[Tuple[str, Tuple[float, float, float]]] = []
    for idx, (spec, pos) in enumerate(zip(chosen_specs, selected)):
        actor_name = f"task_target_{base_seed}_{idx}"
        location = (pos.x, pos.y, _DEFAULT_SPAWN_Z)
        try:
            if spec.kind == "blueprint":
                ucv.spawn_bp_asset(spec.asset_path, actor_name, location=location)
            else:
                ucv.spawn_static_mesh(spec.asset_path, actor_name, location=location)
        except Exception as exc:
            log.warning("spawn failed for %s (%s): %s", actor_name, spec.asset_path, exc)
            continue
        spawned.append((actor_name, location))

    log.info("spawned %d / %d target objects", len(spawned), n_targets)

    # Rebuild navmesh AFTER spawn so geodesic / path queries see the
    # new obstacles.  This is unconditional — the ``build_navmesh``
    # flag only controls the *initial* build at the start of the
    # function; the post-spawn rebuild must always happen when we
    # have a nav interface, otherwise the path queries below would
    # use a stale navmesh that doesn't know about the targets.
    log.info("rebuilding navmesh after target spawn...")
    nav_interface.build_navmesh(padding_cm=navmesh_padding_cm)

    rc = reward_config or RewardConfig()
    episodes: List[NavigationEpisode] = []
    descriptions = []
    positions_for_start = nav_interface.get_navigable_positions(
        count=len(spawned) * 10, rng=rng,
    )

    for ep_idx, ((actor_name, (tx, ty, tz)), spec) in enumerate(
        zip(spawned, chosen_specs)
    ):
        goal_pos = Position(x=tx, y=ty, node_type="object")

        # Sample a start position within the geodesic window.
        start_pos: Optional[Position] = None
        geo: Optional[float] = None
        ref_waypoints = None
        for attempt in range(max_sampling_attempts):
            candidate = rng.choice(positions_for_start)
            geo = nav_interface.get_geodesic_distance(candidate, goal_pos)
            if geo is None:
                continue
            if not (min_geodesic_cm <= geo <= max_geodesic_cm):
                continue
            ref_waypoints = nav_interface.get_reference_path(candidate, goal_pos)
            if ref_waypoints is None:
                continue
            start_pos = candidate
            break
        if start_pos is None:
            log.warning(
                "skip target %s: no reachable start in [%d, %d]cm",
                actor_name, min_geodesic_cm, max_geodesic_cm,
            )
            continue

        ref_path = ReferencePath(
            waypoints=tuple(ref_waypoints),
            shortest_path_length_cm=geo,
        )
        obj_goal = ObjectGoal(
            object_id=actor_name,
            object_type=spec.category,
            object_category=spec.category,
            position=goal_pos,
            view_points=(ObjectViewPoint(position=goal_pos, iou=None),),
        )
        description = describer.describe(
            target_spec=spec,
            target_name=actor_name,
            target_xy=(tx, ty),
            start_xy=(start_pos.x, start_pos.y),
        )
        ep = NavigationEpisode(
            episode_id=_new_episode_id(base_seed, ep_idx),
            seed=base_seed,
            world=_make_world("live_ue_scene"),
            start_position=start_pos,
            goal_position=goal_pos,
            reference_path=ref_path,
            success_criteria=SuccessCriteria(
                success_distance_cm=success_distance_cm,
                max_steps=max_steps,
                max_episode_time_s=max_episode_time_s,
            ),
            evaluation_metrics=EvaluationMetrics(
                success_distance_cm=success_distance_cm,
                shortest_path_length_cm=geo,
            ),
            generated_at=_now_iso(),
            reward_config=rc,
            task_type="objectnav",
            object_category=spec.category,
            object_goal=obj_goal,
        )
        episodes.append(ep)
        descriptions.append(description)
        log.info(
            "objectnav_search ep %s: target=%s (%s) geo=%.0fcm hint=%r",
            ep.episode_id,
            canonical_noun(spec),
            description.generator,
            geo,
            description.prompt[:80],
        )

    return episodes, spawned, descriptions


def cleanup_spawned_actors(ucv, spawned_actors) -> None:
    """Destroy a list of spawned actors; safe to call on already-gone names."""
    for name, _ in spawned_actors:
        try:
            ucv.destroy_actor(name)
        except Exception as exc:
            log.debug("destroy %s failed: %s", name, exc)


# ── Per-call interface that mirrors sample_pointnav_episode_navmesh ──
#
# Runners already call sample_pointnav_episode_navmesh once per
# episode (see ``gym_env/runner.py``).  The ObjectNav search variant
# keeps the same signature — ``seed + idx`` identifies which target
# out of a pre-spawned batch to return.  The first call with a given
# ``seed`` does the spawn; subsequent calls reuse the cached batch.

_OBJECTNAV_SEARCH_CACHE: Dict[Any, Any] = {}


def sample_objectnav_search_episode(
    ucv,
    *,
    seed: int = 42,
    idx: int = 0,
    describer=None,
    n_targets: int = 5,
    nav_interface=None,
    min_target_spacing_cm: float = 500.0,
    min_geodesic_cm: float = 1500.0,
    max_geodesic_cm: float = 6000.0,
    success_distance_cm: float = 200.0,
    max_steps: int = 40,
    max_episode_time_s: float = 300.0,
    reward_config=None,
    build_navmesh: bool = True,
    navmesh_padding_cm: float = 500.0,
) -> dict:
    """Sample one ObjectNav search episode from a shared batch of targets.

    Same return shape as :func:`sample_pointnav_episode_navmesh`:

        {
            "episode": NavigationEpisode,
            "start_heading_deg": float,
            "difficulty": dict,
            "gt_path_waypoints": list[(x, y)],
            "description": TargetDescription,
            "target_actor_name": str,
            "spawned_actors": list[(name, (x,y,z))],  # pool reference, shared
        }

    The spawn happens on the first call with a given ``seed`` (so
    ``idx=0``).  Subsequent calls with the same seed return the next
    pre-spawned target from the cached batch.
    """
    if describer is None:
        raise ValueError("sample_objectnav_search_episode requires a describer")

    cache_key = (seed, n_targets, min_target_spacing_cm)
    cache = _OBJECTNAV_SEARCH_CACHE.get(cache_key)

    if cache is None:
        episodes, spawned, descriptions = sample_objectnav_search_batch(
            ucv,
            base_seed=seed,
            n_targets=n_targets,
            describer=describer,
            nav_interface=nav_interface,
            min_target_spacing_cm=min_target_spacing_cm,
            min_geodesic_cm=min_geodesic_cm,
            max_geodesic_cm=max_geodesic_cm,
            success_distance_cm=success_distance_cm,
            max_steps=max_steps,
            max_episode_time_s=max_episode_time_s,
            reward_config=reward_config,
            build_navmesh=build_navmesh,
            navmesh_padding_cm=navmesh_padding_cm,
        )
        cache = {
            "episodes": episodes,
            "spawned": spawned,
            "descriptions": descriptions,
        }
        _OBJECTNAV_SEARCH_CACHE[cache_key] = cache

    episodes = cache["episodes"]
    descriptions = cache["descriptions"]
    spawned = cache["spawned"]
    if not episodes:
        raise RuntimeError("ObjectNav search batch produced zero episodes")

    # idx is circular so callers can request arbitrary ep_idx values.
    pick = idx % len(episodes)
    ep = episodes[pick]
    desc = descriptions[pick]

    # Build difficulty & gt_path_waypoints for parity with PointNav.
    eucl = _euclidean(
        ep.start_position.x, ep.start_position.y,
        ep.goal_position.x, ep.goal_position.y,
    )
    geo = ep.reference_path.shortest_path_length_cm
    start_heading_deg = random.Random(seed + idx).uniform(0, 360)
    target_angle_deg = _angle_to_target(
        ep.start_position.x, ep.start_position.y,
        ep.goal_position.x, ep.goal_position.y,
    )
    difficulty = compute_difficulty(eucl, geo, start_heading_deg, target_angle_deg)
    gt_wps = [(p.x, p.y) for p in ep.reference_path.waypoints]

    return {
        "episode": ep,
        "start_heading_deg": round(start_heading_deg, 1),
        "difficulty": difficulty,
        "gt_path_waypoints": gt_wps,
        "description": desc,
        "target_actor_name": desc.target_name,
        "spawned_actors": spawned,
        "prompt": desc.prompt,
    }


def clear_objectnav_search_cache() -> None:
    """Clear the cached spawned-object batches.

    Call at the start of a new run if you want a fresh spawn instead
    of reusing the previous run's pool.
    """
    _OBJECTNAV_SEARCH_CACHE.clear()


# ---------------------------------------------------------------------------
# Legacy (no navmesh) — kept for backwards compat
# ---------------------------------------------------------------------------

def _build_episode(
    *,
    seed: int,
    idx: int,
    start: Position,
    goal: Position,
    success_distance_cm: float,
    max_steps: int,
    max_episode_time_s: float,
    reward_config: RewardConfig,
    object_goal: Optional[ObjectGoal] = None,
    task_type: str = "pointnav",
    object_category: Optional[str] = None,
    map_label: str = "live_ue_scene",
) -> NavigationEpisode:
    straight = math.sqrt((start.x - goal.x) ** 2 + (start.y - goal.y) ** 2)
    ref_path = ReferencePath(
        waypoints=(start, goal),
        shortest_path_length_cm=straight,
    )
    return NavigationEpisode(
        episode_id=_new_episode_id(seed, idx),
        seed=seed,
        world=_make_world(map_label),
        start_position=start,
        goal_position=goal,
        reference_path=ref_path,
        success_criteria=SuccessCriteria(
            success_distance_cm=success_distance_cm,
            max_steps=max_steps,
            max_episode_time_s=max_episode_time_s,
        ),
        evaluation_metrics=EvaluationMetrics(
            success_distance_cm=success_distance_cm,
            shortest_path_length_cm=straight,
        ),
        generated_at=_now_iso(),
        reward_config=reward_config,
        task_type=task_type,
        object_category=object_category,
        object_goal=object_goal,
    )


def sample_pointnav_episode(
    ucv: UCVClient,
    *,
    seed: int = 42,
    idx: int = 0,
    target_distance_cm: float = 2000.0,
    distance_jitter_cm: float = 500.0,
    success_distance_cm: float = 200.0,
    max_steps: int = 60,
    max_episode_time_s: float = 300.0,
    start_xy: Optional[Tuple[float, float]] = None,
    reward_config: Optional[RewardConfig] = None,
) -> NavigationEpisode:
    """Legacy: sample PointNav without navmesh validation."""
    rng = random.Random(seed + idx)
    sx, sy = start_xy if start_xy is not None else (0.0, 0.0)
    distance = target_distance_cm + rng.uniform(-distance_jitter_cm, distance_jitter_cm)
    angle = rng.uniform(0, 2 * math.pi)
    gx = sx + distance * math.cos(angle)
    gy = sy + distance * math.sin(angle)

    start = Position(x=sx, y=sy, node_type="intersection")
    goal = Position(x=gx, y=gy, node_type="intersection")
    rc = reward_config or RewardConfig()

    return _build_episode(
        seed=seed, idx=idx, start=start, goal=goal,
        success_distance_cm=success_distance_cm,
        max_steps=max_steps, max_episode_time_s=max_episode_time_s,
        reward_config=rc, task_type="pointnav",
    )


def sample_objectnav_episode(
    ucv: UCVClient,
    *,
    seed: int = 42,
    idx: int = 0,
    target_filter: Callable[[str], bool],
    object_category: str,
    success_distance_cm: float = 300.0,
    max_steps: int = 60,
    max_episode_time_s: float = 300.0,
    start_xy: Optional[Tuple[float, float]] = None,
    min_separation_cm: float = 800.0,
    reward_config: Optional[RewardConfig] = None,
) -> NavigationEpisode:
    """Legacy: sample ObjectNav without navmesh validation."""
    rng = random.Random(seed + idx)
    sx, sy = start_xy if start_xy is not None else (0.0, 0.0)

    actors = snapshot_scene(ucv)
    candidates = filter_actors(actors, target_filter)
    if not candidates:
        raise RuntimeError(
            f"sample_objectnav_episode: no actors matched filter; "
            f"scene has {len(actors)} actors"
        )

    far_enough = [
        c for c in candidates
        if math.sqrt((c[1][0] - sx) ** 2 + (c[1][1] - sy) ** 2) >= min_separation_cm
    ]
    pool = far_enough or candidates
    target_name, target_loc = rng.choice(pool)
    gx, gy, _gz = target_loc

    start = Position(x=sx, y=sy, node_type="intersection")
    goal = Position(x=gx, y=gy, node_type="intersection")
    rc = reward_config or RewardConfig()

    obj_goal = ObjectGoal(
        object_id=target_name,
        object_type=target_name,
        object_category=object_category,
        position=goal,
        view_points=(ObjectViewPoint(position=goal, iou=None),),
    )
    return _build_episode(
        seed=seed, idx=idx, start=start, goal=goal,
        success_distance_cm=success_distance_cm,
        max_steps=max_steps, max_episode_time_s=max_episode_time_s,
        reward_config=rc,
        object_goal=obj_goal, task_type="objectnav",
        object_category=object_category,
    )
