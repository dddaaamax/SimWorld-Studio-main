"""Task difficulty scoring: 0-10 per task, rubric-based.

Each dimension is scored independently, total = sum of all dimensions.
Scores come from the task's GT trajectory (from NavMesh episode generation).

NavMesh is REQUIRED. If unavailable, the epoch must be skipped and
the coding agent must be told to fix the scene.

Dimensions:
  1. Path length (0-2.5): geodesic distance of GT path
  2. Detour ratio (0-2.5): geodesic / euclidean — how winding the GT path is
  3. Scene blocked ratio (0-2.5): fraction of area blocked by objects (NavMesh)
  4. Heading offset (0-1.0): how far agent starts from facing goal
  5. Task type (0-1.5): objectnav harder than pointnav

Max total: 2.5 + 2.5 + 2.5 + 1.0 + 1.5 = 10.0
"""
from __future__ import annotations

import logging
import math
import random
from typing import Any, Dict, List, Optional, Tuple

log = logging.getLogger(__name__)


class NavMeshRequired(Exception):
    """Raised when NavMesh is not available but required."""
    pass


def score_task_difficulty(
    geodesic_cm: float,
    euclidean_cm: float,
    heading_offset_deg: float = 0.0,
    task_type: str = "pointnav",
    blocked_ratio: float = 0.0,
) -> Dict[str, Any]:
    """Score a single task's difficulty 0-10.

    Args:
        geodesic_cm: GT path length from NavMesh
        euclidean_cm: straight-line start-to-goal distance
        heading_offset_deg: angle between start heading and goal direction (0-180)
        task_type: "pointnav" or "objectnav"
        blocked_ratio: fraction of scene area blocked (from NavMesh measurement)
    """
    # 1. Path length (0-2.5): 500cm=0.5, 1000=1.0, 2500=2.5
    path_score = min(2.5, geodesic_cm / 1000.0)

    # 2. Detour ratio (0-2.5): 1.0=straight=0, 1.5=moderate=1.25, 2.0+=max
    detour = geodesic_cm / euclidean_cm if euclidean_cm > 0 else 1.0
    detour_score = min(2.5, (detour - 1.0) * 2.5)
    detour_score = max(0.0, detour_score)

    # 3. Scene blocked ratio (0-2.5): 0=empty=0, 0.3=moderate=1.5, 0.5+=max
    scene_score = min(2.5, blocked_ratio * 5.0)

    # 4. Heading offset (0-1.0): 0°=facing goal=0, 90°=0.5, 180°=1.0
    heading_score = min(1.0, heading_offset_deg / 180.0)

    # 5. Task type (0-1.5)
    task_score = 0.0 if task_type == "pointnav" else 1.5

    total = path_score + detour_score + scene_score + heading_score + task_score
    total = round(min(10.0, total), 1)

    return {
        "total": total,
        "path_length": round(path_score, 2),
        "detour_ratio": round(detour_score, 2),
        "detour_raw": round(detour, 3),
        "scene_blocked": round(scene_score, 2),
        "heading_offset": round(heading_score, 2),
        "task_type_score": round(task_score, 2),
        "geodesic_cm": round(geodesic_cm, 0),
        "euclidean_cm": round(euclidean_cm, 0),
    }


def measure_blocked_ratio(
    ucv,
    center: Tuple[float, float] = (7661.0, 10970.0),
    bounds_half: float = 10000.0,
    n_samples: int = 100,
    seed: int = 42,
) -> float:
    """Measure fraction of scene area blocked by objects.

    Samples uniform grid points (center ± bounds_half) and uses NavMesh
    projection to check if each point is navigable.
    Returns blocked_ratio = 1 - navigable_ratio.

    Raises NavMeshRequired if NavMesh is not functional.
    """
    rng = random.Random(seed)
    n_navigable = 0
    n_total = 0
    cx, cy = center

    for _ in range(n_samples):
        x = rng.uniform(cx - bounds_half, cx + bounds_half)
        y = rng.uniform(cy - bounds_half, cy + bounds_half)
        n_total += 1
        try:
            resp = ucv.send(f"vget /nav/project {x} {y} 100")
            resp = resp.strip()
            if resp and not resp.startswith("error") and resp != "-1":
                parts = resp.split(",")
                if len(parts) >= 2:
                    px, py = float(parts[0]), float(parts[1])
                    # Valid if projected point is not degenerate (all zeros)
                    if abs(px) > 1 or abs(py) > 1:
                        n_navigable += 1
                    elif abs(x - cx) < 100 and abs(y - cy) < 100:
                        # Near center is valid
                        n_navigable += 1
        except Exception:
            pass

    if n_total == 0:
        raise NavMeshRequired("No samples taken")

    navigable_ratio = n_navigable / n_total
    if navigable_ratio < 0.01:
        raise NavMeshRequired(
            f"NavMesh returned 0 navigable points out of {n_total} samples. "
            "NavMesh may not be built or scene has no walkable surface."
        )

    blocked = 1.0 - navigable_ratio
    log.info("Blocked ratio: %.2f (%d/%d navigable)", blocked, n_navigable, n_total)
    return blocked


def measure_float_penalty(ucv, spawned_names: list, threshold_cm: float = 200.0) -> float:
    """Return fraction of spawned objects floating >threshold_cm above navmesh.

    For each object, compare its actual z to the navmesh-projected floor z at
    the same (x,y).  Returns 0.0 (no penalty) to 1.0 (all objects floating).
    """
    if not spawned_names:
        return 0.0
    floating = 0
    for name in spawned_names:
        try:
            loc = ucv.send(f"vget /object/{name}/location").strip()
            parts = loc.split(",")
            if len(parts) < 3:
                continue
            ox, oy, oz = float(parts[0]), float(parts[1]), float(parts[2])
            nav = ucv.send(f"vget /nav/project {ox} {oy} 0").strip()
            np_parts = nav.split(",")
            if len(np_parts) >= 3 and not nav.startswith("error"):
                floor_z = float(np_parts[2])
                if abs(oz - floor_z) > threshold_cm:
                    floating += 1
                    log.warning("Object %s at z=%.0f, floor_z=%.0f (delta=%.0f) — floating",
                                name, oz, floor_z, abs(oz - floor_z))
        except Exception:
            pass
    ratio = floating / len(spawned_names)
    if ratio > 0:
        log.warning("Float penalty: %d/%d objects floating (penalty=%.2f)",
                    floating, len(spawned_names), ratio)
    return ratio


def compute_coding_reward(
    nav_sr: float,
    difficulty: float = 0.0,
    best_difficulty: float = 0.0,
    target_sr: float = 0.6,
    sigma: float = 0.2,
    progress_beta: float = 0.25,
    float_penalty: float = 0.0,
) -> float:
    """ZPD-shaped reward with a monotone-difficulty bonus.

    Base: Gaussian over SR centered at target_sr=0.6. Unique stable fixed point,
    symmetric penalty for too-easy and too-hard. Fixes the 1-SR bang-bang seen
    in coevolve_20260420_055752.

    Progress term: multiplicative bonus when the current difficulty is at or
    above the running best. This rewards monotone curriculum ascent — the
    coding agent no longer gets the same reward for SR=0.6 at diff=2.0 as at
    diff=4.0. Cap at 1 + progress_beta so the bonus can't overwhelm the SR
    shaping.

    At target_sr=0.6, best_difficulty=diff: reward=1.0*(1+beta)=1.25.
    At target_sr=0.6, diff<<best: reward=1.0*1.0=1.0 (no bonus, no penalty).
    SR<0.1 (catastrophic): reward=0 regardless of difficulty.
    """
    if nav_sr < 0.1:
        return 0.0
    sr_component = math.exp(-((nav_sr - target_sr) ** 2) / (2 * sigma * sigma))
    progress = max(0.0, min(1.0, (difficulty - best_difficulty + 0.5) / 1.0))
    reward = sr_component * (1.0 + progress_beta * progress)
    # Penalise floating objects — multiplier drops to 0 if all objects float.
    reward *= (1.0 - float_penalty)
    return reward
