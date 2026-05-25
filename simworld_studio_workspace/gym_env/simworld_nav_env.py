"""Gym environment wrapping the SimWorld UE simulator for nav tasks.

Design constraints
------------------
* Single shared UnrealCV connection for actions, position queries, and
  RGB capture (no parallel sockets).
* Reward / measures from ``nav_task`` (Habitat-aligned), backed by
  ``EuclideanNavigationInterface`` since this repo's scenes have no
  road graph.
* PIE-mode safe: assumes UE is already in PIE.  Pre-PIE setup
  (spawning the world, starting PIE) is the user's responsibility,
  done either through the Studio JS server or via :class:`MCPClient`
  in a separate setup script.
* Stateless across episodes — call :meth:`reset` with a fresh episode
  to re-spawn / re-position the agent.

Standard Gymnasium 5-tuple step return: ``(obs, reward, terminated,
truncated, info)``.  We do not subclass ``gymnasium.Env`` formally to
avoid pulling its action/observation Space machinery (which doesn't
help us — observations are dicts, actions are tool-call strings).
The class follows the protocol regardless.
"""

from __future__ import annotations

import logging
import math
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from nav_task.episode import NavigationEpisode
from nav_task.interface_euclidean import EuclideanNavigationInterface
from nav_task.measures import (
    CLSMeasure,
    NDTWMeasure,
    PLRMeasure,
    SoftSPLMeasure,
    SPLMeasure,
    SuccessMeasure,
)
from nav_task.reward import NavigationReward
from nav_task.task_spec import make_task_prompt

from .action_space import (
    DEFAULT_FORWARD_DURATION_S,
    DEFAULT_TURN_ANGLE_DEG,
    is_stop_action,
    translate_action,
)
from .mcp_client import MCPClient
from .observation import ObservationBuilder
from .ucv_client import UCVClient

log = logging.getLogger(__name__)


# Per-action wait budget after sending the BP command before reading
# the new position.  StepForward must wait at least the requested
# duration; turns play out in ~1s.
_TICK_BUFFER_S = 0.5

_HUMANOID_BP = "/Game/TrafficSystem/Pedestrian/Base_User_Agent.Base_User_Agent_C"

# Spawn Z for humanoid.  Studio's ``agent-registry.json`` and the
# proven smoke test ``scripts/test_agent_control.py`` both use 110.
# (The SimWorld library's ``Communicator.spawn_agent`` uses 600 but
# that's for a different demo map.)  Override per-map via the
# ``spawn_z`` ctor arg if your scene needs it.
_DEFAULT_SPAWN_Z = 110.0


@dataclass
class StepResult:
    """Convenience container for the per-step ``info`` dict.

    Stored on the env so the runner can introspect at any time without
    re-parsing the dict.  Mostly used for stdout / debug.
    """
    t: int
    action_name: str
    raw_command: str
    reward: float
    cumulative_reward: float
    distance_to_goal_cm: float
    delta_distance_cm: float
    success: bool
    done: bool
    truncated: bool


class SimWorldNavEnv:
    """Navigation environment over a live UE / PIE session."""

    def __init__(
        self,
        *,
        ucv_client: UCVClient,
        mcp_client: Optional[MCPClient] = None,
        agent_name: str = "GymNavAgent_0",
        agent_blueprint: str = _HUMANOID_BP,
        camera_id: Optional[int] = None,
        image_size: Tuple[int, int] = (240, 320),
        capture_rgb: bool = True,
        capture_depth: bool = False,
        require_stop_for_success: bool = False,
        use_collision_penalty: bool = False,
        forward_duration_s: float = DEFAULT_FORWARD_DURATION_S,
        turn_angle_deg: float = DEFAULT_TURN_ANGLE_DEG,
        spawn_on_reset: bool = True,
        spawn_z: float = _DEFAULT_SPAWN_Z,
        agent_speed: int = 200,
        ensure_pie: bool = True,
    ) -> None:
        self.ucv = ucv_client
        self.mcp = mcp_client
        self.agent_name = agent_name
        self.agent_blueprint = agent_blueprint
        self.spawn_on_reset = spawn_on_reset
        self.spawn_z = spawn_z
        self.agent_speed = agent_speed
        self.ensure_pie = ensure_pie
        self._spawned = False
        self._pie_started = False
        self._first_reset = True
        self.forward_duration_s = forward_duration_s
        self.turn_angle_deg = turn_angle_deg

        # camera_id is resolved lazily after the agent is spawned (the
        # humanoid BP attaches its first-person camera at spawn time, so
        # we can only learn its sensor index after that).
        self._camera_id_override = camera_id
        self._camera_id: Optional[int] = camera_id

        self.nav_iface = EuclideanNavigationInterface(
            ucv_send=self.ucv.send,
            agent_name=agent_name,
        )
        self.reward_fn = NavigationReward(
            interface=self.nav_iface,
            step_cost=0.01,
            success_bonus=2.5,
            failed_action_penalty=0.03 if use_collision_penalty else 0.0,
            require_stop=require_stop_for_success,
        )
        self.success_measure = SuccessMeasure(interface=self.nav_iface)
        self.spl_measure = SPLMeasure(interface=self.nav_iface)
        self.softspl_measure = SoftSPLMeasure(interface=self.nav_iface)
        self.plr_measure = PLRMeasure()
        self.ndtw_measure = NDTWMeasure()
        self.cls_measure = CLSMeasure()

        self.obs_builder = ObservationBuilder(
            ucv=self.ucv,
            agent_name=agent_name,
            camera_id=camera_id if camera_id is not None else 0,
            image_size=image_size,
            capture_rgb=capture_rgb,
            capture_depth=capture_depth,
        )

        # Per-episode runtime state
        self.episode: Optional[NavigationEpisode] = None
        self.step_count: int = 0
        self.path_length_cm: float = 0.0
        self._start_xy: Tuple[float, float] = (0.0, 0.0)
        self._last_xy: Tuple[float, float] = (0.0, 0.0)
        self._cumulative_reward: float = 0.0
        self.last_step: Optional[StepResult] = None
        # Full agent (x,y) trajectory in cm, including start and every
        # post-step position.  Needed by nDTW / CLS.
        self.trajectory: List[Tuple[float, float]] = []

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def _bind_agent_name(self, new_name: str) -> None:
        """Switch all env subsystems to a discovered UE actor name."""
        if not new_name or new_name == self.agent_name:
            return
        log.info("env: binding agent_name %s -> %s", self.agent_name, new_name)
        self.agent_name = new_name
        # Euclidean interface keeps the actor id in a private field.
        if hasattr(self.nav_iface, "_agent_name"):
            self.nav_iface._agent_name = new_name
        self.obs_builder.agent_name = new_name

    @staticmethod
    def _find_existing_agent_name(objects: List[str]) -> Optional[str]:
        """Pick a likely controllable pedestrian actor from UE objects."""
        if not objects:
            return None
        # Prefer explicit co-evolve naming if present.
        for name in objects:
            if name.startswith("CoEvolveAgent"):
                return name
        # Fallback to map-existing pedestrian actors.
        for name in objects:
            lname = name.lower()
            if "base_user_agent" in lname or "user_agent" in lname:
                return name
        return None

    def _is_actor_alive(self, name: str) -> bool:
        """An actor name may linger in vget_objects after destroy; the
        location query is the truth source — it returns 'error' for
        dead actors. Used to avoid binding to a tombstone."""
        try:
            resp = self.ucv.send(f"vget /object/{name}/location")
            if not resp:
                return False
            if "error" in resp.lower():
                return False
            parts = resp.strip().split()
            float(parts[0]); float(parts[1]); float(parts[2])
            return True
        except Exception:
            return False

    def reset(self, episode: NavigationEpisode) -> Tuple[Dict[str, Any], Dict[str, Any]]:
        """Spawn / reposition the agent and return the initial obs."""
        log.info("env.reset: episode=%s task=%s", episode.episode_id, episode.task_type)
        self.episode = episode
        self.step_count = 0
        self.path_length_cm = 0.0
        self._cumulative_reward = 0.0

        start = episode.start_position
        self._start_xy = (start.x, start.y)
        self.trajectory = [self._start_xy]

        # Start PIE if requested.  Done in reset() (not __init__) so we
        # can recover from a manual stop/start cycle between episodes.
        if self.ensure_pie and self.mcp is not None and not self._pie_started:
            self._ensure_pie()
            self._pie_started = True

        if not self._spawned:
            # Check if the agent already exists in the scene (e.g. left
            # over from a previous run).  If so, skip the 90-second
            # spawn and just reuse it.
            existing = self.ucv.vget_objects()
            if self.agent_name in existing and self._is_actor_alive(self.agent_name):
                log.info("env: agent %s already in scene, reusing", self.agent_name)
                self._spawned = True
            else:
                discovered = self._find_existing_agent_name(existing)
                if discovered and self._is_actor_alive(discovered):
                    self._bind_agent_name(discovered)
                    log.info("env: discovered existing agent %s, reusing", discovered)
                    self._spawned = True
                elif discovered:
                    log.info("env: discovered %s but it is a dead tombstone, ignoring", discovered)
            if (not self._spawned) and self.spawn_on_reset:
                self._spawn_agent()
                self._spawned = True

        # Place at start.
        #
        # Note 1: gym_interface_demo.ipynb sleeps 5s after spawning
        # before the first camera capture — apparently the BP's
        # FusionCamSensor needs a handful of render ticks to register
        # properly.  Skipping this wait crashes UE on the first
        # vget /camera/{id}/lit png call.
        #
        # Note 2: there is no "exit PIE" command in UnrealCV (only
        # pause / resume / open level).  Our reset is therefore a
        # SOFT reset: teleport the existing agent + clear python-side
        # reward/measure state.  We also issue a StopAgent first to
        # cancel any movement the previous episode might have left
        # running, so the new episode starts from a stationary pose.
        try:
            self.ucv.vbp(self.agent_name, "StopAgent")
        except Exception as exc:
            log.debug("env.reset: StopAgent failed (non-fatal): %s", exc)
        self.ucv.vset_location(self.agent_name, start.x, start.y, self.spawn_z)
        self.ucv.vset_rotation(self.agent_name, 0.0, 0.0, 0.0)
        if self._first_reset:
            time.sleep(5.0)
            self._first_reset = False
        else:
            time.sleep(0.5)

        # Sync interface position cache and reset reward
        self._last_xy = (start.x, start.y)
        self.reward_fn.reset(episode)

        # Resolve camera AFTER teleport: the sensor's world location
        # only aligns with the agent's once the actor has been placed.
        # Cheap (~5 ucv calls for a 5-ghost wave) and re-runs per episode
        # in case the sensor ordering shifted between maps.
        self._resolve_camera_id()

        obs = self.obs_builder.observe(episode, self._start_xy)
        info = self._build_info(initial=True)
        return obs, info

    def _ensure_pie(self) -> None:
        """Start PIE via MCP if it isn't already running.

        PIE start resets the UnrealCV server inside the new game world,
        so we deliberately drop and reconnect the UCV socket afterwards
        — matches the recovery sequence in
        ``scripts/test_agent_control.py:343-355``.
        """
        if self.mcp is None:
            log.warning("env: ensure_pie=True but no mcp_client provided; skipping")
            return
        try:
            already = self.mcp.is_pie_active()
        except Exception as exc:
            log.warning("env: PIE probe failed (%s); attempting start anyway", exc)
            already = False
        if already:
            log.info("env: PIE already active")
            return
        log.info("env: requesting PIE start via MCP")
        self.mcp.start_pie(wait_seconds=5.0)
        # PIE restarts UnrealCV; hard-reset our client so we don't drag
        # a stale receive queue (or stale socket) into the new game world.
        self.ucv.hard_reconnect()
        log.info("env: UCV hard-reconnected after PIE start")

    def _spawn_agent(self) -> None:
        """Spawn + configure the humanoid agent.

        The full sequence here mirrors what SimWorld's own
        ``Communicator.spawn_agent`` does
        (see ``SimWorld/simworld/communicator/communicator.py:603-645``)
        plus the camera-tutorial's ``enable_controller`` step
        (see ``SimWorld/examples/camera.ipynb``).  Empirically the
        env's ``vget /camera/N/lit png`` calls only succeed if all of
        these have been issued — skipping ``set_scale`` or
        ``EnableController`` leaves the FusionCamSensor's
        SceneCapture render target in an uninitialised state and the
        first lit-capture call crashes UE.
        """
        log.info("env: spawning %s as %s", self.agent_blueprint, self.agent_name)

        # 1. Pre-spawn: ensure the humanoid skeletal mesh is compiled
        # and ready for instantiation.
        try:
            self.ucv.send("vrun Editor.AsyncSkinnedAssetCompilation 2")
            time.sleep(2.0)
        except Exception as exc:
            log.warning("env: AsyncSkinnedAssetCompilation failed (non-fatal): %s", exc)

        # 2. Spawn the BP actor.  This may close the socket; spawn_bp_asset
        # already handles a hard reconnect internally.
        self.ucv.spawn_bp_asset(self.agent_blueprint, self.agent_name)

        # 3. Configure transform / collision / mobility / scale.
        # Order and completeness matter for component registration —
        # missing scale has been observed to leave SceneCapture
        # uninitialised on this UE build.
        try:
            self.ucv.send(f"vset /object/{self.agent_name}/scale 1 1 1")
            self.ucv.send(f"vset /object/{self.agent_name}/collision true")
            self.ucv.send(f"vset /object/{self.agent_name}/object_mobility true")
        except Exception as exc:
            log.warning("env: failed to set transform/collision/mobility: %s", exc)

        # 4. Walking speed (matches gym_interface_demo's default of 200).
        try:
            self.ucv.vbp(self.agent_name, f"SetMaxSpeed {self.agent_speed}")
        except Exception as exc:
            log.warning("env: failed to set max speed on agent: %s", exc)

        # 5. Enable AI controller.  Required by the camera.ipynb
        # tutorial path; without this the BP's SceneCaptureComponent
        # is never possessed and its render target stays detached.
        try:
            self.ucv.vbp(self.agent_name, "EnableController True")
        except Exception as exc:
            log.warning("env: EnableController failed (non-fatal): %s", exc)

    def _resolve_camera_id(self) -> None:
        """Resolve the camera index for this agent's first-person view.

        ``USensorBPLib::GetFusionSensorList`` orders sensors as
        ``[pawn's sensors] + [other FusionCamSensors in UE hash order]``
        — the hash order is non-deterministic across ghosts, so the
        slot index we pass from the runner does NOT correspond to the
        sensor index.  Instead, we locate our agent's sensor by
        matching each ``vget /camera/N/location`` against the agent's
        actor location (FusionCamSensor is attached at eye-level so
        the world-space distance is on the order of 10-150 cm).

        If ``camera_id`` is explicitly set via the ctor, honour it
        (useful for legacy / single-agent paths).  Otherwise probe
        the sensor list and pick the closest match.
        """
        if self._camera_id_override is not None:
            self._camera_id = self._camera_id_override
            self.obs_builder.camera_id = self._camera_id
            log.info("env: using override camera_id=%d for %s",
                     self._camera_id, self.agent_name)
            return

        # Location-based resolution
        try:
            agent_x, agent_y, agent_z = self.ucv.vget_location(self.agent_name)
        except Exception as exc:
            log.warning("env: could not read %s location (%s); fallback camera_id=0",
                        self.agent_name, exc)
            self._camera_id = 0
            self.obs_builder.camera_id = 0
            return

        try:
            resp = self.ucv.send("vget /cameras")
        except Exception as exc:
            log.warning("env: vget /cameras failed (%s); fallback camera_id=0", exc)
            self._camera_id = 0
            self.obs_builder.camera_id = 0
            return

        n_cams = len(resp.strip().split()) if resp else 0
        best_id: Optional[int] = None
        best_dist = float("inf")
        for cam_id in range(n_cams):
            try:
                loc_resp = self.ucv.send(f"vget /camera/{cam_id}/location")
            except Exception:
                continue
            parts = loc_resp.strip().split()
            # UCV returns "error ..." if the sensor is invalid; skip those.
            if len(parts) < 3 or parts[0].lower() == "error":
                continue
            try:
                cx, cy, cz = float(parts[0]), float(parts[1]), float(parts[2])
            except ValueError:
                continue
            dist = math.sqrt(
                (cx - agent_x) ** 2
                + (cy - agent_y) ** 2
                + (cz - agent_z) ** 2
            )
            if dist < best_dist:
                best_dist = dist
                best_id = cam_id

        # Sensors attached to the humanoid BP sit at eye-level relative to
        # the actor, so the camera-to-actor distance should be well under
        # a few meters when the match is correct.  Anything farther is
        # likely a stale / wrong-agent sensor.
        if best_id is None or best_dist > 500.0:
            log.warning(
                "env: could not locate camera for %s (best_id=%s dist=%.0fcm "
                "n_cams=%d agent_xyz=(%.0f,%.0f,%.0f)); fallback camera_id=0",
                self.agent_name, best_id, best_dist, n_cams,
                agent_x, agent_y, agent_z,
            )
            self._camera_id = 0
            self.obs_builder.camera_id = 0
            return

        self._camera_id = best_id
        self.obs_builder.camera_id = best_id
        log.info(
            "env: resolved camera_id=%d for %s (dist=%.0fcm, n_cams=%d)",
            best_id, self.agent_name, best_dist, n_cams,
        )

    def close(self) -> None:
        log.info("env.close")
        # We do not destroy the spawned agent — leave the scene as-is so
        # subsequent runs can re-bind.  Caller can destroy via UCV if
        # they want a fully clean teardown.

    # ------------------------------------------------------------------
    # Step
    # ------------------------------------------------------------------

    def step_send(self, action: Dict[str, Any]) -> float:
        """Send action to UE (non-blocking). Returns expected wait_s.

        Use with step_finalize for batched ghost waves — all agents send in
        phase A, caller sleeps once, then all call finalize in phase C.
        """
        if self.episode is None:
            raise RuntimeError("env.step called before reset")
        self.step_count += 1
        action_name = action.get("tool") or action.get("name") or "UNKNOWN"
        cmd = translate_action(
            action, self.agent_name,
            forward_duration_s=self.forward_duration_s,
            turn_angle_deg=self.turn_angle_deg,
        )
        self.ucv.send(cmd)
        self._pending_action = action
        self._pending_cmd = cmd
        self._pending_action_name = action_name
        if action_name == "MOVE_FORWARD":
            return self.forward_duration_s + _TICK_BUFFER_S
        elif action_name in ("TURN_LEFT", "TURN_RIGHT"):
            return 1.0 + _TICK_BUFFER_S
        return _TICK_BUFFER_S

    def step_finalize(self) -> Tuple[Dict[str, Any], float, bool, bool, Dict[str, Any]]:
        """Finalize step after action has completed — read position, capture, compute."""
        action = self._pending_action
        cmd = self._pending_cmd
        action_name = self._pending_action_name
        return self._step_body(action, cmd, action_name)

    def step(
        self, action: Dict[str, Any],
    ) -> Tuple[Dict[str, Any], float, bool, bool, Dict[str, Any]]:
        """Send action + wait + finalize (blocking). For single-agent / sequential use."""
        if self.episode is None:
            raise RuntimeError("env.step called before reset")
        self.step_count += 1
        action_name = action.get("tool") or action.get("name") or "UNKNOWN"
        cmd = translate_action(
            action, self.agent_name,
            forward_duration_s=self.forward_duration_s,
            turn_angle_deg=self.turn_angle_deg,
        )
        log.debug("env.step %d: %s", self.step_count, cmd)
        self.ucv.send(cmd)
        if action_name == "MOVE_FORWARD":
            wait_s = self.forward_duration_s + _TICK_BUFFER_S
        elif action_name in ("TURN_LEFT", "TURN_RIGHT"):
            wait_s = 1.0 + _TICK_BUFFER_S
        else:
            wait_s = _TICK_BUFFER_S
        time.sleep(wait_s)
        return self._step_body(action, cmd, action_name)

    def _step_body(self, action, cmd, action_name):
        """Common logic after action has played out (read pos, capture, reward)."""

        # 3. Read new position; update path length and reward
        if is_stop_action(action):
            self.reward_fn.on_stop_action()

        try:
            new_pos = self.nav_iface.get_agent_position()
        except RuntimeError as exc:
            # UE can occasionally lose the actor handle mid-PIE tick.
            # Try one best-effort recovery before failing the episode.
            msg = str(exc).lower()
            if "can not find object" in msg or "cannot find object" in msg:
                log.warning("env.step: agent missing in UE, attempting recovery: %s", exc)
                self._recover_missing_agent()
                new_pos = self.nav_iface.get_agent_position()
            else:
                raise
        new_xy = (new_pos.x, new_pos.y)
        delta = math.sqrt(
            (new_xy[0] - self._last_xy[0]) ** 2
            + (new_xy[1] - self._last_xy[1]) ** 2
        )
        self.path_length_cm += delta
        self._last_xy = new_xy
        self.trajectory.append(new_xy)

        reward = float(self.reward_fn.step(new_xy))
        self._cumulative_reward += reward

        # 4. Done / truncated logic
        success = self.reward_fn.success_bonus_reward.success_given
        max_steps = self.episode.success_criteria.max_steps
        terminated = success or is_stop_action(action)
        truncated = self.step_count >= max_steps and not terminated

        # 5. Build obs + info
        obs = self.obs_builder.observe(self.episode, self._start_xy)
        d_to_goal = self.nav_iface.get_geodesic_distance(
            new_pos, self.episode.goal_position
        ) or 0.0
        info = self._build_info(
            action_name=action_name, raw_command=cmd,
            reward=reward, distance_to_goal_cm=d_to_goal,
            delta_distance_cm=delta, success=success,
            terminated=terminated, truncated=truncated,
        )

        self.last_step = StepResult(
            t=self.step_count, action_name=action_name, raw_command=cmd,
            reward=reward, cumulative_reward=self._cumulative_reward,
            distance_to_goal_cm=d_to_goal, delta_distance_cm=delta,
            success=success, done=terminated, truncated=truncated,
        )
        log.info(
            "step %d %s -> reward=%+.3f cum=%+.3f d_goal=%.0fcm done=%s",
            self.step_count, action_name, reward, self._cumulative_reward,
            d_to_goal, terminated or truncated,
        )

        # 6. Final metrics if episode ended
        if terminated or truncated:
            metrics = self._final_metrics(new_xy)
            info["metrics"] = metrics
            log.info("episode done: %s", metrics)

        return obs, reward, terminated, truncated, info

    def _recover_missing_agent(self) -> None:
        """Best-effort recovery when UE reports agent actor missing."""
        # Refresh socket state first; UnrealCV may have rolled over between ticks.
        self.ucv.hard_reconnect()

        existing = set(self.ucv.vget_objects())
        if self.agent_name not in existing:
            log.warning("env.recover: actor %s not found, respawning", self.agent_name)
            self._spawn_agent()
            self._spawned = True

        # Restore to the last known pose so episode dynamics remain coherent.
        self.ucv.vset_location(self.agent_name, self._last_xy[0], self._last_xy[1], self.spawn_z)
        self.ucv.vset_rotation(self.agent_name, 0.0, 0.0, 0.0)
        try:
            self.ucv.vbp(self.agent_name, "StopAgent")
        except Exception:
            pass
        time.sleep(0.5)
        self._resolve_camera_id()

    # ------------------------------------------------------------------
    # Info / metrics
    # ------------------------------------------------------------------

    def _build_info(
        self, *,
        initial: bool = False,
        action_name: str = "",
        raw_command: str = "",
        reward: float = 0.0,
        distance_to_goal_cm: float = 0.0,
        delta_distance_cm: float = 0.0,
        success: bool = False,
        terminated: bool = False,
        truncated: bool = False,
    ) -> Dict[str, Any]:
        ep = self.episode
        return {
            "episode_id": ep.episode_id if ep else None,
            "task_type": ep.task_type if ep else None,
            "task_prompt": make_task_prompt(ep) if ep else "",
            "step": self.step_count,
            "agent_name": self.agent_name,
            "agent_xy": self._last_xy,
            "start_xy": self._start_xy,
            "goal_xy": (ep.goal_position.x, ep.goal_position.y) if ep else None,
            "distance_to_goal_cm": distance_to_goal_cm,
            "delta_distance_cm": delta_distance_cm,
            "path_length_cm": self.path_length_cm,
            "reward": reward,
            "cumulative_reward": self._cumulative_reward,
            "success": success,
            "terminated": terminated,
            "truncated": truncated,
            "action_name": action_name,
            "raw_command": raw_command,
            "initial": initial,
        }

    def _final_metrics(self, final_xy: Tuple[float, float]) -> Dict[str, float]:
        ep = self.episode
        kwargs = dict(
            final_position_cm=final_xy,
            actual_path_length_cm=self.path_length_cm,
            trajectory_cm=self.trajectory,
        )
        return {
            "SR": float(self.success_measure.compute(ep, **kwargs)),
            "SPL": float(self.spl_measure.compute(ep, **kwargs)),
            "SoftSPL": float(self.softspl_measure.compute(ep, **kwargs)),
            "PLR": float(self.plr_measure.compute(ep, **kwargs)),
            "nDTW": float(self.ndtw_measure.compute(ep, **kwargs)),
            "CLS": float(self.cls_measure.compute(ep, **kwargs)),
            "path_length_cm": self.path_length_cm,
            "cumulative_reward": self._cumulative_reward,
        }
