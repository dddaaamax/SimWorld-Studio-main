"""Lightweight simulated navigation environment (no UE dependency).

Simulates a 2D grid world where the agent must navigate from start to goal.
Obstacles are randomly placed to create detour challenges. The environment
produces the same observation format as SimWorldNavEnv (bearing, distance,
position) so the LLM agent uses identical prompts and action space.

This allows end-to-end co-evolution testing with real LLM calls but
deterministic, fast physics.
"""
from __future__ import annotations

import math
import random
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Set, Tuple


@dataclass
class SimEpisode:
    """Minimal episode spec for the simulated env."""
    episode_id: str
    start_x: float
    start_y: float
    goal_x: float
    goal_y: float
    start_yaw_deg: float = 0.0
    geodesic_cm: float = 0.0
    max_steps: int = 40
    success_distance_cm: float = 200.0
    obstacles: List[Tuple[float, float, float]] = field(default_factory=list)
    # (x, y, radius) circles


def generate_episode(
    seed: int,
    min_path_cm: float = 800.0,
    max_path_cm: float = 2000.0,
    heading_offset_hint: str = "random",
    max_steps: int = 40,
    obstacle_density: float = 0.0,  # 0-1, fraction of difficulty
) -> SimEpisode:
    """Generate a navigation episode with specified difficulty parameters."""
    rng = random.Random(seed)

    # Target path length
    target_path = rng.uniform(min_path_cm, max_path_cm)

    # Place goal at target distance from origin
    goal_angle = rng.uniform(0, 2 * math.pi)
    goal_x = target_path * math.cos(goal_angle)
    goal_y = target_path * math.sin(goal_angle)

    # Start heading based on hint
    goal_bearing_deg = math.degrees(math.atan2(goal_y, goal_x))
    if heading_offset_hint == "easy":
        offset = rng.uniform(-20, 20)
    elif heading_offset_hint == "medium":
        offset = rng.choice([-1, 1]) * rng.uniform(40, 80)
    elif heading_offset_hint == "hard":
        offset = rng.choice([-1, 1]) * rng.uniform(100, 170)
    else:
        offset = rng.uniform(-180, 180)
    start_yaw = goal_bearing_deg + offset

    # Place obstacles
    obstacles = []
    n_obstacles = int(obstacle_density * 8)
    for _ in range(n_obstacles):
        # Place along the direct path with some offset
        t = rng.uniform(0.2, 0.8)
        ox = t * goal_x + rng.uniform(-300, 300)
        oy = t * goal_y + rng.uniform(-300, 300)
        radius = rng.uniform(100, 250)
        obstacles.append((ox, oy, radius))

    return SimEpisode(
        episode_id=f"sim_ep_{seed:04d}",
        start_x=0.0,
        start_y=0.0,
        goal_x=goal_x,
        goal_y=goal_y,
        start_yaw_deg=start_yaw,
        geodesic_cm=target_path,
        max_steps=max_steps,
        success_distance_cm=200.0,
        obstacles=obstacles,
    )


class SimNavEnv:
    """Simulated 2D navigation environment."""

    FORWARD_DISTANCE = 300.0   # cm per MOVE_FORWARD
    TURN_ANGLE = 30.0          # degrees per TURN

    def __init__(self):
        self.x = 0.0
        self.y = 0.0
        self.yaw = 0.0
        self.episode: Optional[SimEpisode] = None
        self.step_count = 0
        self.path_length = 0.0
        self._stopped = False

    def reset(self, episode: SimEpisode) -> Dict[str, Any]:
        self.episode = episode
        self.x = episode.start_x
        self.y = episode.start_y
        self.yaw = episode.start_yaw_deg
        self.step_count = 0
        self.path_length = 0.0
        self._stopped = False
        return self._obs()

    def step(self, action: str) -> Tuple[Dict[str, Any], float, bool, bool, Dict[str, Any]]:
        """Execute action, return (obs, reward, terminated, truncated, info)."""
        self.step_count += 1
        prev_dist = self._distance_to_goal()

        if action == "MOVE_FORWARD":
            dx = self.FORWARD_DISTANCE * math.cos(math.radians(self.yaw))
            dy = self.FORWARD_DISTANCE * math.sin(math.radians(self.yaw))
            new_x = self.x + dx
            new_y = self.y + dy

            # Check obstacle collision
            blocked = False
            for ox, oy, r in self.episode.obstacles:
                dist_to_obs = math.sqrt((new_x - ox)**2 + (new_y - oy)**2)
                if dist_to_obs < r:
                    blocked = True
                    break

            if not blocked:
                self.path_length += self.FORWARD_DISTANCE
                self.x = new_x
                self.y = new_y
            # If blocked, agent stays in place (collision)

        elif action == "TURN_LEFT":
            self.yaw = (self.yaw + self.TURN_ANGLE) % 360

        elif action == "TURN_RIGHT":
            self.yaw = (self.yaw - self.TURN_ANGLE) % 360

        elif action == "STOP":
            self._stopped = True

        curr_dist = self._distance_to_goal()

        # Reward: progress toward goal - small step cost
        reward = (prev_dist - curr_dist) / 100.0 - 0.01

        # Success check: either agent called STOP near goal,
        # or agent walked close enough (auto-success for small models)
        success = curr_dist < self.episode.success_distance_cm
        if success:
            reward += 2.5  # success bonus

        terminated = success
        truncated = self.step_count >= self.episode.max_steps

        # SPL computation
        shortest = self.episode.geodesic_cm
        spl = 0.0
        if success and self.path_length > 0:
            spl = float(success) * shortest / max(shortest, self.path_length)

        info = {
            "step": self.step_count,
            "distance_to_goal_cm": curr_dist,
            "success": success,
            "SR": 1.0 if success else 0.0,
            "SPL": spl,
            "path_length_cm": self.path_length,
        }

        return self._obs(), reward, terminated, truncated, info

    def _obs(self) -> Dict[str, Any]:
        dist = self._distance_to_goal()
        bearing = self._bearing_to_goal()
        return {
            "agent_x": self.x,
            "agent_y": self.y,
            "agent_yaw_deg": self.yaw,
            "distance_to_goal_cm": dist,
            "bearing_deg": bearing,
        }

    def _distance_to_goal(self) -> float:
        return math.sqrt(
            (self.x - self.episode.goal_x)**2 +
            (self.y - self.episode.goal_y)**2
        )

    def _bearing_to_goal(self) -> float:
        """Bearing to goal relative to agent's heading. Positive = left."""
        goal_angle = math.degrees(math.atan2(
            self.episode.goal_y - self.y,
            self.episode.goal_x - self.x,
        ))
        bearing = (goal_angle - self.yaw + 180) % 360 - 180
        return bearing
