"""nav_task — Navigation task generator for Unreal Engine embodied-AI simulation."""

from .episode import (
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
from .interface_euclidean import EuclideanNavigationInterface
from .measures import Measure, SPLMeasure, SoftSPLMeasure, SuccessMeasure
from .registry import MeasureRegistry
from .reward import DistanceToGoalReward, NavigationReward, RewardFunction, SuccessBonusReward
from .task_spec import (
    NAVIGATION_ACTIONS,
    OBJECTNAV_SPEC,
    POINTNAV_SPEC,
    TASK_SPECS,
    TaskSpec,
    compute_objectgoal_id,
    compute_pointgoal,
    get_task_spec,
    make_task_prompt,
)
from .scene_graph_interface import SceneGraphNavigationInterface
from .navmesh_interface import NavmeshNavigationInterface
from .validator import TaskValidator, ValidationResult


def _load_simworld_interfaces():
    """Lazy-load interfaces that depend on SimWorld Map / PyQt5.

    Call this only when you actually need ``UnrealCVNavigationInterface``
    or ``MockNavigationInterface`` (i.e. when a ``roads.json`` graph is
    available and SimWorld is on sys.path).
    """
    from .interface import MockNavigationInterface, SceneObject, UENavigationInterface, UnrealCVNavigationInterface
    return {
        "UENavigationInterface": UENavigationInterface,
        "UnrealCVNavigationInterface": UnrealCVNavigationInterface,
        "MockNavigationInterface": MockNavigationInterface,
        "SceneObject": SceneObject,
    }


# Lazy accessor kept for backwards compat with existing task_gen tests
def __getattr__(name):
    _lazy = {
        "UENavigationInterface", "UnrealCVNavigationInterface",
        "MockNavigationInterface", "SceneObject",
    }
    if name in _lazy:
        return _load_simworld_interfaces()[name]
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = [
    "Position",
    "ReferencePath",
    "SuccessCriteria",
    "EvaluationMetrics",
    "WorldConfig",
    "RewardConfig",
    "ObjectViewPoint",
    "ObjectGoal",
    "NavigationEpisode",
    "EuclideanNavigationInterface",
    "NavigationTaskGenerator",
    "TaskValidator",
    "ValidationResult",
    "Measure",
    "SuccessMeasure",
    "SPLMeasure",
    "SoftSPLMeasure",
    "MeasureRegistry",
    "RewardFunction",
    "DistanceToGoalReward",
    "SuccessBonusReward",
    "NavigationReward",
    "SceneGraphNavigationInterface",
    "NavmeshNavigationInterface",
]
