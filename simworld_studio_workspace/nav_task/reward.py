"""Per-step training reward functions for navigation episodes.

Design
------
Offline evaluation (SR, SPL) lives in measures.py and is computed once at
episode end. This module provides *stateful* per-step reward functions used
during training. The two concepts must stay separate:

  measures.py  → evaluation, no state, called once post-episode
  reward.py    → training signal, stateful, called every step

Distance metric (Habitat-aligned)
---------------------------------
Dense shaping uses **geodesic** distance only — the shortest navigable path
length from the current position to the goal (``UENavigationInterface.
get_geodesic_distance``), same role as Habitat's ``DistanceToGoal`` measure.
Euclidean straight-line distance is **not** used for reward (it can cross
walls and does not match PointNav).

Success bonus (Habitat-aligned options)
---------------------------------------
Habitat's ``Success`` measure requires **stop** + within **geodesic**
distance (see ``habitat/tasks/nav/nav.py`` ``Success``). By default we only
check geodesic distance ≤ ``success_distance_cm`` (simpler for RL without a
discrete STOP action). Set ``require_stop=True`` and call
``SuccessBonusReward.on_stop_action()`` when the agent emits STOP to match
Habitat's gate.

Reference: Habitat-Lab ``RLTaskEnv.get_reward``, ``DistanceToGoalReward``,
``Success`` (``habitat/core/environments.py``, ``habitat/tasks/nav/nav.py``).

Further rewards (UE / reference backlog)
----------------------------------------
Worth adding when you have live sim: **collision** / **proximity-to-obstacle**
shaping (Habitat has auxiliary measures); **compass/GPS noise** matching
sensors; **step penalty** is already ``step_cost`` (slack). These belong in
new ``RewardFunction`` subclasses that read from ``UENavigationInterface``
extensions (raycast, collision count), not in ``DistanceToGoalReward``.

UE integration
--------------
    reward_fn.step_from_interface(ue_interface)   # get_agent_position → step

Tests and offline use pass ``(x, y)`` directly to ``step()``.
"""

from __future__ import annotations

import abc
from typing import TYPE_CHECKING, Optional, Tuple

from .episode import NavigationEpisode, Position

if TYPE_CHECKING:
    from .interface import UENavigationInterface


def _geodesic_to_goal(
    interface: "UENavigationInterface",
    goal: Position,
    cx: float,
    cy: float,
) -> float:
    """Compute geodesic distance from (cx, cy) to goal via the interface.

    Raises RuntimeError if no navigable path exists.
    """
    cur = Position(x=cx, y=cy, node_type="intersection")
    dist = interface.get_geodesic_distance(cur, goal)
    if dist is None:
        raise RuntimeError(
            "Geodesic distance is undefined (no path to goal from this "
            "position — agent may be off the navigable graph)."
        )
    return dist


# ── Base class ────────────────────────────────────────────────────────────────

class RewardFunction(abc.ABC):
    """Stateful per-step reward for a single navigation episode.

    Lifecycle:
        1. Call reset(episode) at the start of each episode.
        2. Call step(current_position_cm) after each agent action.
        3. Read cumulative for the total reward accumulated so far.

    Thread safety: not thread-safe; one instance per running episode.
    """

    @abc.abstractmethod
    def reset(self, episode: NavigationEpisode) -> None:
        """Initialise internal state for a new episode.

        Must be called before the first step(). Safe to call multiple times
        (e.g. when re-using the same instance across episodes).

        Args:
            episode: The episode about to be executed.
        """
        ...

    @abc.abstractmethod
    def step(self, current_position_cm: Tuple[float, float]) -> float:
        """Compute and accumulate the reward for one agent step.

        Args:
            current_position_cm: Agent's (x, y) in cm after the action,
                obtained from the environment (UE or simulation).

        Returns:
            Scalar reward for this step.

        Raises:
            RuntimeError: If reset() has not been called first.
        """
        ...

    @property
    @abc.abstractmethod
    def cumulative(self) -> float:
        """Total reward accumulated since the last reset()."""
        ...

    def step_from_interface(self, ue_interface: "UENavigationInterface") -> float:
        """Fetch the agent's current position from UE, then call step()."""
        pos = ue_interface.get_agent_position()
        return self.step(current_position_cm=(pos.x, pos.y))


# ── DistanceToGoalReward ──────────────────────────────────────────────────────

class DistanceToGoalReward(RewardFunction):
    """Dense shaping: r_t = (d_{t-1} − d_t) − step_cost with geodesic d.

    ``d_t`` is ``interface.get_geodesic_distance(current, goal)`` (graph /
    navmesh shortest path length in cm), matching Habitat's potential-based
    progress from ``DistanceToGoal`` + ``DistanceToGoalReward`` measures.

    Args:
        interface: Required. Supplies geodesic distances (same graph as task gen).
        step_cost: Non-negative per-step penalty. Default 0.01 ≈ |slack_reward|.
    """

    def __init__(
        self,
        interface: "UENavigationInterface",
        step_cost: float = 0.01,
    ) -> None:
        if step_cost < 0:
            raise ValueError(f"step_cost must be >= 0, got {step_cost}")
        self._step_cost = step_cost
        self._interface = interface
        self._episode: Optional[NavigationEpisode] = None
        self._goal: Optional[Position] = None
        self._prev_dist: float = 0.0
        self._cumulative: float = 0.0

    def reset(self, episode: NavigationEpisode) -> None:
        self._episode = episode
        self._goal = episode.goal_position
        start = episode.start_position
        self._prev_dist = _geodesic_to_goal(self._interface, self._goal, start.x, start.y)
        self._cumulative = 0.0

    def step(self, current_position_cm: Tuple[float, float]) -> float:
        if self._episode is None:
            raise RuntimeError("reset() must be called before step().")
        cx, cy = float(current_position_cm[0]), float(current_position_cm[1])
        curr_dist = _geodesic_to_goal(self._interface, self._goal, cx, cy)
        reward = (self._prev_dist - curr_dist) - self._step_cost
        self._prev_dist = curr_dist
        self._cumulative += reward
        return reward

    @property
    def cumulative(self) -> float:
        return self._cumulative

    @property
    def prev_distance_cm(self) -> float:
        """Geodesic distance to goal at end of last step (cm)."""
        return self._prev_dist


# ── SuccessBonusReward ────────────────────────────────────────────────────────

class SuccessBonusReward(RewardFunction):
    """Sparse bonus when geodesic distance to goal ≤ success radius.

    Uses ``interface.get_geodesic_distance(current, goal)``, aligned with
    Habitat's ``Success`` which gates on ``DistanceToGoal`` (geodesic), not
    Euclidean. Optional ``require_stop`` matches Habitat's additional
    ``is_stop_called`` condition — call ``on_stop_action()`` when the agent
    takes the STOP action before/within the same control step as reaching the
    goal (integration detail depends on your env loop).

    The bonus is granted **once** per episode when conditions first become
    true (avoids double-counting if the episode continues after success).

    Args:
        interface: Required for geodesic proximity checks.
        success_bonus: Added once on success (Habitat default 2.5).
        require_stop: If True, also require ``on_stop_action()`` before bonus.
    """

    def __init__(
        self,
        interface: "UENavigationInterface",
        success_bonus: float = 2.5,
        require_stop: bool = False,
    ) -> None:
        self._interface = interface
        self._success_bonus = success_bonus
        self._require_stop = require_stop
        self._episode: Optional[NavigationEpisode] = None
        self._goal: Optional[Position] = None
        self._success_given: bool = False
        self._stop_latched: bool = False
        self._cumulative: float = 0.0

    def on_stop_action(self) -> None:
        """Call when the agent emits the STOP action (Habitat parity)."""
        self._stop_latched = True

    def reset(self, episode: NavigationEpisode) -> None:
        self._episode = episode
        self._goal = episode.goal_position
        self._success_given = False
        self._stop_latched = False
        self._cumulative = 0.0

    def step(self, current_position_cm: Tuple[float, float]) -> float:
        if self._episode is None:
            raise RuntimeError("reset() must be called before step().")
        if self._success_given:
            return 0.0
        cx, cy = float(current_position_cm[0]), float(current_position_cm[1])
        threshold = self._episode.success_criteria.success_distance_cm
        dist = _geodesic_to_goal(self._interface, self._goal, cx, cy)
        if dist > threshold:
            return 0.0
        if self._require_stop and not self._stop_latched:
            return 0.0
        self._success_given = True
        self._cumulative += self._success_bonus
        return self._success_bonus

    @property
    def cumulative(self) -> float:
        return self._cumulative

    @property
    def success_given(self) -> bool:
        return self._success_given


# ── NavigationReward ──────────────────────────────────────────────────────────

class NavigationReward(RewardFunction):
    """Composed PointNav training reward (AllenAct / Habitat-style).

    Per-step reward::

        r_t = (d_{t-1} − d_t)              # geodesic progress shaping
              − step_cost                   # per-step penalty (slack)
              − failed_action_penalty       # if last_action_success is False
              + success_bonus               # one-shot, when goal reached

    This mirrors the AllenAct ``ObjectNaviThorGridTask.judge()`` structure::

        reward  = -0.01                                 # step_cost
        reward += -0.03  if not last_action_success     # failed_action_penalty
        reward += +1.0 / -1.0 on end action             # success_bonus

    ``last_action_success`` is determined via ``interface.get_collision_counts()``
    using SimWorld's ``GetCollisionNum`` blueprint call.  A step is considered
    failed when new collisions occurred since the previous step (the UE agent
    moves through contacts rather than being blocked, unlike ai2thor which
    prevents movement outright).

    Args:
        interface: Required. Supplies geodesic distances and collision counts.
        step_cost: Per-step penalty. Default 0.01 (AllenAct / Habitat slack).
        success_bonus: One-shot bonus on goal reached. Default 2.5 (Habitat).
        failed_action_penalty: Extra penalty when the action produced new
            collisions (agent hit an obstacle). Default 0.0 (off); set to
            0.03 to match AllenAct.
        require_stop: If True, require ``on_stop_action()`` for success
            bonus (Habitat ``Success`` parity).
    """

    def __init__(
        self,
        interface: "UENavigationInterface",
        step_cost: float = 0.01,
        success_bonus: float = 2.5,
        failed_action_penalty: float = 0.0,
        require_stop: bool = False,
    ) -> None:
        self._interface = interface
        self._failed_action_penalty = failed_action_penalty
        self._dtg = DistanceToGoalReward(
            interface=interface,
            step_cost=step_cost,
        )
        self._success = SuccessBonusReward(
            interface=interface,
            success_bonus=success_bonus,
            require_stop=require_stop,
        )
        self._prev_collision_total: int = 0
        self._failed_action_cumulative: float = 0.0

    def reset(self, episode: NavigationEpisode) -> None:
        self._dtg.reset(episode)
        self._success.reset(episode)
        counts = self._interface.get_collision_counts()
        self._prev_collision_total = sum(counts.values())
        self._failed_action_cumulative = 0.0

    def step(self, current_position_cm: Tuple[float, float]) -> float:
        r = self._dtg.step(current_position_cm) + self._success.step(
            current_position_cm
        )
        # Check action success via collision delta (AllenAct pattern)
        if self._failed_action_penalty > 0:
            counts = self._interface.get_collision_counts()
            curr_total = sum(counts.values())
            if curr_total > self._prev_collision_total:
                r -= self._failed_action_penalty
                self._failed_action_cumulative -= self._failed_action_penalty
            self._prev_collision_total = curr_total
        return r

    def on_stop_action(self) -> None:
        """Forward STOP to success component (Habitat parity)."""
        self._success.on_stop_action()

    @property
    def cumulative(self) -> float:
        return (
            self._dtg.cumulative
            + self._success.cumulative
            + self._failed_action_cumulative
        )

    @property
    def distance_to_goal_reward(self) -> DistanceToGoalReward:
        return self._dtg

    @property
    def success_bonus_reward(self) -> SuccessBonusReward:
        return self._success
