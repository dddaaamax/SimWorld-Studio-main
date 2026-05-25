"""Batch episode runner using ghost-mode agents.

One UE instance, N ghost agents running N tasks concurrently.
LLM calls are either batched (vLLM ``/v1/chat/completions`` with
multiple independent requests) or sequential.

Modes
-----
* **batch** (default): ghost agents, RGB on, optional memory + WandB.
  Task generator produces a pool of episodes; agents run them in
  parallel waves.
* **single**: normal (non-ghost) agent, trajectory + frames saved.

Usage::

    # Batch: 6 tasks, 3 concurrent ghost agents per wave
    python -m gym_env.batch_runner --mode batch --n-tasks 6 --wave-size 3 \\
        --model qwen --base-url http://localhost:8000/v1

    # With memory enabled
    python -m gym_env.batch_runner --mode batch --n-tasks 6 --wave-size 3 \\
        --model qwen --memory strategy

    # Single with trajectory
    python -m gym_env.batch_runner --mode single --n-tasks 1 --model claude
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from nav_task.episode import NavigationEpisode
from tqdm.auto import tqdm

from .action_space import nav_tool_schemas
from .llm import LLMClient, LLMMessage, make_llm
from .logger import EpisodeLogger
from .memory import AgentMemory, NullMemory, ReadOnlyMemory, build_memory
from .simworld_nav_env import SimWorldNavEnv
from .ucv_client import UCVClient
from .mcp_client import MCPClient

log = logging.getLogger(__name__)

_HUMANOID_BP = "/Game/TrafficSystem/Pedestrian/Base_User_Agent.Base_User_Agent_C"
_DEFAULT_SPAWN_Z = 110.0

_NAV_SYSTEM_PROMPT = """You are an embodied navigation agent in a 3D city scene.

You may receive a first-person RGB image, a depth map (near = bright,
far = dark), or no image at all depending on the sensor configuration.
Choose ONE navigation action per turn:

  - MOVE_FORWARD : walk forward ~2 seconds (~200-400 cm)
  - TURN_LEFT    : rotate 30 degrees left
  - TURN_RIGHT   : rotate 30 degrees right
  - STOP         : declare you have reached the goal

Each step you receive the bearing to the goal (degrees) and distance.
Discover the bearing sign convention by observing your TURN effects.
STOP when distance < 200 cm.

IMPORTANT OUTPUT FORMAT:
Reply with EXACTLY ONE action name on its own line (e.g. just `MOVE_FORWARD`).
Do NOT explain, do NOT think out loud, do NOT add any other words.
The action name MUST be one of: MOVE_FORWARD, TURN_LEFT, TURN_RIGHT, STOP."""


# ---------------------------------------------------------------------------
# Ghost agent slot — holds per-agent state for one concurrent task
# ---------------------------------------------------------------------------

@dataclass
class AgentSlot:
    """Mutable state for one ghost agent running one episode."""
    idx: int
    agent_name: str
    episode: NavigationEpisode
    env: SimWorldNavEnv
    logger: Optional[EpisodeLogger] = None
    # Per-agent memory view: private L1, shared L2/L3 (see HierarchicalMemory.fork).
    # Falls back to the shared memory object if the backend doesn't support fork.
    mem_view: Any = None
    history: List[LLMMessage] = field(default_factory=list)
    step: int = 0
    done: bool = False
    ended_reason: str = "max_steps"  # "success" | "truncated" | "max_steps" | "llm_error" | "no_tool_call"
    cumulative_reward: float = 0.0
    metrics: Dict[str, Any] = field(default_factory=dict)
    task_prompt: str = ""
    _obs: Dict[str, Any] = field(default_factory=dict)
    _info: Dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _build_user_text(info: Dict[str, Any], obs: Dict[str, Any]) -> str:
    parts: List[str] = []
    parts.append(f"Task: {info.get('task_prompt', '')}")
    if "pointgoal_with_gps_compass" in obs:
        d, ang = obs["pointgoal_with_gps_compass"].tolist()
        parts.append(f"Goal: distance={d:.0f} cm, bearing={math.degrees(ang):+.0f} deg")
    parts.append(
        f"Position: ({obs['agent_xy'][0]:.0f}, {obs['agent_xy'][1]:.0f})"
        f"  yaw={obs['agent_yaw_deg']:+.0f} deg"
    )
    parts.append(f"Step: {info['step']}")
    return "\n".join(parts)


def _strip_images(messages: List[LLMMessage], keep_last_k: int) -> None:
    if keep_last_k is None:
        return
    user_turns = [
        i for i, m in enumerate(messages)
        if m.role == "user" and any(b["type"] == "image" for b in m.content)
    ]
    if len(user_turns) <= keep_last_k:
        return
    for i in user_turns[:-keep_last_k]:
        messages[i].content = [
            b if b["type"] == "text" else {"type": "text", "text": "[image omitted]"}
            for b in messages[i].content
        ]


def _configure_ghost_ucv(ucv: UCVClient, name: str, x: float, y: float, z: float) -> None:
    """Configure ghost mode via UnrealCV: hide, collision channels, teleport.

    Collision is left **disabled** — caller must call
    :func:`_finalize_ghost_agents` after all agents are configured.
    """
    ghost_cmds = [
        (f"vset /object/{name}/collision false", "disable collision"),
        (f"vset /object/{name}/hide", "hide"),
        (f"vset /object/{name}/collision_channel 8", "set channel 8"),
        (f"vset /object/{name}/collision_response 8 ignore", "ignore ghosts"),
        (f"vset /object/{name}/collision_response 2 ignore", "ignore pawns"),
    ]
    for cmd, desc in ghost_cmds:
        try:
            resp = ucv.send(cmd)
            log.info("ghost %s %s: %r", name, desc, resp)
        except Exception as exc:
            log.error("ghost %s %s FAILED: %s", name, desc, exc)

    # Teleport to target position (collision is off, no depenetration)
    try:
        ucv.send(f"vset /object/{name}/location {x} {y} {z}")
        actual_loc = ucv.send(f"vget /object/{name}/location")
        log.info("ghost %s teleported to (%.0f,%.0f,%.0f), actual: %s",
                 name, x, y, z, actual_loc.strip())
    except Exception as exc:
        log.error("ghost %s teleport FAILED: %s", name, exc)

    try:
        ucv.vbp(name, "SetMaxSpeed 200")
        ucv.vbp(name, "EnableController true")
    except Exception as exc:
        log.warning("ghost %s post-spawn config: %s", name, exc)


def _finalize_ghost_agents(ucv: UCVClient, names: List[str]) -> None:
    """Enable collision on all ghost agents in one batch.

    Called after all agents are spawned so UE only rebuilds navmesh once.
    """
    log.info("finalizing %d ghost agents — enabling collision", len(names))
    for name in names:
        try:
            resp = ucv.send(f"vset /object/{name}/collision true")
            log.info("ghost %s collision on: %r", name, resp)
        except Exception as exc:
            log.error("ghost %s collision on FAILED: %s", name, exc)


# ---------------------------------------------------------------------------
# Episode loading
# ---------------------------------------------------------------------------

def _load_episodes_file(path: str) -> List[NavigationEpisode]:
    """Load episodes from a pre-generated JSON file.

    Accepts three shapes produced by ``python -m nav_task``:
      * split file: ``{"episodes": [...], ...}`` (from ``--split``)
      * raw list: ``[{...}, {...}]`` (from ``--output`` with n > 1)
      * single episode: ``{...}`` (from ``--output`` with n == 1)
    """
    text = Path(path).read_text()
    data = json.loads(text)
    if isinstance(data, dict) and "episodes" in data:
        raw_list = data["episodes"]
    elif isinstance(data, list):
        raw_list = data
    elif isinstance(data, dict) and "episode_id" in data:
        raw_list = [data]
    else:
        raise ValueError(
            f"{path}: unrecognised shape (expected split dict, list, or "
            "single-episode dict)"
        )
    return [NavigationEpisode.from_dict(d) for d in raw_list]


# ---------------------------------------------------------------------------
# WandB helpers
# ---------------------------------------------------------------------------

def _init_wandb(args, episodes):
    """Initialize WandB run for batch experiment."""
    import wandb
    config = {
        "mode": args.mode,
        "model": args.model,
        "model_id": args.model_id,
        "memory": args.memory,
        "n_tasks": args.n_tasks,
        "wave_size": args.wave_size,
        "max_steps": args.max_steps,
        "seed": args.seed,
        "ucv_port": args.ucv_port,
        "mcp_port": args.mcp_port,
        "vision_depth": args.vision_depth,
        "nav_min_cm": args.nav_min_cm,
        "nav_max_cm": args.nav_max_cm,
    }
    run_name = args.run_name or f"batch_{args.model}_{args.memory}_n{args.n_tasks}"
    run = wandb.init(
        project=args.wandb_project,
        name=run_name,
        config=config,
        tags=[args.model, args.memory, f"n{args.n_tasks}", f"wave{args.wave_size}"],
    )
    return run


# ---------------------------------------------------------------------------
# Core: run one wave of ghost agents
# ---------------------------------------------------------------------------

def run_wave(
    ucv: UCVClient,
    mcp: Optional[MCPClient],
    llm: LLMClient,
    episodes: List[NavigationEpisode],
    *,
    max_steps: int = 40,
    vision_depth: int = 3,
    spawn_z: float = _DEFAULT_SPAWN_Z,
    memory: Optional[AgentMemory] = None,
    wandb_run=None,
    global_step: int = 0,
    batch_dir: Optional[Path] = None,
    save_frames: bool = False,
    capture_rgb: bool = True,
    capture_depth: bool = False,
    image_kind: str = "rgb",
    reuse_agents: bool = False,
    skip_destroy: bool = False,
    name_prefix: str = "GhostAgent",
) -> Tuple[List[Dict[str, Any]], int]:
    """Run one batch of ghost agents concurrently in one UE instance.

    **One UE instance runs exactly one wave.**  Each agent gets its
    own ``SimWorldNavEnv`` with a unique name and camera ID, plus its
    own :class:`EpisodeLogger` writing per-step JSONL + frames +
    summary under ``batch_dir/ep_XXX_<name>/``.  LLM requests are
    issued sequentially (one per active agent per step).

    If *reuse_agents* is True, ghost agents are assumed to already
    exist in the world — only teleport to new start positions.
    If *skip_destroy* is True, agents are NOT destroyed at the end
    (caller plans to reuse them).

    Returns (list of metrics dicts, updated global_step).
    """
    n = len(episodes)
    log.info("wave: %d ghost agents, max_steps=%d, reuse=%s", n, max_steps, reuse_agents)
    mem = memory or NullMemory()
    # Per-agent trajectory buffers (memory.insert goes to shared mem,
    # but we also track per-agent for correct reflect at end)
    agent_trajectories: List[List[str]] = [[] for _ in range(n)]

    # --- Phase 1: spawn (or reuse) ghost agents ---
    # NOTE: name_prefix MUST be unique per wave when running multiple waves
    # in the same PIE, otherwise UE's UObject FName for the previous (just-
    # destroyed but not-yet-GC'd) ghost still occupies the name and
    # SetActorName -> Actor->Rename() asserts -> UE crash. Loop.py passes
    # a wave-scoped prefix like "GhostW{wave_idx}".
    agent_names = [f"{name_prefix}_{i}" for i in range(n)]
    if not reuse_agents:
        for i, ep in enumerate(episodes):
            name = agent_names[i]
            x, y, z = ep.start_position.x, ep.start_position.y, spawn_z
            log.info("wave: spawning %s at (%.0f,%.0f,%.0f) for %s",
                     name, x, y, z, ep.episode_id)
            ucv.spawn_bp_asset(_HUMANOID_BP, name, location=(x, y, z),
                               auto_repair_collision=False)
            _configure_ghost_ucv(ucv, name, x, y, z)

        # Enable collision on all ghosts in one batch (single navmesh rebuild)
        _finalize_ghost_agents(ucv, agent_names)
    else:
        # Teleport existing agents to new start positions
        for i, ep in enumerate(episodes):
            name = agent_names[i]
            x, y, z = ep.start_position.x, ep.start_position.y, spawn_z
            try:
                ucv.send(f"vset /object/{name}/location {x} {y} {z}")
                ucv.send(f"vset /object/{name}/rotation 0 0 0")
                log.info("wave: teleported %s to (%.0f,%.0f,%.0f) for %s",
                         name, x, y, z, ep.episode_id)
            except Exception as exc:
                log.error("wave: teleport %s failed: %s", name, exc)
        # Destroy orphaned agents from a larger previous wave
        for j in range(n, 30):  # max possible agents from prior waves
            try:
                ucv.send(f"vset /object/{name_prefix}_{j}/destroy")
            except Exception:
                break  # no more agents to clean up

    slots: List[AgentSlot] = []
    for i, ep in enumerate(episodes):
        agent_name = agent_names[i]

        env = SimWorldNavEnv(
            ucv_client=ucv,
            mcp_client=mcp,
            agent_name=agent_name,
            # camera_id=None → env resolves dynamically at reset via
            # location match against vget /cameras.  Spawn-order index
            # is NOT stable: UnrealCV's sensor list starts with the
            # PlayerController's pawn (whichever ghost called
            # EnableController True LAST), so slot_idx → camera_id is
            # wrong in ghost-mode waves.
            camera_id=None,
            capture_rgb=capture_rgb,
            capture_depth=capture_depth,
            spawn_on_reset=False,   # already spawned as ghost
            ensure_pie=False,       # PIE already running
            spawn_z=spawn_z,
        )
        # Mark as already spawned so reset() doesn't re-spawn
        env._spawned = True

        ep_logger: Optional[EpisodeLogger] = None
        if batch_dir is not None:
            ep_logger = EpisodeLogger(
                # Per-episode name (not slot index) so resume doesn't clobber
                # previously-written summaries when a retry wave has fewer
                # episodes than the original run.
                run_name=f"ep_{ep.episode_id}_{agent_name}",
                root=str(batch_dir),
                save_frames=save_frames,
                annotate_frames=False,
                timestamp_dir=False,
                install_log_handler=False,  # batch-level handler already installed
                meta={
                    "episode_id": ep.episode_id,
                    "episode_idx": i,
                    "agent_name": agent_name,
                    "camera_id": i,
                    "task_type": getattr(ep, "task_type", "pointnav"),
                },
            )

        # Per-ghost memory view: private L1, shared L2/L3.  Falls back to
        # the shared object for backends that don't implement fork().
        slot_mem = mem.fork(f"ghost_{i}") if hasattr(mem, "fork") else mem

        slot = AgentSlot(
            idx=i,
            agent_name=agent_name,
            episode=ep,
            env=env,
            logger=ep_logger,
            mem_view=slot_mem,
        )
        # Inject strategy memory lessons into system prompt if available
        system_text = _NAV_SYSTEM_PROMPT
        if hasattr(slot_mem, "get_system_prompt_section"):
            section = slot_mem.get_system_prompt_section()
            if section:
                system_text += section
        slot.history = [LLMMessage.text("system", system_text)]
        slots.append(slot)

    # --- Reset all envs ---
    failed_slots = []
    for slot in slots:
        try:
            obs, info = slot.env.reset(slot.episode)
            slot.task_prompt = info.get("task_prompt", "")
            slot._obs = obs
            slot._info = info
            if slot.logger is not None:
                slot.logger.log_step(0, None, obs, 0.0, False, False, info)
        except Exception as exc:
            log.error("env.reset failed for %s (%s): %s",
                      slot.agent_name, slot.episode.episode_id, exc)
            slot.done = True
            slot.ended_reason = "reset_error"
            failed_slots.append(slot.idx)
    if failed_slots:
        log.warning("wave: %d agents failed to reset: %s", len(failed_slots), failed_slots)

    time.sleep(2)  # let cameras initialize

    # --- Step loop ---
    step_bar = tqdm(
        total=max_steps,
        desc=f"batch ({n} ghosts)",
        unit="step",
        leave=True,
    )
    from concurrent.futures import ThreadPoolExecutor
    _llm_pool = ThreadPoolExecutor(max_workers=max(1, n))

    for t in range(1, max_steps + 1):
        active = [s for s in slots if not s.done]
        if not active:
            break

        # Phase 1: build prompts + memory queries for all active agents (fast, local)
        for slot in active:
            slot.step = t
            obs = slot._obs
            info = slot._info
            info["task_prompt"] = slot.task_prompt

            user_text = _build_user_text(info, obs)

            recalled = slot.mem_view.query(user_text, k=5)
            if recalled:
                recalled_text = "\n".join(f"- {m}" for m in recalled)
                user_text = f"Relevant past experience:\n{recalled_text}\n\n{user_text}"

            no_image_client = getattr(llm, "name", "") == "claude-sdk"
            if no_image_client or image_kind == "none":
                slot.history.append(LLMMessage.text("user", user_text))
            elif image_kind == "rgb_depth":
                rgb = obs.get("rgb")
                depth_rgb = obs.get("depth_rgb")
                imgs = [im for im in (rgb, depth_rgb) if im is not None]
                caps = []
                if rgb is not None:
                    caps.append("[First-person RGB view]")
                if depth_rgb is not None:
                    caps.append("[Depth map — brighter = closer, darker = farther]")
                if imgs:
                    slot.history.append(
                        LLMMessage.user_with_images(user_text, imgs, caps)
                    )
                else:
                    slot.history.append(LLMMessage.text("user", user_text))
            else:
                img = obs.get("rgb") if image_kind == "rgb" else obs.get("depth_rgb")
                if img is not None:
                    slot.history.append(LLMMessage.user_with_image(user_text, img))
                else:
                    slot.history.append(LLMMessage.text("user", user_text))
            _strip_images(slot.history, vision_depth)
            from gym_env.runner import _truncate_history
            slot.history[:] = _truncate_history(slot.history, l1_keep=5, l2_keep=10)

        # Phase 2: parallel LLM calls for all active agents (vLLM batches)
        def _call_llm(slot):
            try:
                return slot, llm.chat(slot.history, nav_tool_schemas(), max_tokens=1024), None
            except Exception as exc:
                return slot, None, exc

        t_llm0 = time.time()
        llm_results = list(_llm_pool.map(_call_llm, active))
        dt_llm = time.time() - t_llm0
        log.info("[timing batch t=%d] llm_parallel=%.2fs  n_active=%d", t, dt_llm, len(active))

        # Phase 3a: send actions for every active slot (non-blocking).
        # All agents act concurrently in UE; we do one sleep for the
        # longest-running action, then finalize each sequentially.
        pending: List[Tuple[AgentSlot, Any, Any, float]] = []  # (slot, resp, tc, prev_d)
        max_wait = 0.0
        for slot, resp, exc in llm_results:
            info = slot._info
            if exc is not None:
                log.error("LLM error for %s: %s", slot.agent_name, exc)
                slot.ended_reason = "llm_error"
                slot.done = True
                continue

            if slot.logger is not None:
                slot.logger.log_llm(t, llm.name, resp)

            if not resp.tool_calls:
                log.info("%s: LLM returned no tool calls at t=%d",
                         slot.agent_name, t)
                slot.ended_reason = "no_tool_call"
                slot.done = True
                continue

            slot.history.append(LLMMessage(
                role="assistant",
                content=[{"type": "text", "text": resp.text or ""}],
                tool_calls=resp.tool_calls,
            ))

            # Only the first tool call is actioned per turn (matches the
            # prompt: "call exactly one tool").
            tc = resp.tool_calls[0]
            prev_d = info.get("distance_to_goal_cm")
            try:
                wait_s = slot.env.step_send(tc.to_action_dict())
            except Exception as exc:
                log.error("%s step_send %d action %s failed: %s",
                          slot.agent_name, t, tc.name, exc)
                slot.done = True
                slot.ended_reason = "step_error"
                continue
            if wait_s > max_wait:
                max_wait = wait_s
            pending.append((slot, resp, tc, prev_d))

        # Phase 3b: single wait — all agents' actions play out in parallel in UE.
        if pending:
            t_ue0 = time.time()
            time.sleep(max_wait)
            log.info("[timing batch t=%d] ue_wait=%.2fs  n_pending=%d", t, time.time() - t_ue0, len(pending))

        # Phase 3c: finalize each slot (read position, compute reward, build obs).
        t_fin0 = time.time()
        for slot, resp, tc, prev_d in pending:
            try:
                obs, reward, done, truncated, info = slot.env.step_finalize()
            except Exception as exc:
                log.error("%s step_finalize %d action %s failed: %s",
                          slot.agent_name, t, tc.name, exc)
                slot.done = True
                slot.ended_reason = "step_error"
                continue
            global_step += 1
            slot.cumulative_reward += float(reward)

            if slot.logger is not None:
                slot.logger.log_step(
                    t, tc.to_action_dict(), obs, reward, done, truncated, info,
                )

            slot.history.append(LLMMessage(
                role="tool",
                tool_call_id=tc.id,
                content=[{"type": "text", "text": (
                    f"reward={reward:+.3f} "
                    f"d_goal={info['distance_to_goal_cm']:.0f}cm"
                )}],
            ))

            new_d = info.get("distance_to_goal_cm")
            if prev_d is not None and new_d is not None:
                delta = prev_d - new_d
                step_record = (
                    f"t={t} {tc.name} d_goal:{prev_d:.0f}->{new_d:.0f}cm "
                    f"(delta={delta:+.0f}) reward={reward:+.3f}"
                )
            else:
                step_record = f"t={t} {tc.name} reward={reward:+.3f}"
            agent_trajectories[slot.idx].append(step_record)

            # Per-ghost L1 insert: private working memory for this slot.
            # Hierarchical backend reads these structured fields; simpler
            # backends (text/null) just append the raw record.
            try:
                bearing_rad = 0.0
                if "pointgoal_with_gps_compass" in obs:
                    _d, _bearing = obs["pointgoal_with_gps_compass"].tolist()
                    bearing_rad = _bearing
                slot.mem_view.insert(
                    step_record,
                    metadata={
                        "step": t,
                        "action": tc.name,
                        "bearing_deg": math.degrees(bearing_rad),
                        "distance_cm": float(new_d or 0.0),
                        "prev_distance_cm": float(prev_d or new_d or 0.0),
                        "reward": float(reward),
                        "yaw_deg": float(obs.get("agent_yaw_deg", 0.0)),
                    },
                )
            except Exception as exc:
                log.debug("%s: mem_view.insert failed: %s", slot.agent_name, exc)

            if wandb_run:
                import wandb
                wandb.log({
                    "batch/step_reward": float(reward),
                    "batch/cumulative_reward": float(slot.cumulative_reward),
                    "batch/distance_to_goal": float(info["distance_to_goal_cm"]),
                    "batch/action": tc.name,
                    "batch/agent": slot.agent_name,
                    "batch/episode_idx": slot.idx,
                    "batch/step": t,
                }, step=global_step)

            if done or truncated:
                slot.metrics = info.get("metrics", {}) or {}
                slot.ended_reason = "success" if done else "truncated"
                slot.done = True

            slot._obs = obs
            slot._info = info

        n_done = sum(1 for s in slots if s.done)
        dt_fin = time.time() - t_fin0
        log.info("[timing batch t=%d] finalize=%.2fs  done=%d/%d", t, dt_fin, n_done, n)
        step_bar.set_postfix(done=f"{n_done}/{n}")
        step_bar.update(1)
        log.info("batch t=%d: %d/%d done", t, n_done, n)
    step_bar.close()

    # --- Finalize timed-out slots: compute metrics from env state ---
    for slot in slots:
        if slot.metrics:
            continue
        try:
            final_metrics = slot.env._final_metrics(slot.env._last_xy)
        except Exception as exc:
            log.warning("%s: _final_metrics failed (%s)", slot.agent_name, exc)
            final_metrics = {}
        final_metrics.setdefault("cumulative_reward", slot.cumulative_reward)
        slot.metrics = final_metrics

    # --- End-of-episode memory update ---
    # Preferred path (hierarchical/ghost view): call end_episode() on each
    # slot's own L1 → folds into shared L2 under a lock.  Distill runs
    # ONCE at the end of the wave rather than per-ghost, amortizing the
    # LLM-based L3 refresh cost across the whole batch.
    #
    # Fallback path: older backends expose reflect()/reset() on the shared
    # object with no per-agent isolation.
    for slot in slots:
        view = slot.mem_view
        sr = float(slot.metrics.get("SR", 0) or 0)
        final_d = float(slot.metrics.get("distance_to_goal_cm", 0) or 0)
        try:
            if hasattr(view, "end_episode"):
                view.end_episode(
                    success=sr > 0,
                    total_steps=slot.step,
                    final_distance_cm=final_d,
                )
            elif hasattr(view, "reflect") and hasattr(view, "_trajectory"):
                view._trajectory = agent_trajectories[slot.idx]
                outcome = (
                    f"{'SUCCESS' if sr > 0 else 'FAILED'}: "
                    f"steps={slot.step}, reason={slot.ended_reason}, "
                    f"SR={sr:.0f}, SPL={slot.metrics.get('SPL', 0):.3f}"
                )
                view.reflect(outcome)
                view._trajectory = []
            elif hasattr(view, "reflect"):
                outcome = (
                    f"{'SUCCESS' if sr > 0 else 'FAILED'}: "
                    f"steps={slot.step}, reason={slot.ended_reason}, "
                    f"SR={sr:.0f}, SPL={slot.metrics.get('SPL', 0):.3f}"
                )
                view.reflect(outcome)
                if hasattr(view, "reset"):
                    view.reset()
        except Exception as exc:
            log.warning("%s: memory end_episode failed: %s", slot.agent_name, exc)

    # Wave-level distill: L2 → L3 once after all ghosts have folded in.
    if hasattr(mem, "distill_if_due"):
        try:
            mem.distill_if_due()
        except Exception as exc:
            log.warning("wave: memory distill_if_due failed: %s", exc)

    # --- Collect results, write per-episode summary, cleanup ---
    results = []
    for slot in slots:
        sr = float(slot.metrics.get("SR", 0) or 0)
        spl = float(slot.metrics.get("SPL", 0) or 0)
        softspl = float(slot.metrics.get("SoftSPL", 0) or 0)
        plr = float(slot.metrics.get("PLR", 0) or 0)
        ndtw = float(slot.metrics.get("nDTW", 0) or 0)
        cls = float(slot.metrics.get("CLS", 0) or 0)
        path_cm = float(slot.metrics.get("path_length_cm", 0) or 0)
        cum_r = float(slot.metrics.get("cumulative_reward", slot.cumulative_reward) or 0)

        log.info(
            "%s episode=%s SR=%.0f SPL=%.3f nDTW=%.3f CLS=%.3f steps=%d reason=%s",
            slot.agent_name, slot.episode.episode_id, sr, spl, ndtw, cls,
            slot.step, slot.ended_reason,
        )

        summary = {
            "episode_id": slot.episode.episode_id,
            "episode_idx": slot.idx,
            "agent_name": slot.agent_name,
            "SR": sr,
            "SPL": spl,
            "SoftSPL": softspl,
            "PLR": plr,
            "nDTW": ndtw,
            "CLS": cls,
            "steps": slot.step,
            "path_length_cm": path_cm,
            "cumulative_reward": cum_r,
            "ended_reason": slot.ended_reason,
            "metrics": slot.metrics,
        }

        if slot.logger is not None:
            try:
                slot.logger.log_summary(summary)
            except Exception as exc:
                log.warning("log_summary failed for %s: %s",
                            slot.agent_name, exc)
            slot.logger.close()

        # WandB per-episode rollup (one point per episode, keyed to final step)
        if wandb_run:
            import wandb
            wandb.log({
                "episode/SR": sr,
                "episode/SPL": spl,
                "episode/SoftSPL": softspl,
                "episode/PLR": plr,
                "episode/nDTW": ndtw,
                "episode/CLS": cls,
                "episode/steps": slot.step,
                "episode/path_length_cm": path_cm,
                "episode/cumulative_reward": cum_r,
                "episode/idx": slot.idx,
                "episode/ended_reason_code": {
                    "success": 0, "truncated": 1, "max_steps": 2,
                    "llm_error": 3, "no_tool_call": 4,
                }.get(slot.ended_reason, -1),
            }, step=global_step)

        results.append(summary)
        # Destroy ghost agent (unless caller wants to reuse)
        # NOTE: Actor->Destroy() only marks pending-kill; the UObject FName
        # lingers until UE's next GC tick. Reusing the same name in a
        # subsequent wave within ~1s will crash UE on Rename collision.
        # name_prefix should be unique per wave to avoid this.
        if not skip_destroy:
            try:
                ucv.send(f"vset /object/{slot.agent_name}/destroy")
            except Exception:
                pass

    return results, global_step


# ---------------------------------------------------------------------------
# Sequential runner — drop-in replacement for run_wave that uses ONE
# normal (non-ghost) agent and runs episodes one after another. Avoids
# the multi-camera resolution / ghost-collision-channel complexity of
# run_wave; pays a wall-clock cost (no LLM batching) but uses far less
# UE/GPU resource per epoch.
# ---------------------------------------------------------------------------

def run_sequential(
    ucv: UCVClient,
    mcp: Optional[MCPClient],
    llm: LLMClient,
    episodes: List[NavigationEpisode],
    *,
    max_steps: int = 40,
    vision_depth: int = 3,
    spawn_z: float = _DEFAULT_SPAWN_Z,
    memory: Optional[AgentMemory] = None,
    wandb_run=None,
    global_step: int = 0,
    batch_dir: Optional[Path] = None,
    save_frames: bool = False,
    capture_rgb: bool = True,
    capture_depth: bool = False,
    image_kind: str = "rgb",
    reuse_agents: bool = False,  # accepted for sig parity, ignored
    skip_destroy: bool = False,
    name_prefix: str = "SeqAgent",
) -> Tuple[List[Dict[str, Any]], int]:
    """Sequential normal-agent runner. ONE agent, ONE camera, episodes in series.

    Returns (list of summary dicts, updated global_step) — same schema
    as :func:`run_wave` so loop.py callers don't need changes.
    """
    from .runner import run_episode

    n = len(episodes)
    log.info("sequential: %d episodes, max_steps=%d, agent=%s",
             n, max_steps, name_prefix)
    mem = memory or NullMemory()

    # Defensive cleanup: destroy any leftover nav-agent actors from
    # prior runs. The env's auto-discover logic prefers any actor whose
    # name starts with "CoEvolveAgent", so a stale leftover from a
    # previous PIE session will hijack our fresh spawn and yield a
    # "vget_location -> 'error'" failure on the first reset.
    try:
        existing_objs = ucv.vget_objects() or []
        stale = [
            nm for nm in existing_objs
            if nm.startswith("CoEvolveAgent")
            or nm.startswith("SeqE")
            or nm.startswith("GhostE")
            or nm.startswith("GymNavAgent")
            or nm == name_prefix
        ]
        for nm in stale:
            try:
                ucv.send(f"vset /object/{nm}/destroy")
            except Exception:
                pass
        if stale:
            log.info("sequential: cleaned %d stale agent actor(s): %s",
                     len(stale), stale)
    except Exception as exc:
        log.warning("sequential: stale-agent cleanup failed: %s", exc)

    # Single env: spawns the agent on the FIRST reset(), then teleports
    # for subsequent episodes.
    env = SimWorldNavEnv(
        ucv_client=ucv,
        mcp_client=mcp,
        agent_name=name_prefix,
        camera_id=None,        # env resolves dynamically at reset
        capture_rgb=capture_rgb,
        capture_depth=capture_depth,
        spawn_on_reset=True,
        ensure_pie=False,      # PIE managed by loop.py
        spawn_z=spawn_z,
    )

    # Per-agent memory view: hierarchical backends fork private L1 + share L2/L3.
    seq_mem = mem.fork(name_prefix) if hasattr(mem, "fork") else mem

    results: List[Dict[str, Any]] = []
    for i, ep in enumerate(episodes):
        log.info(
            "sequential: episode %d/%d %s start=(%.0f,%.0f) goal=(%.0f,%.0f)",
            i + 1, n, ep.episode_id,
            ep.start_position.x, ep.start_position.y,
            ep.goal_position.x, ep.goal_position.y,
        )

        ep_logger = None
        if batch_dir is not None:
            ep_logger = EpisodeLogger(
                run_name=f"ep_{ep.episode_id}_{name_prefix}",
                root=str(batch_dir),
                save_frames=save_frames,
                annotate_frames=False,
                timestamp_dir=False,
                install_log_handler=False,
                meta={
                    "episode_id": ep.episode_id,
                    "episode_idx": i,
                    "agent_name": name_prefix,
                    "task_type": getattr(ep, "task_type", "pointnav"),
                },
            )

        ended_reason = "max_steps"
        metrics: Dict[str, Any] = {}
        steps_used = 0
        try:
            # NB: run_episode requires a non-None logger. Provide one even
            # when batch_dir is None to keep the contract simple.
            if ep_logger is None:
                ep_logger = EpisodeLogger(
                    run_name=f"ep_{ep.episode_id}_{name_prefix}",
                    save_frames=False,
                    annotate_frames=False,
                    timestamp_dir=False,
                    install_log_handler=False,
                )
            metrics = run_episode(
                env, llm, ep, ep_logger,
                memory=seq_mem,
                max_steps=max_steps,
                vision_history_depth=vision_depth,
            )
            steps_used = int(env.step_count or 0)
            sr_val = float(metrics.get("SR", 0) or 0)
            ended_reason = "success" if sr_val > 0 else "max_steps"
        except Exception as exc:
            log.error("sequential: episode %d (%s) failed: %s: %s",
                      i + 1, ep.episode_id, type(exc).__name__, exc, exc_info=True)
            ended_reason = "step_error"
            try:
                ucv.hard_reconnect()
            except Exception:
                pass

        sr = float(metrics.get("SR", 0) or 0)
        spl = float(metrics.get("SPL", 0) or 0)
        softspl = float(metrics.get("SoftSPL", 0) or 0)
        plr = float(metrics.get("PLR", 0) or 0)
        ndtw = float(metrics.get("nDTW", 0) or 0)
        cls_ = float(metrics.get("CLS", 0) or 0)
        path_cm = float(metrics.get("path_length_cm", 0) or 0)
        cum_r = float(metrics.get("cumulative_reward", 0) or 0)

        log.info(
            "sequential: %s ep=%s SR=%.0f SPL=%.3f steps=%d reason=%s",
            name_prefix, ep.episode_id, sr, spl, steps_used, ended_reason,
        )

        summary = {
            "episode_id": ep.episode_id,
            "episode_idx": i,
            "agent_name": name_prefix,
            "SR": sr,
            "SPL": spl,
            "SoftSPL": softspl,
            "PLR": plr,
            "nDTW": ndtw,
            "CLS": cls_,
            "steps": steps_used,
            "path_length_cm": path_cm,
            "cumulative_reward": cum_r,
            "ended_reason": ended_reason,
            "metrics": metrics,
        }
        results.append(summary)
        global_step += steps_used

        if wandb_run:
            import wandb
            wandb.log({
                "episode/SR": sr,
                "episode/SPL": spl,
                "episode/SoftSPL": softspl,
                "episode/PLR": plr,
                "episode/nDTW": ndtw,
                "episode/CLS": cls_,
                "episode/steps": steps_used,
                "episode/path_length_cm": path_cm,
                "episode/cumulative_reward": cum_r,
                "episode/idx": i,
                "episode/ended_reason_code": {
                    "success": 0, "truncated": 1, "max_steps": 2,
                    "llm_error": 3, "no_tool_call": 4, "step_error": 5,
                }.get(ended_reason, -1),
            }, step=global_step)

    # Wave-level distill (mirrors run_wave behaviour).
    if hasattr(mem, "distill_if_due"):
        try:
            mem.distill_if_due()
        except Exception as exc:
            log.warning("sequential: memory distill_if_due failed: %s", exc)

    if not skip_destroy:
        try:
            ucv.send(f"vset /object/{name_prefix}/destroy")
        except Exception:
            pass

    return results, global_step


# ---------------------------------------------------------------------------
# Single mode (normal agent, with trajectory)
# ---------------------------------------------------------------------------

def run_single(
    ucv: UCVClient,
    mcp: Optional[MCPClient],
    llm: LLMClient,
    episode: NavigationEpisode,
    *,
    max_steps: int = 40,
    vision_depth: int = 3,
    run_name: Optional[str] = None,
    spawn_z: float = _DEFAULT_SPAWN_Z,
    memory: Optional[AgentMemory] = None,
) -> Dict[str, Any]:
    """Run a single episode with normal (non-ghost) agent + trajectory saving."""
    from .runner import run_episode

    env = SimWorldNavEnv(
        ucv_client=ucv,
        mcp_client=mcp,
        agent_name="GymNavAgent_0",
        capture_rgb=True,
        spawn_z=spawn_z,
        ensure_pie=True,
    )
    logger = EpisodeLogger(
        run_name=run_name or f"single_{episode.episode_id}",
        save_frames=True,
        annotate_frames=True,
    )
    metrics = run_episode(
        env, llm, episode, logger,
        memory=memory or NullMemory(),
        max_steps=max_steps,
        vision_history_depth=vision_depth,
    )
    return metrics


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m gym_env.batch_runner",
        description="Batch or single episode runner with ghost-mode support.",
    )
    p.add_argument("--mode", choices=["batch", "single"], default="batch",
                   help="batch = ghost agents; single = normal agent, save traj")
    p.add_argument("--n-tasks", type=int, default=4,
                   help=(
                       "Number of concurrent ghost agents (and episodes) "
                       "to run in this UE instance.  One UE instance runs "
                       "exactly one wave — spawn more UE instances for "
                       "more concurrency."
                   ))
    # Kept for back-compat; if supplied, must equal --n-tasks.
    p.add_argument("--wave-size", type=int, default=None,
                   help="Deprecated alias for --n-tasks (must equal --n-tasks if set).")
    # LLM
    p.add_argument("--model", default="claude")
    p.add_argument("--model-id", default=None)
    p.add_argument("--base-url", default=None)
    p.add_argument("--api-key", default=None)
    # UE connection — defaults honour env vars so one `export` can
    # flip an entire shell session to a different UE instance.
    import os as _os
    p.add_argument("--ucv-host", default=_os.environ.get("UNREALCV_HOST", "127.0.0.1"))
    p.add_argument("--ucv-port", type=int,
                   default=int(_os.environ.get("UNREALCV_PORT", "9002")))
    p.add_argument("--mcp-host", default=_os.environ.get("UNREAL_MCP_HOST", "127.0.0.1"))
    p.add_argument("--mcp-port", type=int,
                   default=int(_os.environ.get("UNREAL_MCP_PORT", "55558")))
    # Episode generation
    p.add_argument("--scene-graph", default=None)
    p.add_argument("--episodes-file", default=None,
                   help=(
                       "Load pre-generated episodes from a JSON file instead "
                       "of sampling them at runtime. Accepts either a split "
                       "file (dict with 'episodes' list, as produced by "
                       "`python -m nav_task --split ...`) or a raw list / "
                       "single-episode dict from `--output`. When set, the "
                       "runner skips navmesh build and episode sampling — "
                       "all path/geodesic info is read from the file. "
                       "--n-tasks selects how many episodes from the file "
                       "to run (must be <= file size)."
                   ))
    p.add_argument("--nav-min-cm", type=float, default=1000.0)
    p.add_argument("--nav-max-cm", type=float, default=4000.0)
    p.add_argument("--seed", type=int, default=42)
    p.add_argument("--max-steps", type=int, default=40)
    p.add_argument("--vision-depth", type=int, default=3)
    # Trajectory recording — batch defaults to no frames (fast eval),
    # single-mode always keeps frames on.
    p.add_argument("--save-frames", action="store_true", default=False,
                   help="Save RGB frames per step (default off in batch mode; "
                        "single mode always saves).")
    p.add_argument("--no-rgb", action="store_true", default=False,
                   help="Disable RGB capture entirely (faster, smaller logs).")
    # Memory
    p.add_argument("--memory", default="none",
                   choices=["none", "text", "mem0", "strategy", "hierarchical"],
                   help="Memory backend: none, text, mem0, strategy, or hierarchical")
    p.add_argument("--eval-mode", default="train",
                   choices=["train", "test"],
                   dest="eval_mode",
                   help=(
                       "train: memory is read-write (insert + query). "
                       "test: memory is read-only (query only, no insert). "
                       "Use 'test' for deterministic evaluation with frozen "
                       "memories from a prior training run."
                   ))
    # WandB
    p.add_argument("--wandb-project", default="simworld-nav")
    p.add_argument("--wandb-key", default=None,
                   help="WandB API key (or set WANDB_API_KEY env var)")
    p.add_argument("--no-wandb", action="store_true",
                   help="Disable WandB logging")
    # Misc
    p.add_argument("--run-name", default=None)
    p.add_argument("--log-level", default="INFO")
    return p


def main(argv: Optional[List[str]] = None) -> None:
    args = _build_parser().parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-5s %(name)s | %(message)s",
    )

    # --wave-size is a deprecated alias for --n-tasks.  One UE instance
    # runs exactly one wave; to run more ghost agents you launch another
    # UE instance.
    if args.wave_size is not None and args.wave_size != args.n_tasks:
        log.warning(
            "--wave-size=%d ignored — one UE instance runs one wave. "
            "Setting --n-tasks=%d is what controls concurrency.",
            args.wave_size, args.n_tasks,
        )

    from .episode_builder import (
        sample_pointnav_episode,
        sample_pointnav_episode_navmesh,
    )

    mcp = MCPClient(host=args.mcp_host, port=args.mcp_port, name="batch-mcp")

    # Start PIE.  Need a longer wait when --scene-graph is set because
    # the navmesh plugin's game-world binding takes ~10s after PIE
    # transition to be queryable; 5s is enough for spawn_bp_asset but
    # not for `vset /nav/build`.
    pie_wait = 12.0 if args.scene_graph else 5.0
    try:
        mcp.start_pie(wait_seconds=pie_wait)
    except Exception as exc:
        log.warning("PIE auto-start failed (%s); proceeding anyway", exc)

    ucv = UCVClient(host=args.ucv_host, port=args.ucv_port, name="batch-0")
    for attempt in range(15):
        try:
            ucv.connect()
            break
        except Exception:
            log.info("waiting for UnrealCV... (attempt %d)", attempt + 1)
            time.sleep(2)
    else:
        raise RuntimeError("UnrealCV not available after PIE start")

    llm = make_llm(
        args.model,
        model=args.model_id,
        base_url=args.base_url,
        api_key=args.api_key,
    )

    # Build memory
    memory = build_memory(
        args.memory,
        agent_id="batch_agent",
        llm_model=args.model_id,
        llm_base_url=args.base_url,
        llm_api_key=args.api_key,
    )
    if args.eval_mode == "test":
        memory = ReadOnlyMemory(memory)
    log.info("memory backend: %s (eval_mode=%s)", getattr(memory, 'name', type(memory).__name__), args.eval_mode)

    # WandB
    import os
    wandb_run = None
    if not args.no_wandb:
        if args.wandb_key:
            os.environ["WANDB_API_KEY"] = args.wandb_key
        if os.environ.get("WANDB_API_KEY"):
            try:
                wandb_run = _init_wandb(args, [])
                log.info("WandB run initialized: %s", wandb_run.name)
            except Exception as exc:
                log.warning("WandB init failed (%s); continuing without", exc)
        else:
            log.info("WandB disabled (no API key)")

    # Episode source: either load from a pre-generated file (deterministic,
    # skips live navmesh build) or sample at runtime via UE navmesh.
    episodes: List[NavigationEpisode] = []
    if args.episodes_file:
        log.info("Loading episodes from %s (skipping navmesh build + sampling)",
                 args.episodes_file)
        episodes = _load_episodes_file(args.episodes_file)
        if args.n_tasks > len(episodes):
            raise RuntimeError(
                f"--n-tasks={args.n_tasks} but {args.episodes_file} only "
                f"contains {len(episodes)} episode(s)"
            )
        episodes = episodes[:args.n_tasks]
        for i, ep in enumerate(episodes):
            log.info("  episode %d: %s", i, ep.episode_id)
    else:
        # Build navmesh once, then generate all episodes.  The plugin's
        # game-world binding is racy right after PIE starts — retry on
        # "No game world available" or any error containing PIE-start hints.
        nav_interface = None
        if args.scene_graph:
            from nav_task.navmesh_interface import NavmeshNavigationInterface
            nav_interface = NavmeshNavigationInterface(ucv)
            for attempt in range(6):
                resp = nav_interface.build_navmesh()
                log.info("navmesh build attempt %d: %s", attempt + 1, resp)
                if "error" not in resp.lower():
                    break
                log.warning("navmesh build returned error; sleeping 3s and retrying")
                time.sleep(3.0)
            else:
                raise RuntimeError(
                    f"navmesh build failed after 6 attempts; last response: {resp}"
                )

        log.info("Generating %d pointnav episodes (seed=%d)", args.n_tasks, args.seed)
        for i in range(args.n_tasks):
            seed = args.seed + i
            if args.scene_graph:
                result = sample_pointnav_episode_navmesh(
                    ucv,
                    seed=seed, idx=i,
                    min_geodesic_cm=args.nav_min_cm,
                    max_geodesic_cm=args.nav_max_cm,
                    build_navmesh=False,
                    nav_interface=nav_interface,
                )
                episodes.append(result["episode"])
                log.info("  episode %d: %s", i, result["episode"].episode_id)
            else:
                ep = sample_pointnav_episode(ucv, seed=seed, idx=i)
                episodes.append(ep)
                log.info("  episode %d: %s", i, ep.episode_id)

    # --- Run ---
    if args.mode == "single":
        log.info("=== SINGLE MODE: 1 episode, normal agent, trajectory saved ===")
        metrics = run_single(
            ucv, mcp, llm, episodes[0],
            max_steps=args.max_steps,
            vision_depth=args.vision_depth,
            run_name=args.run_name,
            memory=memory,
        )
        print(f"\n[SINGLE] {metrics}")
    else:
        # Create a single batch-level directory housing one subdir per
        # episode.  Install ONE FileHandler here so the whole batch run
        # is captured in ``batch_run.log`` — per-episode EpisodeLoggers
        # are created with ``install_log_handler=False`` to avoid
        # duplicating every log record N times.
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        batch_run_id = f"batch_{ts}_{args.run_name or 'results'}"
        batch_dir = Path("runs") / batch_run_id
        batch_dir.mkdir(parents=True, exist_ok=True)

        batch_log_path = batch_dir / "batch_run.log"
        batch_fh = logging.FileHandler(batch_log_path, encoding="utf-8")
        batch_fh.setLevel(logging.DEBUG)
        batch_fh.setFormatter(logging.Formatter(
            "%(asctime)s %(levelname)-5s %(name)s | %(message)s"
        ))
        logging.getLogger().addHandler(batch_fh)

        def _git_sha() -> Optional[str]:
            import subprocess
            try:
                return subprocess.check_output(
                    ["git", "rev-parse", "HEAD"],
                    stderr=subprocess.DEVNULL, text=True,
                ).strip()
            except Exception:
                return None

        batch_meta = {
            "batch_run_id": batch_run_id,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "git_sha": _git_sha(),
            "mode": args.mode,
            "model": args.model,
            "model_id": args.model_id,
            "memory": args.memory,
            "eval_mode": args.eval_mode,
            "n_tasks": args.n_tasks,
            "max_steps": args.max_steps,
            "vision_depth": args.vision_depth,
            "seed": args.seed,
            "ucv_port": args.ucv_port,
            "mcp_port": args.mcp_port,
            "nav_min_cm": args.nav_min_cm,
            "nav_max_cm": args.nav_max_cm,
            "scene_graph": args.scene_graph,
            "episodes_file": args.episodes_file,
            "save_frames": args.save_frames,
            "capture_rgb": not args.no_rgb,
            "run_name": args.run_name,
            "wandb_project": args.wandb_project if not args.no_wandb else None,
            "episode_ids": [ep.episode_id for ep in episodes],
        }
        (batch_dir / "batch_meta.json").write_text(
            json.dumps(batch_meta, indent=2), encoding="utf-8"
        )
        log.info("=== BATCH MODE: %d concurrent ghost agents in one UE instance ===",
                 args.n_tasks)
        log.info("batch output dir: %s", batch_dir)

        # One UE instance runs exactly one wave.  No loop — if you want
        # more concurrency, spawn more UE instances (see run_batch.sh).
        global_step = 0
        try:
            all_results, global_step = run_wave(
                ucv, mcp, llm, episodes,
                max_steps=args.max_steps,
                vision_depth=args.vision_depth,
                memory=memory,
                wandb_run=wandb_run,
                global_step=global_step,
                batch_dir=batch_dir,
                save_frames=args.save_frames,
                capture_rgb=not args.no_rgb,
            )
        except Exception:
            log.exception("batch crashed")
            raise

        # Batch summary
        n = len(all_results)
        n_success = sum(1 for r in all_results if r.get("SR", 0) > 0)
        avg_sr = n_success / n if n else 0.0
        avg_spl = sum(r.get("SPL", 0) for r in all_results) / n if n else 0.0
        avg_softspl = sum(r.get("SoftSPL", 0) for r in all_results) / n if n else 0.0
        avg_cum_r = sum(r.get("cumulative_reward", 0) for r in all_results) / n if n else 0.0
        avg_path = sum(r.get("path_length_cm", 0) for r in all_results) / n if n else 0.0

        batch_summary = {
            "batch_run_id": batch_run_id,
            "finished_at": datetime.now(timezone.utc).isoformat(),
            "n_episodes": n,
            "n_success": n_success,
            "SR": avg_sr,
            "SPL": avg_spl,
            "SoftSPL": avg_softspl,
            "cumulative_reward_mean": avg_cum_r,
            "path_length_cm_mean": avg_path,
            "episodes": all_results,
        }
        (batch_dir / "batch_summary.json").write_text(
            json.dumps(batch_summary, indent=2), encoding="utf-8"
        )

        # Keep the legacy flat JSON for back-compat with analyze_runs.
        legacy_path = Path("runs") / f"batch_{args.run_name or 'results'}.json"
        legacy_path.parent.mkdir(parents=True, exist_ok=True)
        with open(legacy_path, "w") as f:
            json.dump(all_results, f, indent=2)

        print(f"\n{'='*50}")
        print(f"BATCH RESULTS: {n} episodes")
        print(f"  SR:      {n_success}/{n} ({100*avg_sr:.0f}%)")
        print(f"  SPL:     {avg_spl:.3f}")
        print(f"  SoftSPL: {avg_softspl:.3f}")
        print(f"  cum_r:   {avg_cum_r:+.1f} avg")
        print(f"{'='*50}")
        for r in all_results:
            print(
                f"  {r['episode_id']}: SR={r['SR']:.0f} "
                f"SPL={r['SPL']:.3f} steps={r['steps']} "
                f"cum_r={r['cumulative_reward']:+.1f} "
                f"end={r['ended_reason']}"
            )
        log.info("batch summary written to %s", batch_dir / "batch_summary.json")

        # WandB final summary
        if wandb_run:
            import wandb
            wandb.log({
                "batch/SR": avg_sr,
                "batch/SPL": avg_spl,
                "batch/SoftSPL": avg_softspl,
                "batch/cumulative_reward_mean": avg_cum_r,
                "batch/path_length_cm_mean": avg_path,
                "batch/n_episodes": n,
                "batch/n_success": n_success,
            })

        # Remove the batch-level file handler before we exit.
        try:
            logging.getLogger().removeHandler(batch_fh)
            batch_fh.close()
        except Exception:
            pass

    ucv.disconnect()
    if wandb_run:
        import wandb
        wandb.finish()


if __name__ == "__main__":
    main()
