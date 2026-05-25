"""Navigation interface built from a 2D scene graph — no SimWorld / roads.json required.

Builds a grid navigation graph from object AABBs (center + size).  Obstacle cells
are any grid nodes whose centre falls inside an obstacle AABB expanded by
``agent_radius_cm``.  A* on the 8-connected grid provides reference paths and
geodesic distances.

World bounds are derived in priority order:

1. Floor / ground polygon — the smallest object whose name matches
   *ground_name_patterns* (default ``("ground", "floor")``).  Using the
   smallest candidate avoids picking an infinite floor plane when a tighter
   arena mesh is also present.
2. Obstacle union + *world_padding_cm* (fallback when no floor/ground object
   is found).

Typical usage::

    from nav_task.scene_graph_interface import SceneGraphNavigationInterface
    from nav_task import NavigationTaskGenerator

    iface = SceneGraphNavigationInterface("my_scene.json")
    gen   = NavigationTaskGenerator(iface, roads_file="my_scene.json")
    episodes = gen.generate(seed=42, n_episodes=10)
"""
from __future__ import annotations

import heapq
import json as _json_mod
import math
import random
from typing import Dict, FrozenSet, List, Optional, Set, Tuple

from .episode import ObjectViewPoint, Position
from .interface import (
    SceneObject,
    UENavigationInterface,
    _compute_path_length,
    _load_scene_objects,
)

# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------

_DEFAULT_BACKGROUND_CLASSES: FrozenSet[str] = frozenset(
    {
        "StaticMeshActor",       # SM_SkySphere, Arena_Env_Ground (world-scale AABBs)
        "Floor_C",               # ground plane
        "NavMeshBoundsVolume",   # UE nav system volume — not a physical obstacle
        "RecastNavMesh",         # UE nav mesh actor — not a physical obstacle
    }
)

# Case-insensitive substring patterns used to identify floor/ground objects
# when deriving world bounds (priority 2).
_DEFAULT_GROUND_NAME_PATTERNS: Tuple[str, ...] = ("ground", "floor")

# ---------------------------------------------------------------------------
# Scene-graph loading helpers
# ---------------------------------------------------------------------------


def _load_raw(scene_graph_file: str) -> Tuple[object, list]:
    """Load a scene-graph JSON file and return ``(raw, items)``.

    *raw* is the top-level parsed value (dict or list).
    *items* is the normalised list of object dicts.
    """
    with open(scene_graph_file) as f:
        raw = _json_mod.load(f)
    items: list = (
        raw
        if isinstance(raw, list)
        else raw.get("elements", raw.get("objects", []))
    )
    return raw, items


def _extract_obstacles(
    items: list,
    background_classes: FrozenSet[str],
) -> List[dict]:
    """Extract obstacle AABBs from an items list.

    Objects whose ``"class"`` is in *background_classes* are skipped.

    Returns dicts with keys ``cx``, ``cy``, ``hw``, ``hh`` (half-extents),
    in the same unit as the source JSON.
    """
    obstacles = []
    for item in items:
        if item.get("class", "") in background_classes:
            continue
        center = item.get("center", {})
        size = item.get("size", {})
        cx = float(center.get("x", 0.0))
        cy = float(center.get("y", 0.0))
        hw = float(size.get("width", 0.0)) / 2.0
        hh = float(size.get("height", 0.0)) / 2.0
        if hw <= 0 or hh <= 0:
            continue
        obstacles.append({"cx": cx, "cy": cy, "hw": hw, "hh": hh})
    return obstacles


def _derive_world_bounds(
    items: list,
    obstacles: List[dict],
    ground_name_patterns: Tuple[str, ...],
    world_padding_cm: float,
) -> Tuple[float, float, float, float]:
    """Derive world bounds with the following priority:

    1. **Floor / ground polygon** — the smallest object (by AABB area) whose
       ``"name"`` contains any of *ground_name_patterns* (case-insensitive).
       Using the smallest candidate avoids accidentally picking an
       infinite-floor plane when a tighter arena mesh is also present.

    2. **Obstacle union + padding** — bounding box of all obstacle AABBs
       expanded by *world_padding_cm* on every side.

    Returns (x_min, x_max, y_min, y_max).
    """
    # ── Priority 1: floor / ground polygon ─────────────────────────────────
    ground_candidates = []
    for item in items:
        name = item.get("name", "").lower()
        if not any(pat in name for pat in ground_name_patterns):
            continue
        center = item.get("center", {})
        size = item.get("size", {})
        cx = float(center.get("x", 0.0))
        cy = float(center.get("y", 0.0))
        hw = float(size.get("width", 0.0)) / 2.0
        hh = float(size.get("height", 0.0)) / 2.0
        if hw > 0 and hh > 0:
            ground_candidates.append((hw * hh, cx, cy, hw, hh))  # (area, …)

    if ground_candidates:
        # Pick the smallest (most specific) floor/ground polygon
        _, cx, cy, hw, hh = min(ground_candidates, key=lambda t: t[0])
        return (cx - hw, cx + hw, cy - hh, cy + hh)

    # ── Priority 2: obstacle union + padding ───────────────────────────────
    x_min = min(o["cx"] - o["hw"] for o in obstacles) - world_padding_cm
    x_max = max(o["cx"] + o["hw"] for o in obstacles) + world_padding_cm
    y_min = min(o["cy"] - o["hh"] for o in obstacles) - world_padding_cm
    y_max = max(o["cy"] + o["hh"] for o in obstacles) + world_padding_cm
    return x_min, x_max, y_min, y_max


# ---------------------------------------------------------------------------
# Grid graph construction
# ---------------------------------------------------------------------------

GridKey = Tuple[int, int]


def _build_grid_graph(
    obstacles: List[dict],
    bounds: Tuple[float, float, float, float],
    resolution_cm: float,
    agent_radius_cm: float,
) -> Tuple[
    Dict[GridKey, Position],
    Dict[GridKey, List[GridKey]],
    float,  # x_origin (world x of grid column 0)
    float,  # y_origin (world y of grid row 0)
]:
    """Build an 8-connected grid navigation graph from obstacle AABBs.

    Grid cells whose centre falls inside any obstacle AABB inflated by
    *agent_radius_cm* are marked occupied.  All remaining cells are free
    and connected to their 8 free neighbours.

    Parameters
    ----------
    obstacles:
        List of AABB dicts (``cx``, ``cy``, ``hw``, ``hh``).
    bounds:
        (x_min, x_max, y_min, y_max) — world extent of the grid.
    resolution_cm:
        Grid cell edge length in cm.
    agent_radius_cm:
        Obstacle inflation distance in cm (clearance margin).

    Returns
    -------
    nodes : dict (gx, gy) → Position
    adj   : dict (gx, gy) → list of free neighbour (gx, gy)
    x_origin, y_origin
    """
    x_min, x_max, y_min, y_max = bounds
    cols = math.ceil((x_max - x_min) / resolution_cm) + 1
    rows = math.ceil((y_max - y_min) / resolution_cm) + 1

    def _occupied(wx: float, wy: float) -> bool:
        for o in obstacles:
            if (
                abs(wx - o["cx"]) < o["hw"] + agent_radius_cm
                and abs(wy - o["cy"]) < o["hh"] + agent_radius_cm
            ):
                return True
        return False

    nodes: Dict[GridKey, Position] = {}
    for gx in range(cols):
        for gy in range(rows):
            wx = x_min + gx * resolution_cm
            wy = y_min + gy * resolution_cm
            if not _occupied(wx, wy):
                nodes[(gx, gy)] = Position(x=wx, y=wy, node_type="free")

    _DIRS = [(-1, -1), (-1, 0), (-1, 1), (0, -1), (0, 1), (1, -1), (1, 0), (1, 1)]
    adj: Dict[GridKey, List[GridKey]] = {k: [] for k in nodes}
    for (gx, gy) in nodes:
        for dx, dy in _DIRS:
            nb: GridKey = (gx + dx, gy + dy)
            if nb in nodes:
                adj[(gx, gy)].append(nb)

    return nodes, adj, x_min, y_min


# ---------------------------------------------------------------------------
# Largest connected component (LCC)
# ---------------------------------------------------------------------------


def _largest_component(
    nodes: Dict[GridKey, Position],
    adj: Dict[GridKey, List[GridKey]],
) -> Set[GridKey]:
    """Return the set of grid keys belonging to the largest connected component.

    Uses iterative BFS to avoid recursion depth limits on large grids.
    Ensures that every start/goal pair sampled from the returned set has a
    valid A* path (no isolated islands).  Mirrors Habitat's ``island_radius``
    filter (``pointnav_generator.py:141``).
    """
    if not nodes:
        return set()

    visited: Set[GridKey] = set()
    best: Set[GridKey] = set()

    for seed in nodes:
        if seed in visited:
            continue
        component: Set[GridKey] = set()
        queue = [seed]
        while queue:
            cur = queue.pop()
            if cur in component:
                continue
            component.add(cur)
            for nb in adj.get(cur, []):
                if nb not in component:
                    queue.append(nb)
        visited |= component
        if len(component) > len(best):
            best = component

    return best


# ---------------------------------------------------------------------------
# A* on grid
# ---------------------------------------------------------------------------


def _astar(
    start: GridKey,
    goal: GridKey,
    adj: Dict[GridKey, List[GridKey]],
    nodes: Dict[GridKey, Position],
) -> Optional[List[GridKey]]:
    """A* shortest path on the pre-built grid graph.

    Uses Euclidean distance as both edge cost and heuristic (admissible
    because straight-line distance ≤ any detour).

    Returns an ordered list of grid keys from *start* to *goal* (inclusive),
    or ``None`` if no path exists.
    """
    if start not in nodes or goal not in nodes:
        return None
    if start == goal:
        return [start]

    gp = nodes[goal]

    def h(k: GridKey) -> float:
        p = nodes[k]
        return math.hypot(p.x - gp.x, p.y - gp.y)

    open_heap: List[Tuple[float, GridKey]] = []
    heapq.heappush(open_heap, (h(start), start))
    came_from: Dict[GridKey, Optional[GridKey]] = {start: None}
    g_cost: Dict[GridKey, float] = {start: 0.0}

    while open_heap:
        _, current = heapq.heappop(open_heap)
        if current == goal:
            path: List[GridKey] = []
            node: Optional[GridKey] = current
            while node is not None:
                path.append(node)
                node = came_from[node]
            path.reverse()
            return path

        cur_g = g_cost[current]
        cp = nodes[current]
        for nb in adj.get(current, []):
            np_ = nodes[nb]
            new_g = cur_g + math.hypot(cp.x - np_.x, cp.y - np_.y)
            if new_g < g_cost.get(nb, math.inf):
                came_from[nb] = current
                g_cost[nb] = new_g
                heapq.heappush(open_heap, (new_g + h(nb), nb))

    return None


# ---------------------------------------------------------------------------
# Interface
# ---------------------------------------------------------------------------


class SceneGraphNavigationInterface(UENavigationInterface):
    """Navigation interface built purely from a 2D scene graph.

    No SimWorld or ``roads.json`` required.  Works with any generated scene
    that provides object positions and sizes in a JSON file.

    World bounds are derived in priority order (see module docstring).

    Parameters
    ----------
    scene_graph_file:
        Path to a scene-graph JSON file.  Accepts a plain list or a dict
        with an ``"elements"`` key.
    resolution_cm:
        Grid cell edge length in cm (default 500 cm = 5 m).
    agent_radius_cm:
        Obstacle inflation in cm (default 100 cm = 1 m).
    background_classes:
        ``"class"`` values to exclude from obstacle detection.
        Defaults to ``{"StaticMeshActor", "Floor_C"}``.
    ground_name_patterns:
        Case-insensitive name substrings that identify floor/ground objects
        used for bounds derivation (priority 2).
        Defaults to ``("ground", "floor")``.
    world_padding_cm:
        Extra margin added around obstacle union when bounds must fall back
        to priority 3.  Ignored when bounds come from priority 1 or 2.
    elements_file:
        Optional SimWorld ``elements.json`` for ObjectNav scene-object loading.
    ue_assets_file:
        Optional ``ue_assets.json`` for ObjectNav category mapping.
    """

    def __init__(
        self,
        scene_graph_file: str,
        resolution_cm: float = 500.0,
        agent_radius_cm: float = 100.0,
        background_classes: Optional[FrozenSet[str]] = None,
        ground_name_patterns: Tuple[str, ...] = _DEFAULT_GROUND_NAME_PATTERNS,
        world_padding_cm: float = 1000.0,
        elements_file: Optional[str] = None,
        ue_assets_file: Optional[str] = None,
    ) -> None:
        self._scene_graph_file = scene_graph_file
        self._resolution_cm = resolution_cm
        self._elements_file = elements_file
        self._ue_assets_file = ue_assets_file

        if background_classes is None:
            background_classes = _DEFAULT_BACKGROUND_CLASSES

        _, items = _load_raw(scene_graph_file)
        obstacles = _extract_obstacles(items, background_classes)
        if not obstacles:
            raise ValueError(
                f"No obstacle objects found in '{scene_graph_file}'. "
                "Check background_classes filter or scene graph format."
            )

        bounds = _derive_world_bounds(
            items, obstacles, ground_name_patterns, world_padding_cm
        )

        self._nodes, self._adj, self._x_origin, self._y_origin = _build_grid_graph(
            obstacles, bounds, resolution_cm, agent_radius_cm
        )
        if not self._nodes:
            raise ValueError(
                "Grid has no free cells after obstacle inflation. "
                "Try reducing agent_radius_cm or increasing resolution_cm."
            )

        # Restrict to the largest connected component so every sampled
        # start/goal pair is guaranteed reachable (mirrors Habitat's
        # island_radius filter in pointnav_generator.py:141).
        lcc = _largest_component(self._nodes, self._adj)
        self._nodes = {k: v for k, v in self._nodes.items() if k in lcc}
        self._adj = {
            k: [nb for nb in v if nb in lcc]
            for k, v in self._adj.items()
            if k in lcc
        }

        xs = [p.x for p in self._nodes.values()]
        ys = [p.y for p in self._nodes.values()]
        self._x_min, self._x_max = min(xs), max(xs)
        self._y_min, self._y_max = min(ys), max(ys)

        # Stable sorted list for deterministic seeded sampling
        self._positions: List[Position] = [
            self._nodes[k] for k in sorted(self._nodes)
        ]

        self._geodesic_cache: Dict[Tuple[GridKey, GridKey], Optional[float]] = {}

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _snap(self, pos: Position) -> Optional[GridKey]:
        """Snap a world position to the nearest free grid cell.

        Starts at the grid cell nearest to *pos* and expands outward ring
        by ring until a free cell is found.  O(1) for positions in free
        space; O(r²) near obstacles where r is the clearance in grid cells.
        """
        gx0 = round((pos.x - self._x_origin) / self._resolution_cm)
        gy0 = round((pos.y - self._y_origin) / self._resolution_cm)

        max_r = (
            max(max(abs(k[0] - gx0), abs(k[1] - gy0)) for k in self._nodes)
            if self._nodes
            else 0
        )

        for r in range(max_r + 1):
            best_key: Optional[GridKey] = None
            best_dist = math.inf
            for dx in range(-r, r + 1):
                for dy in range(-r, r + 1):
                    if max(abs(dx), abs(dy)) != r:
                        continue
                    k: GridKey = (gx0 + dx, gy0 + dy)
                    if k in self._nodes:
                        p = self._nodes[k]
                        d = math.hypot(p.x - pos.x, p.y - pos.y)
                        if d < best_dist:
                            best_dist = d
                            best_key = k
            if best_key is not None:
                return best_key
        return None

    # ------------------------------------------------------------------
    # UENavigationInterface abstract methods
    # ------------------------------------------------------------------

    def get_navigable_positions(
        self,
        node_type: Optional[str] = None,
        count: Optional[int] = None,
        rng: Optional[random.Random] = None,
    ) -> List[Position]:
        positions = list(self._positions)
        if count is not None:
            if count > len(positions):
                raise ValueError(
                    f"Requested {count} positions but only {len(positions)} available"
                )
            _rng = rng if rng is not None else random.Random()
            positions = _rng.sample(positions, count)
        return positions

    def get_reference_path(
        self,
        start: Position,
        goal: Position,
    ) -> Optional[List[Position]]:
        sk = self._snap(start)
        gk = self._snap(goal)
        if sk is None or gk is None:
            return None
        if sk == gk:
            return None  # same cell → matches existing interface contract
        grid_path = _astar(sk, gk, self._adj, self._nodes)
        if grid_path is None or len(grid_path) < 2:
            return None
        return [self._nodes[k] for k in grid_path]

    def get_shortest_path_length(
        self,
        start: Position,
        goal: Position,
    ) -> Optional[float]:
        path = self.get_reference_path(start, goal)
        return _compute_path_length(path) if path is not None else None

    def get_geodesic_distance(
        self,
        start: Position,
        goal: Position,
    ) -> Optional[float]:
        sk = self._snap(start)
        gk = self._snap(goal)
        if sk is None or gk is None:
            return None
        if sk == gk:
            return math.hypot(start.x - goal.x, start.y - goal.y)
        cache_key = (sk, gk)
        if cache_key in self._geodesic_cache:
            return self._geodesic_cache[cache_key]
        dist = self.get_shortest_path_length(start, goal)
        self._geodesic_cache[cache_key] = dist
        return dist

    def get_world_bounds(self) -> Tuple[float, float, float, float]:
        return (self._x_min, self._x_max, self._y_min, self._y_max)

    def get_agent_position(self) -> Position:
        raise NotImplementedError(
            "SceneGraphNavigationInterface has no live agent. "
            "Pass current_position_cm directly to reward functions."
        )

    def get_collision_counts(self) -> dict:
        return {
            "HumanCollision": 0,
            "ObjectCollision": 0,
            "BuildingCollision": 0,
            "VehicleCollision": 0,
        }

    def get_scene_objects(self, category: Optional[str] = None) -> List[SceneObject]:
        if self._elements_file is None:
            return []
        return _load_scene_objects(
            self._elements_file,
            ue_assets_file=self._ue_assets_file,
            category=category,
        )

    def clear_geodesic_cache(self) -> None:
        """Clear the geodesic distance cache."""
        self._geodesic_cache = {}
