"""Episode validation: checks geometry, graph connectivity, and field consistency."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional

from .episode import NavigationEpisode, Position


@dataclass(frozen=True)
class ValidationResult:
    """Outcome of a single validation run.

    Attributes:
        is_valid: True iff the episode passed all checks.
        errors: Human-readable error strings; empty on success.
    """

    is_valid: bool
    errors: tuple  # tuple[str, ...] — frozen requires hashable

    @classmethod
    def ok(cls) -> ValidationResult:
        return cls(is_valid=True, errors=())

    @classmethod
    def fail(cls, errors: List[str]) -> ValidationResult:
        return cls(is_valid=False, errors=tuple(errors))


class TaskValidator:
    """Validates a NavigationEpisode against structural and geometric rules.

    Args:
        world_x_min: World X lower bound in cm.
        world_x_max: World X upper bound in cm.
        world_y_min: World Y lower bound in cm.
        world_y_max: World Y upper bound in cm.
        min_path_length_cm: Minimum acceptable graph path length.
        min_start_goal_distance_cm: Minimum straight-line start-to-goal
            separation; rejects trivially easy episodes.
    """

    def __init__(
        self,
        world_x_min: float = -9500.0,
        world_x_max: float = 9500.0,
        world_y_min: float = -9500.0,
        world_y_max: float = 9500.0,
        min_path_length_cm: float = 1000.0,
        max_path_length_cm: Optional[float] = None,
        min_start_goal_distance_cm: float = 500.0,
    ) -> None:
        self._x_min = world_x_min
        self._x_max = world_x_max
        self._y_min = world_y_min
        self._y_max = world_y_max
        self._min_path_length = min_path_length_cm
        self._max_path_length = max_path_length_cm
        self._min_straight_line = min_start_goal_distance_cm

    def validate(self, episode: NavigationEpisode) -> ValidationResult:
        """Run all validation checks on an episode.

        Checks performed (in order):
          1. start and goal positions are within world bounds.
          2. start != goal (positional equality check).
          3. reference_path is non-empty and starts/ends at start/goal.
          4. shortest_path_length_cm matches the waypoint sum (±1 cm tolerance).
          5. shortest_path_length_cm >= min_path_length_cm.
          6. Straight-line distance >= min_start_goal_distance_cm.
          7. success_distance_cm < shortest_path_length_cm (agent cannot start
             inside the goal radius).
          8. ObjectNav-specific: object_category and object_goal are present,
             view_points is non-empty, goal_position is among view_points.

        Args:
            episode: Episode to validate.

        Returns:
            ValidationResult with is_valid and any errors.
        """
        errors: List[str] = []

        start = episode.start_position
        goal = episode.goal_position
        path = episode.reference_path.waypoints
        path_len = episode.reference_path.shortest_path_length_cm
        success_r = episode.success_criteria.success_distance_cm

        # 1. Bounds
        if not self._in_bounds(start.x, start.y):
            errors.append(f"start_position ({start.x}, {start.y}) is outside world bounds")
        if not self._in_bounds(goal.x, goal.y):
            errors.append(f"goal_position ({goal.x}, {goal.y}) is outside world bounds")

        # 2. start != goal
        if abs(start.x - goal.x) < 1e-3 and abs(start.y - goal.y) < 1e-3:
            errors.append("start_position and goal_position are identical")

        # 3. reference_path non-empty and endpoints match
        if not path:
            errors.append("reference_path.waypoints is empty")
        else:
            first, last = path[0], path[-1]
            if abs(first.x - start.x) > 1.0 or abs(first.y - start.y) > 1.0:
                errors.append(
                    f"reference_path first waypoint ({first.x}, {first.y}) "
                    f"does not match start_position ({start.x}, {start.y})"
                )
            if abs(last.x - goal.x) > 1.0 or abs(last.y - goal.y) > 1.0:
                errors.append(
                    f"reference_path last waypoint ({last.x}, {last.y}) "
                    f"does not match goal_position ({goal.x}, {goal.y})"
                )

        # 4. path length consistency (±1 cm float arithmetic tolerance)
        if path and len(path) > 1:
            computed_len = sum(
                path[i].distance_to(path[i + 1]) for i in range(len(path) - 1)
            )
            if abs(computed_len - path_len) > 1.0:
                errors.append(
                    f"shortest_path_length_cm ({path_len:.4f}) does not match "
                    f"waypoint sum ({computed_len:.4f}); diff={abs(computed_len - path_len):.4f}"
                )

        # 5. minimum path length
        if path_len < self._min_path_length:
            errors.append(
                f"shortest_path_length_cm ({path_len:.1f}) < min_path_length_cm "
                f"({self._min_path_length:.1f})"
            )

        # 5b. maximum path length
        if self._max_path_length is not None and path_len > self._max_path_length:
            errors.append(
                f"shortest_path_length_cm ({path_len:.1f}) > max_path_length_cm "
                f"({self._max_path_length:.1f})"
            )

        # 6. minimum straight-line distance
        straight = self._straight_line_distance(start, goal)
        if straight < self._min_straight_line:
            errors.append(
                f"straight-line distance ({straight:.1f} cm) < "
                f"min_start_goal_distance_cm ({self._min_straight_line:.1f} cm)"
            )

        # 7. success_distance_cm < path length (agent cannot start inside goal radius)
        if success_r >= path_len:
            errors.append(
                f"success_distance_cm ({success_r:.1f}) >= shortest_path_length_cm "
                f"({path_len:.1f}); agent would start inside the goal radius"
            )

        # 8. ObjectNav-specific checks
        if episode.task_type == "objectnav":
            if episode.object_category is None:
                errors.append("objectnav episode missing object_category")
            if episode.object_goal is None:
                errors.append("objectnav episode missing object_goal")
            else:
                og = episode.object_goal
                if not og.view_points:
                    errors.append("objectnav object_goal has no view_points")
                else:
                    # goal_position must be one of the view_points
                    vp_positions = {(vp.position.x, vp.position.y) for vp in og.view_points}
                    if (goal.x, goal.y) not in vp_positions:
                        errors.append(
                            f"goal_position ({goal.x}, {goal.y}) is not among "
                            f"object_goal.view_points"
                        )

        if errors:
            return ValidationResult.fail(errors)
        return ValidationResult.ok()

    def _in_bounds(self, x: float, y: float) -> bool:
        return self._x_min <= x <= self._x_max and self._y_min <= y <= self._y_max

    def _straight_line_distance(self, start: Position, goal: Position) -> float:
        return start.distance_to(goal)
