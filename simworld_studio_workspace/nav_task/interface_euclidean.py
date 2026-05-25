"""Graph-free navigation interface for dynamically spawned scenes.

When scenes are built ad-hoc by a coding agent (no roads.json / no
SimWorld Map graph), geodesic distance degenerates to Euclidean.
Reward shaping, SR, SPL, and SoftSPL all remain well-defined — the
only loss is obstacle-aware shortest-path modelling.

This module intentionally does NOT import nav_task.interface (which
triggers the SimWorld Map / PyQt5 dependency chain).  It re-derives
from the ABC defined there by duck-typing the same protocol.
"""

from __future__ import annotations

import math
from typing import TYPE_CHECKING, Dict, List, Optional, Tuple

from .episode import Position

if TYPE_CHECKING:
    pass  # avoid circular / heavy imports


class EuclideanNavigationInterface:
    """Navigation interface backed purely by Euclidean distance.

    Designed for scenes without a pre-built road graph.  Accepts an
    external ``ucv_send`` callable so the owning env can share a single
    UnrealCV TCP connection.

    Parameters
    ----------
    ucv_send : callable(str) -> str
        Function that sends an UnrealCV command string and returns the
        response.  Typically ``UCVClient.send``.
    agent_name : str
        UE actor name for ``vget /object/{agent_name}/location``.
    """

    def __init__(
        self,
        ucv_send,
        agent_name: str = "Base_User_Agent_C_0",
    ) -> None:
        self._ucv = ucv_send
        self._agent_name = agent_name

    # -- position ----------------------------------------------------------

    def get_agent_position(self) -> Position:
        resp = self._ucv(f"vget /object/{self._agent_name}/location")
        parts = resp.strip().split()
        # UnrealCV returns strings like "error Can not find object ..."
        # when the actor has been destroyed or the server is in a bad
        # state (e.g. after a PIE crash or mid-cycle).  Raise a clean
        # RuntimeError here so the batch runner treats it as step_error
        # for this ghost instead of crashing with ValueError on float().
        if len(parts) < 2 or parts[0].lower() == "error":
            raise RuntimeError(
                f"UCV position query failed for {self._agent_name!r}: {resp!r}"
            )
        try:
            x, y = float(parts[0]), float(parts[1])
        except ValueError as exc:
            raise RuntimeError(
                f"UCV returned non-numeric position for "
                f"{self._agent_name!r}: {resp!r}"
            ) from exc
        return Position(x=x, y=y, node_type="intersection")

    # -- distance ----------------------------------------------------------

    def get_geodesic_distance(
        self, start: Position, goal: Position,
    ) -> Optional[float]:
        return _euclidean(start, goal)

    def get_shortest_path_length(
        self, start: Position, goal: Position,
    ) -> Optional[float]:
        return _euclidean(start, goal)

    # -- path (degenerate) -------------------------------------------------

    def get_reference_path(
        self, start: Position, goal: Position,
    ) -> Optional[List[Position]]:
        return [start, goal]

    # -- world bounds (permissive default) ---------------------------------

    def get_world_bounds(self) -> Tuple[float, float, float, float]:
        return (-1_000_000, 1_000_000, -1_000_000, 1_000_000)

    # -- navigable positions (not applicable) ------------------------------

    def get_navigable_positions(self, **_kw) -> List[Position]:
        return []

    # -- collisions --------------------------------------------------------

    def get_collision_counts(self) -> Dict[str, int]:
        """Try ``vbp GetCollisionNum``; return zeros on failure."""
        try:
            import json
            resp = self._ucv(f"vbp {self._agent_name} GetCollisionNum")
            data = json.loads(resp)
            return {
                "HumanCollision": int(data.get("HumanCollision", 0)),
                "ObjectCollision": int(data.get("ObjectCollision", 0)),
                "BuildingCollision": int(data.get("BuildingCollision", 0)),
                "VehicleCollision": int(data.get("VehicleCollision", 0)),
            }
        except Exception:
            return {
                "HumanCollision": 0, "ObjectCollision": 0,
                "BuildingCollision": 0, "VehicleCollision": 0,
            }


def _euclidean(a: Position, b: Position) -> float:
    return math.sqrt((a.x - b.x) ** 2 + (a.y - b.y) ** 2)
