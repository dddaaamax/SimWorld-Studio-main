"""Immutable dataclasses representing a single navigation episode.

All fields required to reproduce, evaluate, and replay an episode are
present here. No references to live objects. JSON-round-trippable.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import List, Literal, Optional

NodeType = Literal["sidewalk", "crosswalk", "intersection"]

GENERATOR_VERSION = "0.1.0"
SCHEMA_VERSION = "1.2.0"


@dataclass(frozen=True)
class Position:
    """A 2-D position in Unreal Engine centimetre space.

    Attributes:
        x: X coordinate in cm.
        y: Y coordinate in cm.
        node_type: Graph node category at this position.
    """

    x: float
    y: float
    node_type: NodeType

    def distance_to(self, other: Position) -> float:
        """Euclidean distance to another position in cm."""
        return math.sqrt((self.x - other.x) ** 2 + (self.y - other.y) ** 2)

    def to_dict(self) -> dict:
        """Serialize to a plain dictionary."""
        return {"x": self.x, "y": self.y, "node_type": self.node_type}

    @classmethod
    def from_dict(cls, d: dict) -> Position:
        """Deserialize from a plain dictionary."""
        return cls(x=float(d["x"]), y=float(d["y"]), node_type=d["node_type"])


@dataclass(frozen=True)
class ReferencePath:
    """The A*-computed reference path between start and goal.

    Attributes:
        waypoints: Ordered list of positions from start to goal (inclusive).
        shortest_path_length_cm: Sum of Euclidean inter-waypoint distances in cm.
    """

    waypoints: tuple  # tuple[Position, ...] — frozen requires hashable
    shortest_path_length_cm: float

    def to_dict(self) -> dict:
        return {
            "waypoints": [wp.to_dict() for wp in self.waypoints],
            "shortest_path_length_cm": self.shortest_path_length_cm,
        }

    @classmethod
    def from_dict(cls, d: dict) -> ReferencePath:
        return cls(
            waypoints=tuple(Position.from_dict(wp) for wp in d["waypoints"]),
            shortest_path_length_cm=float(d["shortest_path_length_cm"]),
        )


@dataclass(frozen=True)
class SuccessCriteria:
    """Episode termination thresholds.

    Attributes:
        success_distance_cm: Goal-radius; agent succeeds when geodesic
            distance to goal ≤ this value.

            **Calibration reference:** Habitat indoor PointNav uses 0.2 m
            (20 cm), derived from the LoCoBot robot radius (Anderson et al.
            2018).  SimWorld's ``waypoint_distance_threshold`` (150–200 cm)
            is for NPC path-following smoothness — a *different* concept
            and too loose for success evaluation.

            Default 100 cm (1 m) is a reasonable outdoor city-scale
            threshold: roughly one agent-body width, tighter than the
            NPC waypoint threshold but looser than Habitat indoor to
            account for outdoor positioning imprecision.  Adjust via
            the generator's ``success_distance_cm`` parameter.
        max_steps: Hard step cap before forced failure.
        max_episode_time_s: Wall-clock cap in seconds.
    """

    success_distance_cm: float = 100.0
    max_steps: int = 5000
    max_episode_time_s: float = 300.0

    def to_dict(self) -> dict:
        return {
            "success_distance_cm": self.success_distance_cm,
            "max_steps": self.max_steps,
            "max_episode_time_s": self.max_episode_time_s,
        }

    @classmethod
    def from_dict(cls, d: dict) -> SuccessCriteria:
        return cls(
            success_distance_cm=float(d["success_distance_cm"]),
            max_steps=int(d["max_steps"]),
            max_episode_time_s=float(d["max_episode_time_s"]),
        )


@dataclass(frozen=True)
class EvaluationMetrics:
    """Evaluation metric configuration (Anderson et al. 2018).

    Attributes:
        success_distance_cm: Threshold used for SR computation (= SuccessCriteria.success_distance_cm).
        shortest_path_length_cm: l_i — the denominator term in SPL.
    """

    success_distance_cm: float
    shortest_path_length_cm: float

    def to_dict(self) -> dict:
        return {
            "type": "Anderson2018",
            "SR": {
                "description": "1 if agent reaches goal within success_distance_cm, else 0",
                "success_distance_cm": self.success_distance_cm,
            },
            "SPL": {
                "description": "S_i * l_i / max(p_i, l_i); l_i stored here for offline scoring",
                "shortest_path_length_cm": self.shortest_path_length_cm,
                "formula": "S_i * l_i / max(p_i, l_i)",
            },
        }

    @classmethod
    def from_dict(cls, d: dict) -> EvaluationMetrics:
        return cls(
            success_distance_cm=float(d["SR"]["success_distance_cm"]),
            shortest_path_length_cm=float(d["SPL"]["shortest_path_length_cm"]),
        )


@dataclass(frozen=True)
class WorldConfig:
    """World coordinate metadata.

    Attributes:
        map_file: Path to roads.json used to build the graph.
        coordinate_unit: Always 'cm' in SimWorld.
        x_min / x_max / y_min / y_max: World bounds in cm.
    """

    map_file: str
    coordinate_unit: str = "cm"
    x_min: float = -9500.0
    x_max: float = 9500.0
    y_min: float = -9500.0
    y_max: float = 9500.0

    def to_dict(self) -> dict:
        return {
            "map_file": self.map_file,
            "coordinate_unit": self.coordinate_unit,
            "bounds": {
                "x_min": self.x_min,
                "x_max": self.x_max,
                "y_min": self.y_min,
                "y_max": self.y_max,
            },
        }

    @classmethod
    def from_dict(cls, d: dict) -> WorldConfig:
        b = d.get("bounds", {})
        return cls(
            map_file=d["map_file"],
            coordinate_unit=d.get("coordinate_unit", "cm"),
            x_min=float(b.get("x_min", -9500.0)),
            x_max=float(b.get("x_max", 9500.0)),
            y_min=float(b.get("y_min", -9500.0)),
            y_max=float(b.get("y_max", 9500.0)),
        )


@dataclass(frozen=True)
class RewardConfig:
    """Reward hyperparameters for reproducible training runs.

    Persisted in the episode JSON so that any training run can reconstruct
    the exact reward function used during generation.

    Distance-to-goal shaping and success proximity always use **geodesic**
    (shortest navigable path length via the same graph / UE nav as
    :meth:`UENavigationInterface.get_geodesic_distance`), matching
    Habitat-Lab PointNav's ``DistanceToGoal`` measure — not Euclidean
    straight-line distance.

    Attributes:
        step_cost: Per-step penalty (Habitat ``slack_reward`` magnitude).
        success_bonus: Sparse bonus (Habitat ``success_reward``).
        require_stop_for_success_bonus: If True, match Habitat ``Success``:
            bonus only when geodesic distance < threshold **and** the agent
            has signaled stop (call ``SuccessBonusReward.on_stop_action()``).
    """

    step_cost: float = 0.01
    success_bonus: float = 2.5
    require_stop_for_success_bonus: bool = False

    def to_dict(self) -> dict:
        return {
            "step_cost": self.step_cost,
            "success_bonus": self.success_bonus,
            "require_stop_for_success_bonus": self.require_stop_for_success_bonus,
        }

    @classmethod
    def from_dict(cls, d: dict) -> RewardConfig:
        # Legacy JSON may contain distance_mode; ignored (always geodesic now).
        return cls(
            step_cost=float(d.get("step_cost", 0.01)),
            success_bonus=float(d.get("success_bonus", 2.5)),
            require_stop_for_success_bonus=bool(
                d.get("require_stop_for_success_bonus", False)
            ),
        )


@dataclass(frozen=True)
class ObjectViewPoint:
    """A navigable position from which a target object is visible.

    Mirrors Habitat-Lab's ``ObjectViewLocation``
    (``habitat/tasks/nav/object_nav_task.py``).

    Attributes:
        position: Navigable graph node position in cm.
        iou: Intersection-over-union visibility quality (0–1).
            None when visibility has not been computed (offline generation).
    """

    position: Position
    iou: Optional[float] = None

    def to_dict(self) -> dict:
        d: dict = {"position": self.position.to_dict()}
        if self.iou is not None:
            d["iou"] = self.iou
        return d

    @classmethod
    def from_dict(cls, d: dict) -> ObjectViewPoint:
        return cls(
            position=Position.from_dict(d["position"]),
            iou=d.get("iou"),
        )


@dataclass(frozen=True)
class ObjectGoal:
    """An object instance serving as an ObjectNav target.

    Mirrors Habitat-Lab's ``ObjectGoal``
    (``habitat/tasks/nav/object_nav_task.py``).

    Attributes:
        object_id: Unique instance identifier, e.g. ``"BP_Trash_can_C_003"``.
        object_type: UE blueprint class, e.g. ``"BP_Trash_can_C"``.
        object_category: Semantic category from ``ue_assets.json``,
            e.g. ``"TRASH"``.  Fixed across all SimWorld maps.
        position: Object center in cm.
        view_points: Navigable positions around the object from which it is
            visible / reachable.  Success is measured to the nearest
            view_point (``distance_to: VIEW_POINTS`` in Habitat).
    """

    object_id: str
    object_type: str
    object_category: str
    position: Position
    view_points: tuple = ()  # tuple[ObjectViewPoint, ...]

    def to_dict(self) -> dict:
        return {
            "object_id": self.object_id,
            "object_type": self.object_type,
            "object_category": self.object_category,
            "position": self.position.to_dict(),
            "view_points": [vp.to_dict() for vp in self.view_points],
        }

    @classmethod
    def from_dict(cls, d: dict) -> ObjectGoal:
        return cls(
            object_id=d["object_id"],
            object_type=d["object_type"],
            object_category=d["object_category"],
            position=Position.from_dict(d["position"]),
            view_points=tuple(
                ObjectViewPoint.from_dict(vp) for vp in d.get("view_points", [])
            ),
        )


@dataclass(frozen=True)
class NavigationEpisode:
    """A fully self-contained, immutable navigation task episode.

    Supports both **PointNav** (``task_type="pointnav"``) and **ObjectNav**
    (``task_type="objectnav"``).  For ObjectNav, ``goal_position`` is set to
    the nearest reachable view_point of the target object (Habitat's
    ``distance_to: VIEW_POINTS`` pattern), and ``object_goal`` carries full
    object metadata.

    All fields needed to reproduce, evaluate, and replay the episode are
    present here. No references to live objects. JSON-round-trippable.

    Attributes:
        episode_id: Unique string identifier (format: nav_ep_{seed}_{index:03d}).
        seed: RNG seed; single source of truth for all random choices made during generation.
        world: Coordinate-space metadata.
        start_position: Agent spawn position.
        goal_position: Target position.
        reference_path: A* reference solution.
        success_criteria: Episode-termination thresholds.
        evaluation_metrics: Offline metric parameters (SR / SPL per Anderson 2018).
        reward_config: Reward hyperparameters for reproducible training.
        generated_at: ISO-8601 UTC timestamp of generation.
        schema_version: Monotonic version of this dataclass layout.
        generator_version: Version of the task-gen tool that produced this episode.
    """

    episode_id: str
    seed: int
    world: WorldConfig
    start_position: Position
    goal_position: Position
    reference_path: ReferencePath
    success_criteria: SuccessCriteria
    evaluation_metrics: EvaluationMetrics
    generated_at: str
    reward_config: RewardConfig = field(default_factory=RewardConfig)
    task_type: str = "pointnav"
    object_category: Optional[str] = None
    object_goal: Optional[ObjectGoal] = None
    schema_version: str = SCHEMA_VERSION
    generator_version: str = GENERATOR_VERSION

    def to_dict(self) -> dict:
        """Serialize episode to a JSON-compatible dictionary."""
        d = {
            "schema_version": self.schema_version,
            "tool": "generate_navigation_task",
            "generated_at": self.generated_at,
            "seed": self.seed,
            "episode_id": self.episode_id,
            "task_type": self.task_type,
            "world": self.world.to_dict(),
            "start_position": self.start_position.to_dict(),
            "goal_position": self.goal_position.to_dict(),
            "reference_path": self.reference_path.to_dict(),
            "success_criteria": self.success_criteria.to_dict(),
            "evaluation_metrics": self.evaluation_metrics.to_dict(),
            "reward_config": self.reward_config.to_dict(),
            "metadata": self._build_metadata(),
        }
        if self.object_category is not None:
            d["object_category"] = self.object_category
        if self.object_goal is not None:
            d["object_goal"] = self.object_goal.to_dict()
        return d

    def _build_metadata(self) -> dict:
        straight_line = self.start_position.distance_to(self.goal_position)
        waypoints = self.reference_path.waypoints
        node_types = {wp.node_type for wp in waypoints}
        d = {
            "task_type": self.task_type,
            "geodesic_distance_cm": self.reference_path.shortest_path_length_cm,
            "straight_line_distance_cm": round(straight_line, 4),
            "path_node_count": len(waypoints),
            "start_node_type": self.start_position.node_type,
            "goal_node_type": self.goal_position.node_type,
            "contains_crosswalk": "crosswalk" in node_types,
            "contains_intersection": "intersection" in node_types,
            "generator_version": self.generator_version,
        }
        if self.object_category is not None:
            d["object_category"] = self.object_category
        return d

    def to_json(self, indent: int = 2) -> str:
        """Serialize episode to a formatted JSON string."""
        return json.dumps(self.to_dict(), indent=indent)

    @classmethod
    def from_dict(cls, d: dict) -> NavigationEpisode:
        """Deserialize episode from a dictionary produced by to_dict()."""
        og_raw = d.get("object_goal")
        return cls(
            episode_id=d["episode_id"],
            seed=int(d["seed"]),
            world=WorldConfig.from_dict(d["world"]),
            start_position=Position.from_dict(d["start_position"]),
            goal_position=Position.from_dict(d["goal_position"]),
            reference_path=ReferencePath.from_dict(d["reference_path"]),
            success_criteria=SuccessCriteria.from_dict(d["success_criteria"]),
            evaluation_metrics=EvaluationMetrics.from_dict(d["evaluation_metrics"]),
            generated_at=d["generated_at"],
            reward_config=RewardConfig.from_dict(d.get("reward_config", {})),
            task_type=d.get("task_type", "pointnav"),
            object_category=d.get("object_category"),
            object_goal=ObjectGoal.from_dict(og_raw) if og_raw else None,
            schema_version=d.get("schema_version", SCHEMA_VERSION),
            generator_version=d.get("metadata", {}).get("generator_version", GENERATOR_VERSION),
        )

    @classmethod
    def from_json(cls, s: str) -> NavigationEpisode:
        """Deserialize episode from a JSON string."""
        return cls.from_dict(json.loads(s))
