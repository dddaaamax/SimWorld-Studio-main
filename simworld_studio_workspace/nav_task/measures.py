"""Measure plugin base class and standard Anderson et al. 2018 measures.

Measures are stateless scorers: they receive immutable episode data plus
runtime observations and return a scalar. They do NOT modify episode state.

Reference: Anderson et al. (2018), "On Evaluation of Embodied Navigation Agents"
  SPL = (1/N) * Σ_i [ S_i * l_i / max(p_i, l_i) ]
  where:
    S_i = 1 if agent succeeded on episode i, else 0
    l_i = shortest path length (stored in episode.evaluation_metrics)
    p_i = agent's actual path length (provided at runtime)
"""

from __future__ import annotations

import abc
import math
from typing import TYPE_CHECKING, Any, Optional

from .episode import NavigationEpisode, Position

if TYPE_CHECKING:
    from .interface import UENavigationInterface


class Measure(abc.ABC):
    """Base class for all evaluation measures.

    A measure is a pure function of an episode's ground-truth data and an
    agent's runtime trajectory. It is stateless between calls.
    """

    @property
    @abc.abstractmethod
    def name(self) -> str:
        """Canonical name used as the key in result dictionaries."""
        ...

    @abc.abstractmethod
    def compute(self, episode: NavigationEpisode, **runtime_kwargs: Any) -> float:
        """Compute the measure value.

        Args:
            episode: The ground-truth episode (immutable).
            **runtime_kwargs: Runtime observations; see subclass docstrings
                for required keys.

        Returns:
            Scalar measure value.
        """
        ...


class SuccessMeasure(Measure):
    """SR (Success Rate) for a single episode.

    Returns 1.0 if the agent's final position is within
    ``episode.success_criteria.success_distance_cm`` of the goal, else 0.0.

    Distance is **geodesic** (shortest navigable path) when an interface is
    provided, matching Habitat's ``Success`` measure which gates on
    ``DistanceToGoal`` (geodesic, not Euclidean).

    Args:
        interface: Optional navigation interface for geodesic distance.
            If None, falls back to Euclidean (offline-only convenience).

    Required runtime_kwargs:
        final_position_cm (tuple[float, float]): Agent's (x, y) at episode end.
    """

    def __init__(self, interface: Optional["UENavigationInterface"] = None) -> None:
        self._interface = interface

    @property
    def name(self) -> str:
        return "SR"

    def compute(
        self,
        episode: NavigationEpisode,
        final_position_cm: tuple,
        **_: Any,
    ) -> float:
        """Return 1.0 if final_position_cm is within the success radius.

        Uses geodesic distance when an interface is available (Habitat-aligned),
        falling back to Euclidean otherwise.

        Args:
            episode: Episode containing goal position and success criteria.
            final_position_cm: Agent's final (x, y) in cm.

        Returns:
            1.0 (success) or 0.0 (failure).
        """
        goal = episode.goal_position
        threshold = episode.success_criteria.success_distance_cm
        fx, fy = float(final_position_cm[0]), float(final_position_cm[1])
        if self._interface is not None:
            pos = Position(x=fx, y=fy, node_type="intersection")
            geo = self._interface.get_geodesic_distance(pos, goal)
            if geo is not None:
                return 1.0 if geo <= threshold else 0.0
        dist = math.sqrt((fx - goal.x) ** 2 + (fy - goal.y) ** 2)
        return 1.0 if dist <= threshold else 0.0


class SPLMeasure(Measure):
    """SPL (Success weighted by inverse Path Length) for a single episode.

    Formula: S_i * l_i / max(p_i, l_i)

    where:
      S_i = SuccessMeasure result (0 or 1)
      l_i = episode.evaluation_metrics.shortest_path_length_cm
      p_i = agent's actual path length in cm (sum of step distances)

    Required runtime_kwargs:
        final_position_cm (tuple[float, float]): Agent's (x, y) at episode end.
        actual_path_length_cm (float): Total distance the agent travelled in cm.
    """

    def __init__(
        self,
        success_measure: Optional[SuccessMeasure] = None,
        interface: Optional["UENavigationInterface"] = None,
    ) -> None:
        """Args:
            success_measure: Optional pre-built SuccessMeasure to reuse;
                a new one is created if not provided.
            interface: Optional navigation interface for geodesic distance,
                forwarded to the SuccessMeasure if one is created.
        """
        if success_measure is not None:
            self._success = success_measure
        else:
            self._success = SuccessMeasure(interface=interface)

    @property
    def name(self) -> str:
        return "SPL"

    def compute(
        self,
        episode: NavigationEpisode,
        final_position_cm: tuple,
        actual_path_length_cm: float,
        **_: Any,
    ) -> float:
        """Compute SPL for one episode.

        Args:
            episode: Episode containing shortest-path length and success threshold.
            final_position_cm: Agent's final (x, y) in cm.
            actual_path_length_cm: Total distance the agent moved, in cm.

        Returns:
            SPL scalar in [0, 1].
        """
        s_i = self._success.compute(episode, final_position_cm=final_position_cm)
        if s_i == 0.0:
            return 0.0

        l_i = episode.evaluation_metrics.shortest_path_length_cm
        p_i = float(actual_path_length_cm)

        if l_i <= 0.0:
            return 0.0

        return s_i * l_i / max(p_i, l_i)


class SoftSPLMeasure(Measure):
    """Soft SPL: gives partial credit based on how close the agent got.

    Unlike standard SPL (which is 0 on failure), SoftSPL replaces the
    binary success S_i with a continuous soft-success:

        soft_success = max(0, 1 − d_final / d_start)

    where d_final is geodesic distance to goal at episode end and d_start
    is geodesic distance at episode start.

    Formula: soft_success * l_i / max(p_i, l_i)

    Matches Habitat-Lab ``SoftSPL``
    (``habitat/tasks/nav/nav.py:612-656``).

    Required runtime_kwargs:
        final_position_cm (tuple[float, float]): Agent's final (x, y).
        actual_path_length_cm (float): Total distance agent moved.
    """

    def __init__(self, interface: Optional["UENavigationInterface"] = None) -> None:
        self._interface = interface

    @property
    def name(self) -> str:
        return "SoftSPL"

    def compute(
        self,
        episode: NavigationEpisode,
        final_position_cm: tuple,
        actual_path_length_cm: float,
        **_: Any,
    ) -> float:
        l_i = episode.evaluation_metrics.shortest_path_length_cm
        p_i = float(actual_path_length_cm)
        if l_i <= 0.0:
            return 0.0

        # Compute soft success: 1 − (final_distance / start_distance)
        goal = episode.goal_position
        fx, fy = float(final_position_cm[0]), float(final_position_cm[1])

        # Final distance to goal
        if self._interface is not None:
            pos = Position(x=fx, y=fy, node_type="intersection")
            d_final = self._interface.get_geodesic_distance(pos, goal)
            if d_final is None:
                d_final = math.sqrt((fx - goal.x) ** 2 + (fy - goal.y) ** 2)
        else:
            d_final = math.sqrt((fx - goal.x) ** 2 + (fy - goal.y) ** 2)

        # Start distance = shortest_path_length (geodesic from start to goal)
        d_start = l_i

        soft_success = max(0.0, 1.0 - d_final / d_start) if d_start > 0 else 0.0
        return soft_success * l_i / max(p_i, l_i)


class NDTWMeasure(Measure):
    """Normalized DTW stub — uses SoftSPL as approximation."""
    def __init__(self, interface=None) -> None:
        self._s = SoftSPLMeasure(interface=interface)
    @property
    def name(self) -> str: return "nDTW"
    def compute(self, episode, final_position_cm, actual_path_length_cm, **kw):
        return self._s.compute(episode, final_position_cm, actual_path_length_cm)


class PLRMeasure(Measure):
    """Path Length Ratio — actual / shortest."""
    @property
    def name(self) -> str: return "PLR"
    def compute(self, episode, actual_path_length_cm, **kw):
        l_i = episode.evaluation_metrics.shortest_path_length_cm
        p_i = float(actual_path_length_cm)
        return (p_i / l_i) if l_i > 0 else 0.0


class CLSMeasure(Measure):
    """Coverage weighted by Length Score stub — uses SoftSPL."""
    def __init__(self, interface=None) -> None:
        self._s = SoftSPLMeasure(interface=interface)
    @property
    def name(self) -> str: return "CLS"
    def compute(self, episode, final_position_cm, actual_path_length_cm, **kw):
        return self._s.compute(episode, final_position_cm, actual_path_length_cm)
