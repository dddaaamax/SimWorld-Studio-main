"""Main task generator: orchestrates episode sampling, validation, and serialization."""

from __future__ import annotations

import random
from datetime import datetime, timezone
from typing import List, Optional

from .episode import (
    EvaluationMetrics,
    NavigationEpisode,
    ObjectGoal,
    ObjectViewPoint,
    ReferencePath,
    RewardConfig,
    SuccessCriteria,
    WorldConfig,
)
from .interface import UENavigationInterface, _compute_path_length, _compute_view_points
from .validator import TaskValidator


class NavigationTaskGenerator:
    """Generate one or more NavigationEpisode objects from a roads graph.

    A single seed controls all randomness: start/goal node sampling.
    The seed is embedded in every produced episode for traceability.

    Args:
        interface: A UENavigationInterface implementation. Inject
            MockNavigationInterface for offline / test use.
        roads_file: Path to roads.json; stored in WorldConfig of each episode.
        success_distance_cm: Goal-success radius in cm.
        max_steps: Per-episode step cap.
        max_episode_time_s: Per-episode wall-clock cap.
        min_path_length_cm: Minimum graph path length to accept an episode.
            Episodes shorter than this are resampled.
        max_retries: Resampling attempts before raising RuntimeError.
        node_type: Restrict start/goal sampling to this node type. Pass None
            to sample from all nodes. Note: in the base SimWorld map all
            nodes are 'intersection' type, so None is usually the right choice.
    """

    def __init__(
        self,
        interface: UENavigationInterface,
        roads_file: str,
        success_distance_cm: float = 100.0,
        max_steps: int = 5000,
        max_episode_time_s: float = 300.0,
        min_path_length_cm: float = 1000.0,
        max_path_length_cm: Optional[float] = None,
        max_retries: int = 50,
        node_type: Optional[str] = None,
        world_x_min: Optional[float] = None,
        world_x_max: Optional[float] = None,
        world_y_min: Optional[float] = None,
        world_y_max: Optional[float] = None,
        step_cost: float = 0.01,
        success_bonus: float = 2.5,
        require_stop_for_success_bonus: bool = False,
    ) -> None:
        self._interface = interface
        self._roads_file = roads_file
        self._success_distance_cm = success_distance_cm
        self._max_steps = max_steps
        self._max_episode_time_s = max_episode_time_s
        self._min_path_length_cm = min_path_length_cm
        self._max_path_length_cm = max_path_length_cm
        self._max_retries = max_retries
        self._node_type = node_type
        self._reward_config = RewardConfig(
            step_cost=step_cost,
            success_bonus=success_bonus,
            require_stop_for_success_bonus=require_stop_for_success_bonus,
        )

        # Auto-derive bounds from the loaded map unless the caller overrides.
        if any(b is None for b in (world_x_min, world_x_max, world_y_min, world_y_max)):
            auto = interface.get_world_bounds()
            world_x_min = auto[0] if world_x_min is None else world_x_min
            world_x_max = auto[1] if world_x_max is None else world_x_max
            world_y_min = auto[2] if world_y_min is None else world_y_min
            world_y_max = auto[3] if world_y_max is None else world_y_max

        self._world_x_min = world_x_min
        self._world_x_max = world_x_max
        self._world_y_min = world_y_min
        self._world_y_max = world_y_max
        self._validator = TaskValidator(
            min_path_length_cm=min_path_length_cm,
            max_path_length_cm=max_path_length_cm,
            world_x_min=world_x_min,
            world_x_max=world_x_max,
            world_y_min=world_y_min,
            world_y_max=world_y_max,
        )

    def generate(
        self,
        seed: int,
        n_episodes: int = 1,
    ) -> List[NavigationEpisode]:
        """Generate n_episodes episodes from a single seed.

        One random.Random(seed) instance drives all sampling decisions —
        start/goal node selection across all episodes. Each episode embeds
        the original seed for traceability.

        Args:
            seed: Master RNG seed.
            n_episodes: Number of episodes to generate.

        Returns:
            List of validated NavigationEpisode objects.

        Raises:
            RuntimeError: If a valid episode cannot be found within
                max_retries attempts for any episode index.
        """
        rng = random.Random(seed)
        episodes = []
        for idx in range(n_episodes):
            episode = self._sample_episode(seed=seed, episode_index=idx, rng=rng)
            episodes.append(episode)
        return episodes

    def _sample_episode(
        self,
        seed: int,
        episode_index: int,
        rng: random.Random,
    ) -> NavigationEpisode:
        """Sample start/goal, compute path, build and validate one episode.

        Resamples up to max_retries times if the episode fails validation.

        Args:
            seed: Master seed embedded in episode metadata.
            episode_index: Zero-based index used to build episode_id.
            rng: Shared Random instance from generate(); caller owns it.

        Returns:
            A validated NavigationEpisode.

        Raises:
            RuntimeError: If no valid episode found after max_retries.
        """
        # Fetch all navigable positions once (no RNG needed for full list)
        all_positions = self._interface.get_navigable_positions(
            node_type=self._node_type,
            count=None,
            rng=None,
        )

        if len(all_positions) < 2:
            raise RuntimeError(
                f"Graph has only {len(all_positions)} navigable position(s); "
                "need at least 2 to form a start/goal pair."
            )

        last_error = None
        for attempt in range(self._max_retries):
            # Sample start and goal without replacement
            pair = rng.sample(all_positions, 2)
            start, goal = pair[0], pair[1]

            # Compute reference path
            waypoints = self._interface.get_reference_path(start, goal)
            if waypoints is None or len(waypoints) < 2:
                last_error = "no path found between sampled start and goal"
                continue

            path_length = _compute_path_length(waypoints)

            world = WorldConfig(
                map_file=self._roads_file,
                x_min=self._world_x_min,
                x_max=self._world_x_max,
                y_min=self._world_y_min,
                y_max=self._world_y_max,
            )
            success_criteria = SuccessCriteria(
                success_distance_cm=self._success_distance_cm,
                max_steps=self._max_steps,
                max_episode_time_s=self._max_episode_time_s,
            )
            episode = NavigationEpisode(
                episode_id=self._make_episode_id(seed, episode_index),
                seed=seed,
                world=world,
                start_position=start,
                goal_position=goal,
                reference_path=ReferencePath(
                    waypoints=tuple(waypoints),
                    shortest_path_length_cm=round(path_length, 4),
                ),
                success_criteria=success_criteria,
                evaluation_metrics=EvaluationMetrics(
                    success_distance_cm=self._success_distance_cm,
                    shortest_path_length_cm=round(path_length, 4),
                ),
                generated_at=datetime.now(timezone.utc).isoformat(),
                reward_config=self._reward_config,
            )

            result = self._validator.validate(episode)
            if result.is_valid:
                return episode

            last_error = "; ".join(result.errors)

        raise RuntimeError(
            f"Failed to generate a valid episode for index {episode_index} "
            f"after {self._max_retries} attempts. Last error: {last_error}"
        )

    # ── ObjectNav generation ────────────────────────────────────────────────

    def generate_objectnav(
        self,
        seed: int,
        object_category: str,
        n_episodes: int = 1,
        max_view_distance_cm: float = 500.0,
    ) -> List[NavigationEpisode]:
        """Generate ObjectNav episodes targeting a semantic object category.

        The goal position is set to the nearest reachable **view_point**
        of a randomly selected object instance, matching Habitat-Lab's
        ``distance_to: VIEW_POINTS`` pattern
        (``habitat/tasks/nav/nav.py:960-987``).

        **Success / STOP action:** Habitat ObjectNav requires the agent to
        call STOP within ``success_distance_cm`` of a view_point.  Use
        ``NavigationReward(require_stop=True)`` (same as PointNav) and call
        ``on_stop_action()`` when the agent emits ``StopAgent``.

        Args:
            seed: Master RNG seed.
            object_category: Semantic category from ``ue_assets.json``,
                e.g. ``"TRASH"``, ``"VEGETATION"``.
            n_episodes: Number of episodes to generate.
            max_view_distance_cm: Maximum geodesic distance from object's
                nearest graph node to consider a node as a view_point.

        Returns:
            List of validated ObjectNav episodes.

        Raises:
            RuntimeError: If no objects of the category exist or no valid
                episode can be found within max_retries.
        """
        rng = random.Random(seed)
        episodes = []
        for idx in range(n_episodes):
            episode = self._sample_objectnav_episode(
                seed=seed,
                episode_index=idx,
                rng=rng,
                object_category=object_category,
                max_view_distance_cm=max_view_distance_cm,
            )
            episodes.append(episode)
        return episodes

    def _sample_objectnav_episode(
        self,
        seed: int,
        episode_index: int,
        rng: random.Random,
        object_category: str,
        max_view_distance_cm: float,
    ) -> NavigationEpisode:
        """Sample a single ObjectNav episode with retry loop."""
        all_positions = self._interface.get_navigable_positions(
            node_type=self._node_type, count=None, rng=None,
        )
        objects = self._interface.get_scene_objects(category=object_category)
        if not objects:
            raise RuntimeError(
                f"No scene objects found for category '{object_category}'. "
                "Ensure elements_file is provided to the interface."
            )
        if len(all_positions) < 2:
            raise RuntimeError(
                f"Graph has only {len(all_positions)} navigable position(s)."
            )

        last_error = None
        for attempt in range(self._max_retries):
            start = rng.choice(all_positions)
            obj = rng.choice(objects)

            # Compute view_points: try UE visibility first, fall back to geodesic
            try:
                view_points = self._interface.compute_view_points_with_visibility(
                    object_actor_name=f"{obj.object_type}_{obj.instance_index}",
                    candidate_positions=all_positions,
                )
            except (ConnectionError, RuntimeError):
                view_points = []
            if not view_points:
                # Fallback: geodesic proximity heuristic (offline generation)
                view_points = _compute_view_points(
                    obj.position, all_positions, self._interface,
                    max_view_distance_cm,
                )
            if not view_points:
                last_error = (
                    f"no graph nodes within {max_view_distance_cm} cm "
                    f"(geodesic) of object {obj.object_type}"
                )
                continue

            # Find the nearest reachable view_point from start
            goal_node = None
            for vp in view_points:
                path = self._interface.get_reference_path(start, vp.position)
                if path is not None and len(path) >= 2:
                    goal_node = vp.position
                    waypoints = path
                    break
            if goal_node is None:
                last_error = "no reachable view_point from sampled start"
                continue

            path_length = _compute_path_length(waypoints)

            world = WorldConfig(
                map_file=self._roads_file,
                x_min=self._world_x_min,
                x_max=self._world_x_max,
                y_min=self._world_y_min,
                y_max=self._world_y_max,
            )
            success_criteria = SuccessCriteria(
                success_distance_cm=self._success_distance_cm,
                max_steps=self._max_steps,
                max_episode_time_s=self._max_episode_time_s,
            )
            object_goal = ObjectGoal(
                object_id=f"{obj.object_type}_{obj.instance_index:03d}",
                object_type=obj.object_type,
                object_category=obj.category,
                position=obj.position,
                view_points=tuple(view_points),
            )
            episode = NavigationEpisode(
                episode_id=f"objnav_ep_{seed}_{episode_index:03d}",
                seed=seed,
                world=world,
                start_position=start,
                goal_position=goal_node,
                reference_path=ReferencePath(
                    waypoints=tuple(waypoints),
                    shortest_path_length_cm=round(path_length, 4),
                ),
                success_criteria=success_criteria,
                evaluation_metrics=EvaluationMetrics(
                    success_distance_cm=self._success_distance_cm,
                    shortest_path_length_cm=round(path_length, 4),
                ),
                generated_at=datetime.now(timezone.utc).isoformat(),
                reward_config=self._reward_config,
                task_type="objectnav",
                object_category=object_category,
                object_goal=object_goal,
            )

            result = self._validator.validate(episode)
            if result.is_valid:
                return episode
            last_error = "; ".join(result.errors)

        raise RuntimeError(
            f"Failed to generate a valid ObjectNav episode for index "
            f"{episode_index} after {self._max_retries} attempts. "
            f"Last error: {last_error}"
        )

    def _make_episode_id(self, seed: int, index: int) -> str:
        """Build a deterministic episode identifier string.

        Format: nav_ep_{seed}_{index:03d}
        """
        return f"nav_ep_{seed}_{index:03d}"
