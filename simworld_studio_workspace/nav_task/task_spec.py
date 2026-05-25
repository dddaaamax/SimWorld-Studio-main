"""Task specification: prompts, observations, and actions for navigation tasks.

Bridges episode configs (what we generate) with the agent interface (what the
agent sees and does).  Each task type defines:

  - **Prompt template**: natural-language instruction for the agent
  - **Observation spec**: which sensors the agent receives each step
  - **Action space**: discrete actions mapped to SimWorld commands

Sensor observations are fetched from a live UE session via UnrealCV
(``simworld/communicator/unrealcv.py``) or the MCP agent tools
(``SimWorld-Studio-Dev/web/server/mcp-server.js``).

Reference:
  - Habitat PointNav: ``habitat/config/habitat/task/pointnav.yaml``
  - Habitat ObjectNav: ``habitat/config/habitat/task/objectnav.yaml``
  - AllenAct iTHOR: ``allenact_plugins/ithor_plugin/ithor_tasks.py``
  - SimWorld agent registry: ``SimWorld-Studio-Dev/web/server/agent-registry.json``
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from .episode import NavigationEpisode, Position


# ── Action Space ─────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Action:
    """A discrete agent action mapped to a SimWorld UE command.

    Attributes:
        name: Canonical action name (Habitat convention).
        simworld_cmd: UnrealCV blueprint command template.
            ``{agent}`` is replaced with the agent actor name at runtime.
        description: Human-readable description.
    """

    name: str
    simworld_cmd: str
    description: str


# Standard PointNav / ObjectNav action set (Habitat-aligned)
NAVIGATION_ACTIONS: Tuple[Action, ...] = (
    Action(
        name="MOVE_FORWARD",
        simworld_cmd="vbp {agent} StepForward 2 0",
        description="Walk forward for 2 seconds",
    ),
    Action(
        name="TURN_LEFT",
        simworld_cmd="vbp {agent} TurnAround 1 30 left",
        description="Turn left 30 degrees",
    ),
    Action(
        name="TURN_RIGHT",
        simworld_cmd="vbp {agent} TurnAround 1 30 right",
        description="Turn right 30 degrees",
    ),
    Action(
        name="STOP",
        simworld_cmd="vbp {agent} StopAgent",
        description="Signal that the agent has reached the goal",
    ),
)


# ── Observation Spec ─────────────────────────────────────────────────────────

@dataclass(frozen=True)
class SensorSpec:
    """Specification for a single sensor observation.

    Attributes:
        name: Sensor name (Habitat convention).
        unrealcv_cmd: UnrealCV command to fetch this observation.
            ``{agent}`` / ``{camera}`` replaced at runtime.
        dtype: Expected data type (``"image"``, ``"vector"``, ``"int"``).
        shape: Expected shape (e.g. ``(240, 320, 3)`` for RGB).
        description: Human-readable description.
    """

    name: str
    unrealcv_cmd: str
    dtype: str
    shape: Optional[tuple] = None
    description: str = ""


# Sensors available for navigation tasks
RGB_SENSOR = SensorSpec(
    name="rgb",
    unrealcv_cmd="vget /camera/{camera}/lit png",
    dtype="image",
    shape=(240, 320, 3),
    description="First-person RGB image",
)

DEPTH_SENSOR = SensorSpec(
    name="depth",
    unrealcv_cmd="vget /camera/{camera}/depth npy",
    dtype="image",
    shape=(240, 320, 1),
    description="Depth map",
)

GPS_SENSOR = SensorSpec(
    name="gps",
    unrealcv_cmd="vget /object/{agent}/location",
    dtype="vector",
    shape=(2,),
    description="Agent (x, y) displacement relative to start position",
)

COMPASS_SENSOR = SensorSpec(
    name="compass",
    unrealcv_cmd="vget /object/{agent}/rotation",
    dtype="vector",
    shape=(1,),
    description="Agent heading angle relative to goal direction",
)

POINTGOAL_SENSOR = SensorSpec(
    name="pointgoal_with_gps_compass",
    unrealcv_cmd="",  # Computed from GPS + goal, not a direct UE query
    dtype="vector",
    shape=(2,),
    description="Goal in polar coordinates (distance, angle) relative to agent",
)

OBJECTGOAL_SENSOR = SensorSpec(
    name="objectgoal",
    unrealcv_cmd="",  # Derived from episode.object_category → integer ID
    dtype="int",
    shape=(1,),
    description="Target object category ID (integer)",
)


# ── Task Specification ───────────────────────────────────────────────────────

@dataclass(frozen=True)
class TaskSpec:
    """Complete task specification: prompt + observations + actions.

    Attributes:
        task_type: ``"pointnav"`` or ``"objectnav"``.
        prompt_template: Natural-language instruction template.
            Placeholders: ``{object_category}``, ``{goal_distance}``,
            ``{goal_angle}``.
        sensors: Ordered list of sensor specs the agent receives.
        actions: Discrete action space.
    """

    task_type: str
    prompt_template: str
    sensors: Tuple[SensorSpec, ...]
    actions: Tuple[Action, ...] = NAVIGATION_ACTIONS


POINTNAV_SPEC = TaskSpec(
    task_type="pointnav",
    prompt_template="Navigate to the target location.",
    sensors=(
        RGB_SENSOR,
        DEPTH_SENSOR,
        GPS_SENSOR,
        COMPASS_SENSOR,
        POINTGOAL_SENSOR,
    ),
)

OBJECTNAV_SPEC = TaskSpec(
    task_type="objectnav",
    prompt_template="Find and navigate to a {object_category}.",
    sensors=(
        RGB_SENSOR,
        DEPTH_SENSOR,
        GPS_SENSOR,
        COMPASS_SENSOR,
        OBJECTGOAL_SENSOR,
    ),
)

TASK_SPECS: Dict[str, TaskSpec] = {
    "pointnav": POINTNAV_SPEC,
    "objectnav": OBJECTNAV_SPEC,
}


# ── Prompt Generation ────────────────────────────────────────────────────────

def make_task_prompt(episode: NavigationEpisode) -> str:
    """Generate the task prompt for an episode.

    Args:
        episode: A NavigationEpisode (PointNav or ObjectNav).

    Returns:
        Natural-language task instruction string.

    Examples:
        >>> make_task_prompt(pointnav_episode)
        'Navigate to the target location.'
        >>> make_task_prompt(objectnav_episode)
        'Find and navigate to a TRASH.'
    """
    spec = TASK_SPECS.get(episode.task_type)
    if spec is None:
        raise ValueError(f"Unknown task_type: {episode.task_type}")
    return spec.prompt_template.format(
        object_category=episode.object_category or "",
    )


def get_task_spec(episode: NavigationEpisode) -> TaskSpec:
    """Return the TaskSpec for an episode's task type."""
    spec = TASK_SPECS.get(episode.task_type)
    if spec is None:
        raise ValueError(f"Unknown task_type: {episode.task_type}")
    return spec


# ── Observation Helpers ──────────────────────────────────────────────────────

def compute_pointgoal(
    agent_x: float,
    agent_y: float,
    agent_yaw_deg: float,
    goal: Position,
) -> Tuple[float, float]:
    """Compute goal in agent-relative polar coordinates.

    Matches Habitat's ``PointGoalWithGPSCompassSensor``: returns
    ``(distance, angle)`` where angle is relative to agent heading.

    Args:
        agent_x: Agent x position in cm.
        agent_y: Agent y position in cm.
        agent_yaw_deg: Agent heading in degrees (0 = east, CCW positive).
        goal: Goal position.

    Returns:
        ``(distance_cm, relative_angle_rad)`` — distance in cm,
        angle in radians (positive = goal is to the left).
    """
    dx = goal.x - agent_x
    dy = goal.y - agent_y
    distance = math.sqrt(dx * dx + dy * dy)
    goal_angle = math.atan2(dy, dx)
    agent_heading = math.radians(agent_yaw_deg)
    relative_angle = goal_angle - agent_heading
    # Normalize to [-pi, pi]
    relative_angle = (relative_angle + math.pi) % (2 * math.pi) - math.pi
    return (distance, relative_angle)


def compute_objectgoal_id(
    object_category: str,
    all_categories: Optional[List[str]] = None,
) -> int:
    """Map object category string to integer ID.

    Uses sorted category list for deterministic mapping, matching
    Habitat's ``category_to_task_category_id`` pattern.

    Args:
        object_category: Category string (e.g. ``"TRASH"``).
        all_categories: Full sorted category vocabulary. If None, uses
            the default SimWorld categories from ``ue_assets.json``.

    Returns:
        Integer category ID.
    """
    if all_categories is None:
        all_categories = _DEFAULT_CATEGORIES
    return all_categories.index(object_category)


# Default SimWorld categories (sorted, from ue_assets.json "colors" keys)
_DEFAULT_CATEGORIES: List[str] = sorted([
    "BUILDING",
    "BOX",
    "CAN",
    "FURNITURE",
    "ROAD_BLOCKER",
    "SCOOTER",
    "STREET_FURNITURE",
    "TABLE",
    "TRASH",
    "VEGETATION",
])
