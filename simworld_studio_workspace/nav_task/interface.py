"""Abstract and concrete UE navigation interface implementations.

Both implementations use SimWorld's Map class for graph-based pathfinding.
Neither requires a live UE process for position sampling or A* routing.
The UE connection in UnrealCVNavigationInterface is reserved for future
in-engine navmesh extensions (e.g., ProjectPointToNavigation via
execute_python_script on MCP port 55560).

NOTE: SimWorld must be on sys.path. Add the following before importing this
module if SimWorld is not installed as a package:

    import sys
    sys.path.insert(0, "/home/tina/SimWorld")
"""

from __future__ import annotations

import abc
import json as _json_mod
import random
import socket
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

from .episode import ObjectViewPoint, Position

# ── SimWorld imports ────────────────────────────────────────────────────────
# SimWorld's __init__.py pulls in GUI-only modules (PyQt5, pyqtgraph) that are
# not available in headless environments. We bypass it by:
#   1. Stubbing GUI-only packages in sys.modules before any simworld import.
#   2. Pre-populating the 'simworld' package stub so Python never executes
#      simworld/__init__.py (which would import CityFunctionCall → pyqtgraph).
#   3. Loading only the three submodules we actually need via importlib.

_SIMWORLD_ROOT = str(Path(__file__).parents[2] / "SimWorld")


def _ensure_simworld_importable() -> None:
    """Load SimWorld submodules without triggering the full package init."""
    import importlib.util
    import types

    # ── Step 1: stub out GUI packages ────────────────────────────────────
    def _stub(name: str, attrs: dict = None) -> types.ModuleType:
        if name not in sys.modules:
            mod = types.ModuleType(name)
            sys.modules[name] = mod
            # Attach to parent package stub if present
            if "." in name:
                parent_name, child = name.rsplit(".", 1)
                parent = sys.modules.get(parent_name)
                if parent:
                    setattr(parent, child, mod)
        mod = sys.modules[name]
        for k, v in (attrs or {}).items():
            setattr(mod, k, v)
        return mod

    _stub("PyQt5")
    _stub("PyQt5.QtCore", {"Qt": type("Qt", (), {})()})
    _stub("PyQt5.QtGui",  {"QColor": object, "QPainter": object, "QPen": object})
    _stub("PyQt5.QtWidgets", {"QApplication": object, "QWidget": object})
    _stub("pyqtgraph")

    # ── Step 2: add SimWorld root to sys.path ─────────────────────────────
    if _SIMWORLD_ROOT not in sys.path:
        sys.path.insert(0, _SIMWORLD_ROOT)

    # ── Step 3: pre-populate simworld package stubs ───────────────────────
    # This prevents Python from executing simworld/__init__.py, which would
    # cascade into unavailable GUI dependencies.
    simworld_pkg_path = str(Path(_SIMWORLD_ROOT) / "simworld")

    def _load(module_name: str, file_path: str) -> types.ModuleType:
        """Load a module by file path, bypassing package __init__ files."""
        if module_name in sys.modules:
            return sys.modules[module_name]
        spec = importlib.util.spec_from_file_location(module_name, file_path)
        mod = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = mod  # register before exec to handle circular refs
        spec.loader.exec_module(mod)
        return mod

    # Pre-register package stubs (so submodule imports resolve to these)
    for pkg in ("simworld", "simworld.config", "simworld.utils", "simworld.map"):
        if pkg not in sys.modules:
            m = types.ModuleType(pkg)
            m.__path__ = [simworld_pkg_path + "/" + pkg.replace("simworld.", "").replace(".", "/")]
            m.__package__ = pkg
            sys.modules[pkg] = m

    # Load in dependency order, exposing symbols on package stubs immediately
    # after each load so that later modules can import from the stub.
    _load("simworld.config.config_loader",
          f"{simworld_pkg_path}/config/config_loader.py")
    sys.modules["simworld.config"].Config = sys.modules["simworld.config.config_loader"].Config

    _load("simworld.utils.vector",
          f"{simworld_pkg_path}/utils/vector.py")
    sys.modules["simworld.utils"].Vector = sys.modules["simworld.utils.vector"].Vector

    _load("simworld.utils.logger",
          f"{simworld_pkg_path}/utils/logger.py")
    _load("simworld.utils.load_json",
          f"{simworld_pkg_path}/utils/load_json.py")

    # map.py does `from simworld.config import Config` and
    # `from simworld.utils.load_json import load_json` at module level,
    # so both must be exposed on their package stubs before this call.
    _load("simworld.map.map",
          f"{simworld_pkg_path}/map/map.py")
    sys.modules["simworld.map"].Map = sys.modules["simworld.map.map"].Map
    sys.modules["simworld.map"].Node = sys.modules["simworld.map.map"].Node
    sys.modules["simworld.map"].Edge = sys.modules["simworld.map.map"].Edge


_ensure_simworld_importable()

from simworld.map.map import Map, Node          # noqa: E402
from simworld.utils.vector import Vector        # noqa: E402
from simworld.config.config_loader import Config  # noqa: E402


def _build_map(roads_file: str, sidewalk_offset: float) -> Map:
    """Construct a Map instance from a roads.json file.

    Uses Config() which loads SimWorld's default.yaml. The roads_file and
    sidewalk_offset are passed explicitly so the config values are only used
    as fallbacks (they won't be reached).
    """
    config = Config()
    m = Map(config=config)
    m.initialize_map_from_file(roads_file=roads_file, sidewalk_offset=sidewalk_offset)
    return m


def _node_to_position(node: Node) -> Position:
    """Convert a SimWorld Node to a Position dataclass."""
    return Position(x=node.position.x, y=node.position.y, node_type=node.type)


def _compute_path_length(waypoints: List[Position]) -> float:
    """Sum Euclidean distances along an ordered waypoint list (in cm)."""
    total = 0.0
    for i in range(len(waypoints) - 1):
        total += waypoints[i].distance_to(waypoints[i + 1])
    return total


@dataclass(frozen=True)
class SceneObject:
    """A scene object instance loaded from ``elements.json``.

    Attributes:
        object_type: UE blueprint class, e.g. ``"BP_Trash_can_C"``.
        category: Semantic category from ``ue_assets.json``, e.g. ``"TRASH"``.
        position: Center position in cm.
        instance_index: Ordinal index among instances of the same type.
    """

    object_type: str
    category: str
    position: Position
    instance_index: int


_UE_ASSETS_PATH = str(Path(__file__).parents[2] / "SimWorld" / "simworld" / "data" / "ue_assets.json")


def _load_scene_objects(
    elements_file: str,
    ue_assets_file: Optional[str] = None,
    category: Optional[str] = None,
) -> List[SceneObject]:
    """Parse ``elements.json`` + ``ue_assets.json`` into SceneObject list.

    Args:
        elements_file: Path to a SimWorld ``elements.json``.
        ue_assets_file: Path to ``ue_assets.json``. Defaults to the
            SimWorld install at ``SimWorld/simworld/data/ue_assets.json``.
        category: If given, return only objects of this category.

    Returns:
        List of SceneObject with coordinates converted to cm.
    """
    if ue_assets_file is None:
        ue_assets_file = _UE_ASSETS_PATH
    with open(ue_assets_file) as f:
        ue_assets = _json_mod.load(f)
    with open(elements_file) as f:
        elements_data = _json_mod.load(f)

    # Build type → category mapping from ue_assets "color" field
    type_to_category: dict = {}
    for key, val in ue_assets.items():
        if key == "colors" or not isinstance(val, dict):
            continue
        if "color" in val:
            type_to_category[key] = val["color"]

    # Track instance counts per type for unique indexing
    type_counts: dict = {}
    objects: List[SceneObject] = []
    for elem in elements_data.get("elements", []):
        etype = elem["type"]
        # Lookup category: try exact, then without BP_ prefix
        cat = type_to_category.get(etype) or type_to_category.get(
            etype.replace("BP_", "")
        )
        if cat is None:
            continue
        if category is not None and cat != category:
            continue
        idx = type_counts.get(etype, 0)
        type_counts[etype] = idx + 1
        center = elem["center"]
        objects.append(
            SceneObject(
                object_type=etype,
                category=cat,
                position=Position(
                    x=center["x"] * 100.0,
                    y=center["y"] * 100.0,
                    node_type="intersection",
                ),
                instance_index=idx,
            )
        )
    return objects


def _compute_view_points(
    obj_position: Position,
    navigable_positions: List[Position],
    interface: "UENavigationInterface",
    max_geodesic_cm: float = 500.0,
) -> List[ObjectViewPoint]:
    """Find navigable positions near an object (candidate view_points).

    Uses **geodesic distance** (graph shortest path) rather than Euclidean
    to avoid selecting nodes on the wrong side of a building block.
    A node that is Euclidean-close but geodesically far is not a valid
    view_point (the agent can't see the object from there).

    Actual visibility / IOU scoring requires a live UE renderer and is
    not available at offline generation time.  Geodesic proximity is the
    best offline heuristic for outdoor city navigation.

    Args:
        obj_position: Object center in cm.
        navigable_positions: All graph nodes.
        interface: Navigation interface for geodesic distance queries.
        max_geodesic_cm: Maximum geodesic distance from a graph node to
            the object's nearest node to qualify as a view_point.

    Returns:
        List of ObjectViewPoint sorted by geodesic distance (nearest first).
    """
    # First, find the graph node closest (Euclidean) to the object to use
    # as the anchor for geodesic queries.
    if not navigable_positions:
        return []
    obj_anchor = min(navigable_positions, key=lambda p: p.distance_to(obj_position))

    candidates = []
    for pos in navigable_positions:
        geo = interface.get_geodesic_distance(pos, obj_anchor)
        if geo is not None and geo <= max_geodesic_cm:
            candidates.append((geo, ObjectViewPoint(position=pos)))
    candidates.sort(key=lambda x: x[0])
    return [vp for _, vp in candidates]


class UENavigationInterface(abc.ABC):
    """Abstract bridge between the task generator and the UE navigation stack.

    Implementations must be constructable without a live UE process.
    All coordinates are in cm.
    """

    @abc.abstractmethod
    def get_navigable_positions(
        self,
        node_type: Optional[str] = None,
        count: Optional[int] = None,
        rng: Optional[random.Random] = None,
    ) -> List[Position]:
        """Return navigable positions drawn from the scene graph.

        Args:
            node_type: If given, restrict to nodes of this type
                ('sidewalk', 'crosswalk', 'intersection').
                NOTE: In the base SimWorld map all nodes are 'intersection'.
                Pass None to sample from all nodes.
            count: If given, sample exactly this many positions without
                replacement; if None, return all matching nodes.
            rng: A seeded random.Random instance supplied by the caller.
                Implementations must not create their own RNG — this
                parameter is the single source of randomness.

        Returns:
            List of Position objects in cm.

        Raises:
            ValueError: If count > number of available nodes.
            RuntimeError: If the graph cannot be queried.
        """
        ...

    @abc.abstractmethod
    def get_shortest_path_length(
        self,
        start: Position,
        goal: Position,
    ) -> Optional[float]:
        """Return the A* path length between two positions in cm.

        Args:
            start: Start position in cm.
            goal: Goal position in cm.

        Returns:
            Path length in cm, or None if no path exists.
        """
        ...

    @abc.abstractmethod
    def get_reference_path(
        self,
        start: Position,
        goal: Position,
    ) -> Optional[List[Position]]:
        """Return the ordered A* waypoint sequence from start to goal.

        Args:
            start: Start position in cm.
            goal: Goal position in cm.

        Returns:
            Ordered list of Position objects (including start and goal),
            or None if no path exists.
        """
        ...

    @abc.abstractmethod
    def get_world_bounds(self) -> tuple:
        """Return (x_min, x_max, y_min, y_max) bounding all map nodes in cm.

        The bounds include the sidewalk-offset margin already applied during
        map construction, so every navigable position is guaranteed to lie
        strictly inside them.

        Returns:
            4-tuple of floats: (x_min, x_max, y_min, y_max) in cm.
        """
        ...

    @abc.abstractmethod
    def get_geodesic_distance(
        self,
        start: "Position",
        goal: "Position",
    ) -> Optional[float]:
        """Return the geodesic (shortest navigable path) distance in cm.

        Unlike Euclidean distance, this accounts for obstacles and the
        actual navigable topology. For graph-based implementations this
        is the A* path length; for UE navmesh implementations it queries
        the engine's navigation system.

        Args:
            start: Start position in cm.
            goal: Goal position in cm.

        Returns:
            Geodesic distance in cm, or None if no path exists.
        """
        ...

    @abc.abstractmethod
    def get_agent_position(self) -> "Position":
        """Return the agent's current position in the live UE scene (in cm).

        This is the runtime observation entry-point used by reward functions:
            reward_fn.step_from_interface(ue_interface)
                → calls get_agent_position()
                → passes result to reward_fn.step()

        Implementations
        ---------------
        UnrealCVNavigationInterface:
            Query UE via UnrealCV TCP (e.g. ``vget /camera/0/location``), then
            snap ``node_type`` from the nearest graph node.

        MockNavigationInterface:
            Returns the position set by ``set_agent_position()`` for tests;
            if unset, raises ``RuntimeError`` (tests may pass coordinates
            directly to ``reward_fn.step()`` instead).

        Returns:
            Position representing the agent's (x, y, node_type) in cm.

        Raises:
            ConnectionError / RuntimeError: If UE is unreachable or the
                mock has no position set.
        """
        ...

    @abc.abstractmethod
    def get_collision_counts(self) -> dict:
        """Return per-category collision counts since the last reset.

        Categories follow the SimWorld ``GetCollisionNum`` blueprint call:
        ``HumanCollision``, ``ObjectCollision``, ``BuildingCollision``,
        ``VehicleCollision``.

        Returns:
            Dict mapping category name to cumulative int count.
        """
        ...

    def get_scene_objects(
        self,
        category: Optional[str] = None,
    ) -> List["SceneObject"]:
        """Return scene objects, optionally filtered by category.

        Non-abstract with empty default: implementations that lack an
        ``elements.json`` (pure PointNav) can ignore this method.

        Override to load objects from ``elements.json`` + ``ue_assets.json``
        for ObjectNav episode generation.

        Args:
            category: If given, return only objects of this category.

        Returns:
            List of SceneObject instances (empty if not overridden).
        """
        return []

    def compute_view_points_with_visibility(
        self,
        object_actor_name: str,
        candidate_positions: List["Position"],
        **kwargs,
    ) -> List[ObjectViewPoint]:
        """Compute view_points with true visibility via UE renderer.

        Default returns empty list (no UE session).  Override in
        ``UnrealCVNavigationInterface`` to use segmentation masks.

        Args:
            object_actor_name: UE actor name of the target object.
            candidate_positions: Graph nodes to test.

        Returns:
            List of ObjectViewPoint with IOU scores.
        """
        return []


class _GraphNavigationMixin:
    """Shared graph-based implementation used by both real and mock interfaces.

    Subclasses must set self._map (a SimWorld Map instance) before calling
    any methods from this mixin.

    Includes a **geodesic distance cache** (Habitat pattern: per-episode
    reuse via ``_shortest_path_cache`` on Episode objects). Keyed by
    (start_node, goal_node) pair; cleared via ``clear_geodesic_cache()``.
    """

    _map: Map
    _geodesic_cache: dict  # set in subclass __init__

    def get_navigable_positions(
        self,
        node_type: Optional[str] = None,
        count: Optional[int] = None,
        rng: Optional[random.Random] = None,
    ) -> List[Position]:
        if node_type is not None:
            nodes = self._map.get_nodes_by_type(node_type)
        else:
            nodes = list(self._map.nodes)

        positions = [_node_to_position(n) for n in nodes]

        if not positions:
            raise RuntimeError(
                f"No navigable positions found"
                + (f" with node_type='{node_type}'" if node_type else "")
            )

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
        start_node = self._map.get_closest_node(Vector(start.x, start.y))
        goal_node = self._map.get_closest_node(Vector(goal.x, goal.y))

        if start_node is None or goal_node is None:
            return None
        if start_node == goal_node:
            return None

        path_nodes = self._map.get_shortest_path(start_node, goal_node)
        if path_nodes is None:
            return None

        return [_node_to_position(n) for n in path_nodes]

    def get_shortest_path_length(
        self,
        start: Position,
        goal: Position,
    ) -> Optional[float]:
        waypoints = self.get_reference_path(start, goal)
        if waypoints is None:
            return None
        return _compute_path_length(waypoints)

    def clear_geodesic_cache(self) -> None:
        """Clear the geodesic distance cache.

        Call between episodes if reusing the interface across episodes
        and you want to free memory.  The cache is keyed by graph-node
        pairs, so it remains valid as long as the graph doesn't change.
        """
        self._geodesic_cache = {}

    def get_geodesic_distance(
        self,
        start: Position,
        goal: Position,
    ) -> Optional[float]:
        # Snap positions to graph nodes for cache key
        start_node = self._map.get_closest_node(Vector(start.x, start.y))
        goal_node = self._map.get_closest_node(Vector(goal.x, goal.y))

        if start_node is None or goal_node is None:
            return None
        if start_node == goal_node:
            return start.distance_to(goal)

        # Check cache (Habitat pattern: reuse per-episode path objects)
        cache = getattr(self, "_geodesic_cache", None)
        if cache is None:
            self._geodesic_cache = {}
            cache = self._geodesic_cache

        cache_key = (id(start_node), id(goal_node))
        if cache_key in cache:
            return cache[cache_key]

        dist = self.get_shortest_path_length(start, goal)
        cache[cache_key] = dist
        return dist

    def get_world_bounds(self) -> tuple:
        """Compute (x_min, x_max, y_min, y_max) from all map nodes in cm.

        Uses the instance's sidewalk_offset (defaulting to 500 cm) as the
        margin so the returned rectangle matches the map-construction geometry.
        """
        nodes = list(self._map.nodes)
        if not nodes:
            raise RuntimeError("Map has no nodes; cannot compute world bounds.")
        xs = [n.position.x for n in nodes]
        ys = [n.position.y for n in nodes]
        buf = getattr(self, "_sidewalk_offset", 500.0)
        return (min(xs) - buf, max(xs) + buf, min(ys) - buf, max(ys) + buf)


class UnrealCVNavigationInterface(_GraphNavigationMixin, UENavigationInterface):
    """Real implementation backed by SimWorld Map + optional UnrealCV / MCP connection.

    Position sampling and A* pathfinding use the local Map graph loaded from
    roads.json — no UE process required for task generation.

    The ue_host / ue_port / mcp_port parameters are stored for future
    extensions that need in-engine navmesh queries via execute_python_script,
    but are not used in the current task-generation workflow.

    Args:
        roads_file: Path to the production roads.json.
        agent_name: UE actor name for the navigating agent (e.g.
            ``"Base_User_Agent_C_0"``).  Used to query position and
            collision via ``vget /object/{agent_name}/location`` and
            ``vbp {agent_name} GetCollisionNum``, matching the SimWorld
            communicator pattern (``simworld/communicator/unrealcv.py``).
        sidewalk_offset: Sidewalk offset in cm passed to Map.initialize_map_from_file.
        ue_host: UnrealCV host (default 127.0.0.1).
        ue_port: UnrealCV port (default 9000).
        mcp_port: MCP TCP port for execute_python_script (default 55560).
    """

    def __init__(
        self,
        roads_file: str,
        agent_name: str = "Base_User_Agent_C_0",
        sidewalk_offset: float = 500.0,
        ue_host: str = "127.0.0.1",
        ue_port: int = 9000,
        mcp_port: int = 55560,
        elements_file: Optional[str] = None,
    ) -> None:
        self._roads_file = roads_file
        self._agent_name = agent_name
        self._sidewalk_offset = sidewalk_offset
        self._ue_host = ue_host
        self._ue_port = ue_port
        self._mcp_port = mcp_port
        self._elements_file = elements_file
        self._map = _build_map(roads_file, sidewalk_offset)
        self._geodesic_cache: dict = {}
        self._ue_sock: Optional[socket.socket] = None

    def get_scene_objects(self, category=None):
        if self._elements_file is None:
            return []
        return _load_scene_objects(self._elements_file, category=category)

    def _unrealcv_request(self, cmd: str) -> str:
        """Send a command to UnrealCV and return the response string.

        Lazily opens a TCP connection on first call. The connection is
        reused for subsequent requests.

        Args:
            cmd: UnrealCV command, e.g. ``"vget /object/Agent_0/location"``.

        Returns:
            Response string from UnrealCV.

        Raises:
            ConnectionError: If the UE process is unreachable.
            RuntimeError: If the response indicates an error.
        """
        if self._ue_sock is None:
            try:
                sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                sock.settimeout(5.0)
                sock.connect((self._ue_host, self._ue_port))
                # Read UnrealCV banner (variable length, ends with newline)
                sock.recv(4096)
                self._ue_sock = sock
            except OSError as exc:
                raise ConnectionError(
                    f"Cannot connect to UnrealCV at {self._ue_host}:{self._ue_port}. "
                    "Is the UE process running with UnrealCV enabled?"
                ) from exc

        try:
            # UnrealCV text protocol: send command + newline, read response
            self._ue_sock.sendall((cmd + "\n").encode("utf-8"))
            data = self._ue_sock.recv(4096).decode("utf-8")
        except OSError as exc:
            self._ue_sock = None  # Force reconnect on next call
            raise ConnectionError(
                f"Lost connection to UnrealCV at {self._ue_host}:{self._ue_port}."
            ) from exc

        if data.startswith("error"):
            raise RuntimeError(f"UnrealCV error for '{cmd}': {data}")
        return data

    def get_agent_position(self) -> Position:
        """Query UE agent location via UnrealCV.

        Sends ``vget /object/{agent_name}/location`` and parses the
        ``x y z`` response.  The z-coordinate is discarded (2-D navigation).

        This matches the SimWorld communicator pattern
        (``simworld/communicator/unrealcv.py:get_location``).

        Returns:
            Position with (x, y) in cm and node_type from the nearest graph node.

        Raises:
            ConnectionError: If the UE process is unreachable.
        """
        response = self._unrealcv_request(
            f"vget /object/{self._agent_name}/location"
        )
        parts = response.strip().split()
        x, y = float(parts[0]), float(parts[1])
        nearest = self._map.get_closest_node(Vector(x, y))
        node_type = nearest.type if nearest else "intersection"
        return Position(x=x, y=y, node_type=node_type)

    def get_collision_counts(self) -> dict:
        """Query per-category collision counts since last reset.

        Sends ``vbp {agent_name} GetCollisionNum`` and parses the JSON
        response.  This matches the SimWorld communicator pattern
        (``simworld/communicator/unrealcv.py:get_collision_num``) and
        the local-planner collision check
        (``simworld/local_planner/local_planner.py:223``).

        Returns:
            Dict with int counts keyed by ``"HumanCollision"``,
            ``"ObjectCollision"``, ``"BuildingCollision"``,
            ``"VehicleCollision"``.

        Raises:
            ConnectionError: If the UE process is unreachable.
        """
        import json as _json

        response = self._unrealcv_request(
            f"vbp {self._agent_name} GetCollisionNum"
        )
        data = _json.loads(response)
        return {
            "HumanCollision": int(data["HumanCollision"]),
            "ObjectCollision": int(data["ObjectCollision"]),
            "BuildingCollision": int(data["BuildingCollision"]),
            "VehicleCollision": int(data["VehicleCollision"]),
        }

    def compute_view_points_with_visibility(
        self,
        object_actor_name: str,
        candidate_positions: List[Position],
        camera_id: int = 0,
        agent_height_cm: float = 160.0,
        camera_fov: float = 90.0,
        image_width: int = 320,
        image_height: int = 240,
    ) -> List[ObjectViewPoint]:
        """Compute view_points using UE renderer for true visibility.

        For each candidate graph node, places the virtual camera at that
        position, points it toward the target object, captures a
        segmentation mask (``object_mask``), and checks whether the
        object's pixels appear in the image.

        Uses SimWorld's UnrealCV commands:
        ``set_camera_location``, ``set_camera_rotation``,
        ``get_image(object_mask)``
        (``simworld/communicator/unrealcv.py``).

        Requires a **live UE session**.  For offline generation, use
        :func:`_compute_view_points` (geodesic proximity heuristic).

        Args:
            object_actor_name: UE actor name of the target object
                (e.g. ``"BP_Trash_can_C_0"``).
            candidate_positions: Graph nodes to test as view_points.
            camera_id: UnrealCV camera ID (default 0).
            agent_height_cm: Camera Z height in cm (eye level).
            camera_fov: Horizontal field of view in degrees.
            image_width: Capture resolution width.
            image_height: Capture resolution height.

        Returns:
            List of ObjectViewPoint with IOU scores, sorted by IOU
            descending (best visibility first).

        Raises:
            ConnectionError: If the UE process is unreachable.
        """
        import numpy as np

        # Get object position for look-at direction
        obj_response = self._unrealcv_request(
            f"vget /object/{object_actor_name}/location"
        )
        obj_parts = obj_response.strip().split()
        obj_x, obj_y, obj_z = float(obj_parts[0]), float(obj_parts[1]), float(obj_parts[2])

        # Set camera resolution and FOV
        self._unrealcv_request(
            f"vset /camera/{camera_id}/size {image_width} {image_height}"
        )
        self._unrealcv_request(f"vset /camera/{camera_id}/fov {camera_fov}")

        total_pixels = image_width * image_height
        results = []

        for pos in candidate_positions:
            # Place camera at graph node, agent eye height
            self._unrealcv_request(
                f"vset /camera/{camera_id}/location "
                f"{pos.x} {pos.y} {agent_height_cm}"
            )

            # Compute look-at rotation (yaw + pitch toward object)
            dx = obj_x - pos.x
            dy = obj_y - pos.y
            dz = obj_z - agent_height_cm
            import math as _math
            yaw = _math.degrees(_math.atan2(dy, dx))
            dist_xy = _math.sqrt(dx * dx + dy * dy)
            pitch = _math.degrees(_math.atan2(dz, dist_xy)) if dist_xy > 0 else 0.0

            self._unrealcv_request(
                f"vset /camera/{camera_id}/rotation {pitch} {yaw} 0"
            )

            # Capture segmentation mask
            mask_response = self._unrealcv_request(
                f"vget /camera/{camera_id}/object_mask png"
            )
            # Decode PNG bytes to numpy array
            mask_arr = np.frombuffer(mask_response, dtype=np.uint8)
            try:
                import cv2
                mask_img = cv2.imdecode(mask_arr, cv2.IMREAD_COLOR)
            except ImportError:
                from PIL import Image
                import io
                mask_img = np.array(Image.open(io.BytesIO(mask_response)))

            if mask_img is None:
                continue

            # Get target object's segmentation color
            color_response = self._unrealcv_request(
                f"vget /object/{object_actor_name}/color"
            )
            # Response format: "(R=128,G=128,B=128)" or "128 128 128"
            color_str = color_response.strip()
            if "R=" in color_str:
                import re
                m = re.findall(r"(\d+)", color_str)
                target_r, target_g, target_b = int(m[0]), int(m[1]), int(m[2])
            else:
                parts = color_str.split()
                target_r, target_g, target_b = int(parts[0]), int(parts[1]), int(parts[2])

            # Count matching pixels (allow ±2 tolerance for compression)
            if mask_img.ndim == 3 and mask_img.shape[2] >= 3:
                # BGR (cv2) or RGB (PIL) — match any channel order
                matches = (
                    (np.abs(mask_img[:, :, 0].astype(int) - target_r) <= 2)
                    & (np.abs(mask_img[:, :, 1].astype(int) - target_g) <= 2)
                    & (np.abs(mask_img[:, :, 2].astype(int) - target_b) <= 2)
                ) | (
                    (np.abs(mask_img[:, :, 0].astype(int) - target_b) <= 2)
                    & (np.abs(mask_img[:, :, 1].astype(int) - target_g) <= 2)
                    & (np.abs(mask_img[:, :, 2].astype(int) - target_r) <= 2)
                )
                object_pixels = int(np.sum(matches))
            else:
                object_pixels = 0

            if object_pixels > 0:
                iou = object_pixels / total_pixels
                results.append(ObjectViewPoint(position=pos, iou=iou))

        # Sort by IOU descending (best visibility first)
        results.sort(key=lambda vp: vp.iou if vp.iou is not None else 0, reverse=True)
        return results


class MockNavigationInterface(_GraphNavigationMixin, UENavigationInterface):
    """Offline implementation for testing — no UE process required.

    Uses the identical Map code and graph-building logic as
    UnrealCVNavigationInterface; only the roads_file path differs.
    Suitable for all unit and integration tests that do not require
    a live UE session.

    Args:
        roads_file: Path to a roads.json fixture file. Defaults to
            'tests/fixtures/mock_roads.json' relative to the task-gen root.
        sidewalk_offset: Sidewalk offset in cm (default 500.0).
    """

    def __init__(
        self,
        roads_file: str = "tests/fixtures/mock_roads.json",
        sidewalk_offset: float = 500.0,
        elements_file: Optional[str] = None,
    ) -> None:
        self._roads_file = roads_file
        self._sidewalk_offset = sidewalk_offset
        self._elements_file = elements_file
        self._map = _build_map(roads_file, sidewalk_offset)
        self._geodesic_cache: dict = {}
        self._agent_position: Optional[Position] = None
        self._mock_scene_objects: List[SceneObject] = []
        self._collision_counts: dict = {
            "HumanCollision": 0,
            "ObjectCollision": 0,
            "BuildingCollision": 0,
            "VehicleCollision": 0,
        }

    def set_agent_position(self, position: "Position") -> None:
        """Set a simulated agent position for testing reward functions."""
        self._agent_position = position

    def set_collision_counts(self, **counts: int) -> None:
        """Set simulated collision counts for testing.

        Args:
            **counts: Any of ``HumanCollision``, ``ObjectCollision``,
                ``BuildingCollision``, ``VehicleCollision``.
        """
        for key, val in counts.items():
            if key not in self._collision_counts:
                raise ValueError(f"Unknown collision category: {key}")
            self._collision_counts[key] = val

    def set_scene_objects(self, objects: List["SceneObject"]) -> None:
        """Inject mock scene objects for ObjectNav testing."""
        self._mock_scene_objects = list(objects)

    def get_scene_objects(self, category=None):
        if self._mock_scene_objects:
            objs = self._mock_scene_objects
        elif self._elements_file is not None:
            objs = _load_scene_objects(self._elements_file, category=category)
            return objs  # already filtered
        else:
            return []
        if category is not None:
            return [o for o in objs if o.category == category]
        return list(objs)

    def get_agent_position(self) -> "Position":
        if self._agent_position is None:
            raise RuntimeError(
                "MockNavigationInterface.get_agent_position() requires "
                "set_agent_position() to be called first. For offline tests, "
                "pass current_position_cm directly to reward_fn.step()."
            )
        return self._agent_position

    def get_collision_counts(self) -> dict:
        return dict(self._collision_counts)
