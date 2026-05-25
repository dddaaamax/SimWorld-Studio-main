"""NavmeshNavigationInterface: pure navmesh navigation — no scene graph needed.

All position sampling, path queries, and reachability checks go through
the UE Recast/Detour navmesh via UnrealCV ``vget /nav/*`` commands.

Commands used:
    vset /nav/build minX minY minZ maxX maxY maxZ
    vget /nav/path x1 y1 z1 x2 y2 z2
    vget /nav/reachable x1 y1 z1 x2 y2 z2
    vget /nav/random_points N
    vget /nav/random_reachable x y z radius N
    vget /nav/project x y z
    vget /nav/status

Requires a live UE session in PIE with the NavigationHandler plugin.
"""

from __future__ import annotations

import math
import random
from typing import Dict, List, Optional, Tuple

from .episode import Position


def _parse_nav_path_response(response: str) -> dict:
    """Parse ``"length|x0,y0,z0|x1,y1,z1|..."`` or ``"-1"``."""
    response = response.strip()
    if response == "-1":
        return {"length": -1.0, "waypoints": []}
    parts = response.split("|")
    length = float(parts[0])
    waypoints = []
    for part in parts[1:]:
        coords = part.split(",")
        if len(coords) >= 3:
            waypoints.append([float(c) for c in coords[:3]])
    return {"length": length, "waypoints": waypoints}


def _parse_points_response(response: str) -> List[Position]:
    """Parse ``"count|x,y,z|x,y,z|..."``."""
    response = response.strip()
    if not response or response.startswith("error"):
        return []
    parts = response.split("|")
    try:
        n = int(parts[0])
    except ValueError:
        return []
    points = []
    for part in parts[1 : n + 1]:
        coords = part.split(",")
        if len(coords) >= 2:
            points.append(Position(
                x=float(coords[0]), y=float(coords[1]), node_type="navmesh",
            ))
    return points


class NavmeshNavigationInterface:
    """Pure navmesh navigation interface — all queries via UE UnrealCV.

    No scene graph, no offline grid, no A*. Everything comes from the
    live UE navmesh.

    Parameters
    ----------
    ucv_client:
        A connected UCVClient instance.
    """

    def __init__(self, ucv_client) -> None:
        self._ucv = ucv_client
        self._geodesic_cache: Dict[Tuple[float, float, float, float], Optional[float]] = {}

    # ------------------------------------------------------------------
    # Build
    # ------------------------------------------------------------------

    def build_navmesh(
        self,
        min_x: float = -11000, min_y: float = -11000, min_z: float = -1000,
        max_x: float = 11000, max_y: float = 11000, max_z: float = 3000,
        padding_cm: float = 0.0,
    ) -> str:
        """Build navmesh over a bounding box."""
        cmd = (
            f"vset /nav/build "
            f"{min_x - padding_cm} {min_y - padding_cm} {min_z} "
            f"{max_x + padding_cm} {max_y + padding_cm} {max_z}"
        )
        return self._ucv.send(cmd)

    def build_navmesh_from_actor(self, actor_name: str, padding_cm: float = 500.0) -> str:
        cmd = f"vset /nav/build_from_actor {actor_name} {padding_cm}"
        return self._ucv.send(cmd)

    # ------------------------------------------------------------------
    # Position sampling — all from navmesh, no scene graph
    # ------------------------------------------------------------------

    def get_navigable_positions(
        self,
        count: int = 500,
        rng: Optional[random.Random] = None,
    ) -> List[Position]:
        """Sample navigable positions directly from the UE navmesh."""
        # Request more than needed to allow deduplication
        request_count = min(count * 2, 10000)
        cmd = f"vget /nav/random_points {request_count}"
        response = self._ucv.send(cmd).strip()
        points = _parse_points_response(response)

        # Deduplicate (navmesh random can return nearby points)
        if len(points) > count:
            if rng is not None:
                rng.shuffle(points)
            else:
                random.shuffle(points)
            points = points[:count]

        return points

    def get_random_reachable_points(
        self, origin: Position, radius: float, count: int,
    ) -> List[Position]:
        """Sample points reachable from origin within radius."""
        cmd = f"vget /nav/random_reachable {origin.x} {origin.y} 0 {radius} {count}"
        return _parse_points_response(self._ucv.send(cmd))

    # ------------------------------------------------------------------
    # Path queries
    # ------------------------------------------------------------------

    def get_reference_path(
        self, start: Position, goal: Position,
    ) -> Optional[List[Position]]:
        """Query navmesh for the shortest path."""
        cmd = f"vget /nav/path {start.x} {start.y} 0 {goal.x} {goal.y} 0"
        result = _parse_nav_path_response(self._ucv.send(cmd))
        if result["length"] < 0:
            return None
        waypoints = [
            Position(x=float(p[0]), y=float(p[1]), node_type="navmesh")
            for p in result["waypoints"]
        ]
        return waypoints if len(waypoints) >= 2 else None

    def get_geodesic_distance(
        self, start: Position, goal: Position,
    ) -> Optional[float]:
        """Query navmesh geodesic distance (cached)."""
        # Round to 100cm grid for cache key
        key = (
            round(start.x / 100) * 100, round(start.y / 100) * 100,
            round(goal.x / 100) * 100, round(goal.y / 100) * 100,
        )
        if key in self._geodesic_cache:
            return self._geodesic_cache[key]

        cmd = f"vget /nav/path {start.x} {start.y} 0 {goal.x} {goal.y} 0"
        result = _parse_nav_path_response(self._ucv.send(cmd))
        dist = result["length"] if result["length"] >= 0 else None
        self._geodesic_cache[key] = dist
        return dist

    def get_shortest_path_length(
        self, start: Position, goal: Position,
    ) -> Optional[float]:
        return self.get_geodesic_distance(start, goal)

    # ------------------------------------------------------------------
    # Reachability & projection
    # ------------------------------------------------------------------

    def is_reachable(self, start: Position, goal: Position) -> bool:
        cmd = f"vget /nav/reachable {start.x} {start.y} 0 {goal.x} {goal.y} 0"
        return self._ucv.send(cmd).strip() == "true"

    def project_to_navmesh(self, x: float, y: float, z: float = 0) -> Optional[Position]:
        """Project a point onto the navmesh. Returns None if not projectable."""
        resp = self._ucv.send(f"vget /nav/project {x} {y} {z}").strip()
        if resp == "-1":
            return None
        coords = resp.split(",")
        if len(coords) >= 2:
            return Position(x=float(coords[0]), y=float(coords[1]), node_type="navmesh")
        return None

    # ------------------------------------------------------------------
    # Status
    # ------------------------------------------------------------------

    def get_status(self) -> str:
        return self._ucv.send("vget /nav/status")

    def clear_cache(self) -> None:
        self._geodesic_cache.clear()
