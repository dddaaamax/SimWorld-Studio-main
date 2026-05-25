"""Scene-graph queries used by the ObjectNav search task generator.

Given the ``test_map_scene_graph.json`` dump produced by
``test_task_generation.py``, this module provides:

* :func:`load_scene_graph` — parse the JSON into a normalised list.
* :func:`nearby_actors` — return actors within a radius around a point.
* :func:`actor_category` — rough category inference from actor name.

The intent is to keep the describer LLM well-informed about the
local context of a target without hard-coding "landmark" detection
logic in Python — the LLM is free to pick whichever nearby actors
read most naturally in its description.
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Tuple


@dataclass(frozen=True)
class SceneActor:
    """One entry from the scene graph, normalised for the describer."""

    name: str             # raw actor name from UE (e.g. BP_Building_17_C_UAID_...)
    category: str         # rough category: "building" / "tree" / "prop" / ...
    x: float              # cm
    y: float              # cm
    width: float = 200.0  # cm (best-effort from scene_graph "size" field)
    height: float = 200.0

    @property
    def position(self) -> Tuple[float, float]:
        return (self.x, self.y)


# ── Name → category heuristics ───────────────────────────────────────
#
# The project's blueprints follow the ``BP_<Kind>_<number>_C`` naming
# convention.  We strip the ``BP_`` prefix and the trailing ``_C_...``
# runtime suffix, then match a small set of prefixes.

_BUILDING_PREFIXES = ("Building", "Tent", "House", "Warehouse")
_TREE_PREFIXES = ("Tree",)
_VEHICLE_PREFIXES = ("Scooter", "Car", "Vehicle", "Truck", "Bus")
_SMALL_PREFIXES = (
    "Hydrant", "Trash", "Can", "Soda", "Box", "Rabbish",
    "Cart", "Couch", "Table", "RoadCone", "RoadBlocker",
)
_ROAD_PREFIXES = ("Road",)


def actor_category(name: str) -> str:
    """Infer a coarse category from an actor name.

    Unknown → "prop" so it can still be referenced by the describer
    without being completely skipped.
    """
    cleaned = name
    if cleaned.startswith("BP_"):
        cleaned = cleaned[3:]
    # Strip the trailing ``_C_UAID_*`` UE runtime suffix if present.
    cleaned = re.sub(r"_C(_UAID_[0-9A-F_]+)?$", "", cleaned)
    cleaned = re.sub(r"_[0-9]+$", "", cleaned)  # drop instance index

    for p in _BUILDING_PREFIXES:
        if cleaned.startswith(p):
            return "building"
    for p in _TREE_PREFIXES:
        if cleaned.startswith(p):
            return "tree"
    for p in _VEHICLE_PREFIXES:
        if cleaned.startswith(p):
            return "vehicle"
    for p in _ROAD_PREFIXES:
        if cleaned.startswith(p):
            return "road"
    for p in _SMALL_PREFIXES:
        if cleaned.startswith(p):
            return "prop"
    return "prop"


def load_scene_graph(path: str) -> List[SceneActor]:
    """Parse ``test_map_scene_graph.json`` into a list of SceneActor."""
    with open(path) as f:
        data = json.load(f)
    items = data if isinstance(data, list) else data.get("elements", [])
    out: List[SceneActor] = []
    for item in items:
        name = item.get("name", "")
        if not name:
            continue
        center = item.get("center", {})
        size = item.get("size", {})
        out.append(
            SceneActor(
                name=name,
                category=actor_category(name),
                x=float(center.get("x", 0.0)),
                y=float(center.get("y", 0.0)),
                width=float(size.get("width", 200.0)),
                height=float(size.get("height", 200.0)),
            )
        )
    return out


def _dist2(ax: float, ay: float, bx: float, by: float) -> float:
    dx = ax - bx
    dy = ay - by
    return dx * dx + dy * dy


def nearby_actors(
    scene_graph: List[SceneActor],
    x: float,
    y: float,
    radius_cm: float = 5000.0,
    top_k: Optional[int] = None,
    categories: Optional[tuple] = None,
) -> List[SceneActor]:
    """Return actors within ``radius_cm`` of ``(x, y)``, nearest first.

    Args:
        scene_graph: output of :func:`load_scene_graph`.
        x, y: query point in cm.
        radius_cm: include actors whose centre is within this distance.
        top_k: if given, cap the result to the K nearest actors.
        categories: if given, filter to actors whose category is in
            this tuple (e.g. ``("building", "tree")``).
    """
    r2 = radius_cm * radius_cm
    scored: List[Tuple[float, SceneActor]] = []
    for a in scene_graph:
        if categories is not None and a.category not in categories:
            continue
        d2 = _dist2(a.x, a.y, x, y)
        if d2 <= r2:
            scored.append((d2, a))
    scored.sort(key=lambda t: t[0])
    if top_k is not None:
        scored = scored[:top_k]
    return [a for _, a in scored]


def relative_heading(
    from_x: float,
    from_y: float,
    to_x: float,
    to_y: float,
) -> Tuple[str, float]:
    """Return (compass_direction, distance_cm) from one point to another.

    Compass is one of: east / northeast / north / northwest / west /
    southwest / south / southeast.  Uses math convention: X+ = east,
    Y+ = north.  (The caller can flip if the UE map uses different axes.)
    """
    dx = to_x - from_x
    dy = to_y - from_y
    dist = math.sqrt(dx * dx + dy * dy)
    angle = math.degrees(math.atan2(dy, dx))
    if angle < 0:
        angle += 360.0
    labels = [
        (22.5,  "east"),
        (67.5,  "northeast"),
        (112.5, "north"),
        (157.5, "northwest"),
        (202.5, "west"),
        (247.5, "southwest"),
        (292.5, "south"),
        (337.5, "southeast"),
    ]
    for upper, name in labels:
        if angle < upper:
            return name, dist
    return "east", dist
