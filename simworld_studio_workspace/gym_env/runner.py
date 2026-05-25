"""Episode loop: feed observations to the LLM, send chosen actions to env.

Vision history truncation
-------------------------
Per-step RGB images blow up token budgets fast.  By default we keep
only the **last K** observation images in the chat history (K=3); older
turns retain only their text summary.  Disable by passing
``vision_history_depth=None`` to keep all frames.

Loop control
------------
The loop terminates when the env reports ``done`` / ``truncated``, OR
when the LLM returns a turn with no tool calls (the model gives up /
emits a final text answer).
"""

from __future__ import annotations

import argparse
import logging
import math
import sys
import time
from typing import Any, Dict, List, Optional

from nav_task.episode import NavigationEpisode
from nav_task.task_spec import make_task_prompt

from .action_space import nav_tool_schemas
from .llm import LLMClient, LLMMessage, ToolCall, make_llm
from .logger import EpisodeLogger
from .memory import AgentMemory, NullMemory, build_memory
from .simworld_nav_env import SimWorldNavEnv

log = logging.getLogger(__name__)


_NAV_SYSTEM_PROMPT = """You are an embodied navigation agent in a 3D city scene.

You receive a first-person RGB image and a goal description.  Choose
ONE navigation action per turn from this set:

  - MOVE_FORWARD : walk forward roughly 2 seconds (~200-400 cm)
  - TURN_LEFT    : rotate 30 degrees left
  - TURN_RIGHT   : rotate 30 degrees right
  - STOP         : declare you have reached the goal

Each step you receive the bearing to the goal (in degrees) and the
distance.  The sign convention of bearing is NOT stated — discover
it by observing how your TURN actions change it, then remember
which sign means "goal to my right" and which means "left".  If you
notice a flip-flop pattern (turning and bearing sign alternates),
commit to one turn direction for several consecutive steps until
|bearing| clearly decreases toward 0.

STOP when distance < 200 cm.

Think briefly, then call exactly one tool."""


def _build_user_text(info: Dict[str, Any], obs: Dict[str, Any]) -> str:
    parts: List[str] = []
    parts.append(f"Task: {info.get('task_prompt', '')}")
    if "pointgoal_with_gps_compass" in obs:
        d, ang = obs["pointgoal_with_gps_compass"].tolist()
        parts.append(
            f"Goal: distance={d:.0f} cm, bearing={math.degrees(ang):+.0f} deg"
        )
    elif "objectgoal" in obs:
        cat = info.get("task_prompt", "")
        parts.append(f"Goal: find a {cat}")
    parts.append(
        f"Position: ({obs['agent_xy'][0]:.0f}, {obs['agent_xy'][1]:.0f})"
        f"  yaw={obs['agent_yaw_deg']:+.0f} deg"
    )
    parts.append(f"Step: {info['step']}")
    return "\n".join(parts)


def _strip_images(messages: List[LLMMessage], keep_last_k: int) -> None:
    """Mutate ``messages`` in-place: keep images only on the last K user turns."""
    if keep_last_k is None:
        return
    user_turns = [
        i for i, m in enumerate(messages)
        if m.role == "user" and any(b["type"] == "image" for b in m.content)
    ]
    if len(user_turns) <= keep_last_k:
        return
    drop = user_turns[:-keep_last_k]
    for i in drop:
        messages[i].content = [
            b if b["type"] == "text" else {"type": "text", "text": "[image omitted]"}
            for b in messages[i].content
        ]


def _truncate_history(
    messages: List[LLMMessage],
    l1_keep: int = 5,
    l2_keep: int = 10,
) -> List[LLMMessage]:
    """Truncate chat history to keep tokens manageable.

    Three tiers:
    - L3 (system):    all system messages, always kept.
    - L1 (recent):    last ``l1_keep`` turns (user+assistant+tool) in full.
    - L2 (older):     next ``l2_keep`` turns before L1, with user observation
                      compressed to a single compact line (no image).
    - Older:          dropped.

    A "turn" is a user message plus the assistant and tool messages that
    follow it up to the next user message.
    """
    system_msgs = [m for m in messages if m.role == "system"]
    other_msgs  = [m for m in messages if m.role != "system"]

    # Group into turns: each group starts at a user message.
    turns: List[List[LLMMessage]] = []
    current: List[LLMMessage] = []
    for m in other_msgs:
        if m.role == "user" and current:
            turns.append(current)
            current = [m]
        else:
            current.append(m)
    if current:
        turns.append(current)

    # Keep last (l1_keep + l2_keep) turns.
    total_keep = l1_keep + l2_keep
    turns = turns[-total_keep:]

    result = list(system_msgs)
    for i, turn in enumerate(turns):
        idx_from_end = len(turns) - i  # 1 = most recent turn
        if idx_from_end <= l1_keep:
            # L1: keep turn as-is (images already handled by _strip_images)
            result.extend(turn)
        else:
            # L2: compress user message to one line, drop image blocks.
            for m in turn:
                if m.role == "user":
                    text = ""
                    for b in m.content:
                        if isinstance(b, dict) and b.get("type") == "text":
                            text = b.get("text", "")
                            break
                    lines = [
                        ln for ln in text.splitlines()
                        if ln.startswith(("Step:", "Goal:", "Position:"))
                    ]
                    compact = " | ".join(lines) if lines else "[obs]"
                    result.append(LLMMessage.text("user", compact))
                else:
                    result.append(m)
    return result


def run_episode(
    env: SimWorldNavEnv,
    llm: LLMClient,
    episode: NavigationEpisode,
    logger: EpisodeLogger,
    *,
    memory: Optional[AgentMemory] = None,
    max_steps: Optional[int] = None,
    vision_history_depth: Optional[int] = 1,
    max_tokens: int = 1024,
    task_prompt_override: Optional[str] = None,
    history_l1: int = 5,
    history_l2: int = 10,
) -> Dict[str, Any]:
    """Run one episode end-to-end.  Returns the final metrics dict.

    ``task_prompt_override`` — if provided, replaces the default
    ``info['task_prompt']`` that the env generates from the
    NavigationEpisode.  Used by ``objectnav-search`` to inject the
    LLM-generated landmark-relative hint.
    """
    obs, info = env.reset(episode)
    if task_prompt_override is not None:
        info["task_prompt"] = task_prompt_override
    logger.log_step(0, None, obs, 0.0, False, False, info)

    if max_steps is None:
        max_steps = episode.success_criteria.max_steps

    memory = memory or NullMemory()
    memory.reset()

    # Build system prompt: base instructions + L3 skills (if hierarchical)
    system_text = _NAV_SYSTEM_PROMPT
    if hasattr(memory, "get_system_prompt_section"):
        l3_section = memory.get_system_prompt_section()
        if l3_section:
            system_text += l3_section

    history: List[LLMMessage] = [
        LLMMessage.text("system", system_text),
    ]
    recent_actions: List[str] = []
    recent_distances: List[float] = []

    final_metrics: Dict[str, Any] = {}
    for t in range(1, max_steps + 1):
        # Keep the task prompt override stable across env.step calls
        # (env regenerates task_prompt each step from the episode).
        if task_prompt_override is not None:
            info["task_prompt"] = task_prompt_override
        user_text = _build_user_text(info, obs)

        # Rethink check: detect failure patterns (oscillation/stuck/backtrack).
        # Only reports the observation — lets the agent reason about what to do.
        rethink_text = ""
        if hasattr(memory, "check_rethink"):
            rethink = memory.check_rethink()
            if rethink:
                log.warning("[runner t=%d] rethink triggered: %s", t, rethink.reason)
                rethink_text = rethink.prompt + "\n\n"

        # Memory recall: ask the backend for relevant past items and
        # prepend them to the user turn as plain text.  NullMemory
        # returns []; cost is zero when disabled.
        recalled = memory.query(user_text, k=5)
        if recalled:
            memo_block = "Relevant past experience:\n" + "\n".join(
                f"- {m}" for m in recalled
            )
            user_text = memo_block + "\n\n" + user_text

        # Prepend rethink observation if triggered
        if rethink_text:
            user_text = rethink_text + user_text

        # Soft warning only: detect turn oscillation and provide context to LLM.
        # Do NOT override the model action in code.
        if len(recent_actions) >= 6 and len(recent_distances) >= 4:
            last6 = recent_actions[-6:]
            turn_only = all(a in ("TURN_LEFT", "TURN_RIGHT") for a in last6)
            alternates = all(last6[i] != last6[i + 1] for i in range(len(last6) - 1))
            dist_span = max(recent_distances[-4:]) - min(recent_distances[-4:])
            if turn_only and alternates and dist_span < 30.0:
                user_text = (
                    "Rethink hint: You appear to be oscillating between left/right turns "
                    "without reducing distance. Choose an action that makes forward progress.\n\n"
                    + user_text
                )

        # Attach image only if RGB capture is on AND the LLM actually
        # consumes images.  ClaudeAgentSDKClient is text-only.
        rgb = obs.get("rgb")
        if rgb is not None and getattr(llm, "name", "") != "claude-sdk":
            history.append(LLMMessage.user_with_image(user_text, rgb))
        else:
            history.append(LLMMessage.text("user", user_text))
        _strip_images(history, vision_history_depth)
        history[:] = _truncate_history(history, l1_keep=history_l1, l2_keep=history_l2)

        t_step_start = time.time()
        log.info("[runner t=%d] querying %s  history_msgs=%d", t, llm.name, len(history))
        t_llm0 = time.time()
        resp = llm.chat(history, nav_tool_schemas(), max_tokens=max_tokens)
        dt_llm = time.time() - t_llm0
        log.info("[timing t=%d] llm=%.2fs", t, dt_llm)
        logger.log_llm(t, llm.name, resp)

        # Echo to stdout for live debugging
        thought = (resp.text or "").strip().splitlines()
        if thought:
            print(f"[t={t}] {llm.name} thought: {thought[0][:120]}")
        for tc in resp.tool_calls:
            print(f"[t={t}] action -> {tc.name}")

        if not resp.tool_calls:
            preview = (resp.text or "").strip().replace("\n", " ")[:200]
            raise RuntimeError(
                f"LLM response did not contain a valid action at step {t}. "
                f"response_preview={preview!r}"
            )

        history.append(LLMMessage(
            role="assistant",
            content=[{"type": "text", "text": resp.text or ""}],
            tool_calls=resp.tool_calls,
        ))

        done_now = False
        for tc in resp.tool_calls:
            prev_d = info.get("distance_to_goal_cm")
            action_dict = tc.to_action_dict()
            executed_action = action_dict.get("tool") or action_dict.get("name") or tc.name

            t_env0 = time.time()
            obs, reward, done, truncated, info = env.step(action_dict)
            dt_env = time.time() - t_env0
            dt_step = time.time() - t_step_start
            log.info("[timing t=%d] env_step=%.2fs  total=%.2fs", t, dt_env, dt_step)
            logger.log_step(t, action_dict, obs, reward, done, truncated, info)
            history.append(LLMMessage(
                role="tool",
                tool_call_id=tc.id,
                content=[{
                    "type": "text",
                    "text": (
                        f"reward={reward:+.3f} "
                        f"d_goal={info['distance_to_goal_cm']:.0f}cm "
                        f"pos=({info['agent_xy'][0]:.0f},{info['agent_xy'][1]:.0f})"
                    ),
                }],
            ))

            # Memory insert: record what we tried and what happened.
            new_d = info.get("distance_to_goal_cm")
            delta = (prev_d - new_d) if (prev_d is not None and new_d is not None) else 0.0

            # Extract bearing for hierarchical memory
            bearing_deg = 0.0
            if "pointgoal_with_gps_compass" in obs:
                _, ang_rad = obs["pointgoal_with_gps_compass"].tolist()
                bearing_deg = math.degrees(ang_rad)

            memory.insert(
                (
                    f"step={t} action={executed_action} reward={reward:+.3f} "
                    f"d_goal {prev_d:.0f}->{new_d:.0f}cm (delta={delta:+.0f}) "
                    f"yaw={obs['agent_yaw_deg']:+.0f}"
                ),
                metadata={
                    "step": t,
                    "action": executed_action,
                    "reward": float(reward),
                    "d_goal_cm": float(new_d) if new_d is not None else None,
                    "delta_cm": float(delta),
                    "done": bool(done),
                    "bearing_deg": bearing_deg,
                    "distance_cm": float(new_d) if new_d is not None else 0.0,
                    "prev_distance_cm": float(prev_d) if prev_d is not None else 0.0,
                    "yaw_deg": float(obs.get("agent_yaw_deg", 0)),
                },
            )
            recent_actions.append(executed_action)
            if new_d is not None:
                recent_distances.append(float(new_d))

            if done or truncated:
                final_metrics = info.get("metrics", {}) or {}
                done_now = True
                break
        if done_now:
            break

    # End-of-episode memory: insert a concise lesson learned.
    # Tag these with event_type="episode_summary" so structured
    # backends (hierarchical) can skip them from the per-step SAO
    # extraction — they aren't single-step records.
    sr = final_metrics.get("SR", 0)
    path_cm = final_metrics.get("path_length_cm", 0)
    cum_r = final_metrics.get("cumulative_reward", 0)
    if sr > 0:
        memory.insert(
            f"EPISODE SUCCESS: reached goal in {env.step_count} steps, "
            f"path={path_cm:.0f}cm, reward={cum_r:+.0f}. "
            f"Strategy: align bearing to ~0 then MOVE_FORWARD repeatedly.",
            metadata={"event_type": "episode_summary", "success": True},
        )
    else:
        if env.step_count >= (max_steps or 999):
            memory.insert(
                f"EPISODE FAIL: ran out of steps ({env.step_count}). "
                f"d_goal={info.get('distance_to_goal_cm', '?')}cm still far. "
                f"Lesson: don't turn more than 2-3 times in a row; "
                f"switch to MOVE_FORWARD once bearing is within ±45°. "
                f"Use STOP when distance < 200cm.",
                metadata={"event_type": "episode_summary", "success": False},
            )
        else:
            memory.insert(
                f"EPISODE FAIL: d_goal={info.get('distance_to_goal_cm', '?')}cm. "
                f"reward={cum_r:+.0f}.",
                metadata={"event_type": "episode_summary", "success": False},
            )

    # Flush this episode's L1 into L2 (for backends that support it).
    # Without this, the last episode's steps are lost because compaction
    # normally fires at the START of the next reset().
    if hasattr(memory, "end_episode"):
        memory.end_episode(
            success=bool(sr > 0),
            total_steps=env.step_count,
            final_distance_cm=float(info.get("distance_to_goal_cm", 0) or 0),
        )

    logger.log_summary(final_metrics or {"note": "loop exited without done"})
    return final_metrics


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m gym_env.runner",
        description="Run a single nav episode with a chosen LLM.",
    )
    p.add_argument("--model", default="claude",
                   help="LLM short name: claude / claude-sdk / gpt / gemini / qwen")
    p.add_argument("--model-id", default=None,
                   help="Override the model id (e.g. 'Qwen/Qwen3-VL-30B-A3B-Instruct')")
    p.add_argument("--base-url", default=None,
                   help="Override the LLM base URL (e.g. 'http://host:8000/v1')")
    p.add_argument("--api-key", default=None,
                   help="Override the API key for the LLM endpoint")
    p.add_argument("--n-episodes", type=int, default=1,
                   help="Number of episodes to run back-to-back (multi-episode experiment)")
    # UE connection — defaults honour env vars (UNREALCV_HOST/PORT,
    # UNREAL_MCP_HOST/PORT) so one `export` reroutes an entire session.
    import os as _os
    p.add_argument("--ucv-host", default=_os.environ.get("UNREALCV_HOST", "127.0.0.1"))
    p.add_argument("--ucv-port", type=int,
                   default=int(_os.environ.get("UNREALCV_PORT", "9002")))
    p.add_argument("--mcp-host", default=_os.environ.get("UNREAL_MCP_HOST", "127.0.0.1"))
    p.add_argument("--mcp-port", type=int,
                   default=int(_os.environ.get("UNREAL_MCP_PORT", "55558")),
                   help="UE editor MCP TCP port (used to start PIE)")
    p.add_argument("--no-start-pie", action="store_true",
                   help="Skip the auto PIE-start on first reset (assume PIE is already running)")
    p.add_argument("--agent-name", default="GymNavAgent_0")
    p.add_argument("--task",
                   choices=["pointnav", "objectnav", "objectnav-search"],
                   default="pointnav")
    p.add_argument("--target-distance", type=float, default=2000.0,
                   help="Target distance in cm (PointNav)")
    p.add_argument("--target-filter", default=None,
                   help="Substring an actor name must contain (ObjectNav)")
    p.add_argument("--object-category", default="OBJECT")
    p.add_argument("--n-search-targets", type=int, default=5,
                   help="How many small objects to spawn for "
                        "objectnav-search (one episode per target).")
    p.add_argument("--describer-model", default=None,
                   help="LLM short name used to generate the natural-"
                        "language target descriptions.  Defaults to the "
                        "same model as --model.")
    p.add_argument("--describer-model-id", default=None)
    p.add_argument("--describer-base-url", default=None)
    p.add_argument("--describer-api-key", default=None)
    p.add_argument("--scene-graph", default=None,
                   help="Path to scene_graph.json used for NavMesh "
                        "task generation. If omitted, falls back to "
                        "legacy origin-based random-goal sampling.")
    p.add_argument("--nav-min-cm", type=float, default=1000.0,
                   help="Min geodesic distance (cm) for NavMesh PointNav")
    p.add_argument("--nav-max-cm", type=float, default=4000.0,
                   help="Max geodesic distance (cm) for NavMesh PointNav")
    p.add_argument("--seed", type=int, default=42)
    p.add_argument("--max-steps", type=int, default=40)
    p.add_argument("--vision-depth", type=int, default=3,
                   help="How many recent frames to keep in LLM history")
    p.add_argument("--run-name", default=None)
    p.add_argument("--no-rgb", action="store_true",
                   help="Disable RGB capture (text-only ablation). "
                        "Ignored when --record-trajectory is set.")
    p.add_argument("--record-trajectory", action="store_true",
                   help="Save annotated PNG frames of every step under "
                        "runs/<id>/frames/. Forces RGB capture on, even "
                        "for text-only LLMs (claude-sdk).")
    p.add_argument("--memory", default="none",
                   choices=["none", "text", "mem0", "hierarchical"],
                   help="Agent memory backend. 'none' disables memory. "
                        "'text' uses a simple JSON file (no extra deps). "
                        "'mem0' uses mem0ai (pip install mem0ai). "
                        "'hierarchical' uses 3-level memory (L1/L2/L3).")
    p.add_argument("--log-level", default="INFO")
    return p


def main(argv: Optional[List[str]] = None) -> None:
    args = _build_parser().parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-5s %(name)s | %(message)s",
    )

    from .ucv_client import UCVClient
    from .mcp_client import MCPClient
    from .episode_builder import (
        sample_pointnav_episode,
        sample_objectnav_episode,
        sample_pointnav_episode_navmesh,
        sample_objectnav_episode_navmesh,
        sample_objectnav_search_episode,
        clear_objectnav_search_cache,
        cleanup_spawned_actors,
    )

    ucv = UCVClient(host=args.ucv_host, port=args.ucv_port, name="env-0")
    mcp = MCPClient(host=args.mcp_host, port=args.mcp_port, name="env-0-mcp")

    # If we're going to auto-start PIE, do it BEFORE the first UCV
    # connect — UnrealCV inside PIE only comes up once the game world
    # exists, and a connect attempted against the editor world will
    # succeed against a different (cached) socket and then fail when
    # PIE swaps it underneath us.
    if not args.no_start_pie:
        try:
            mcp.start_pie(wait_seconds=5.0)
        except Exception as exc:
            print(f"WARN: PIE auto-start failed ({exc}); proceeding anyway",
                  file=sys.stderr)
    ucv.connect()

    # ── NavMesh interface: build once, reuse across all episodes.
    # Build navmesh interface if --use-navmesh or --scene-graph provided.
    # The navmesh interface no longer needs a scene graph file — all
    # sampling comes from UE navmesh directly.
    nav_interface = None
    if getattr(args, 'use_navmesh', False) or args.scene_graph:
        from nav_task.navmesh_interface import NavmeshNavigationInterface
        nav_interface = NavmeshNavigationInterface(ucv)
        print("Building navmesh...", file=sys.stderr)
        resp = nav_interface.build_navmesh()
        print(f"NavMesh built: {resp}", file=sys.stderr)

    # --record-trajectory wins over --no-rgb.
    capture_rgb = args.record_trajectory or not args.no_rgb

    env = SimWorldNavEnv(
        ucv_client=ucv,
        mcp_client=mcp,
        agent_name=args.agent_name,
        capture_rgb=capture_rgb,
        ensure_pie=not args.no_start_pie,
    )

    # ── Single source of truth: model config flows to both agent + mem0.
    llm = make_llm(
        args.model,
        model=args.model_id,
        base_url=args.base_url,
        api_key=args.api_key,
    )
    memory = build_memory(
        args.memory,
        agent_id=args.agent_name,
        llm_model=args.model_id,
        llm_base_url=args.base_url,
        llm_api_key=args.api_key,
    )

    # Wire the agent LLM into hierarchical memory so its L3 skill
    # distillation can use it.  Using the same LLM keeps a single
    # source of truth for the experiment.
    from .memory.hierarchical import HierarchicalMemory
    if isinstance(memory, HierarchicalMemory) and memory._llm_call is None:
        def _hier_llm_call(prompt: str) -> str:
            resp = llm.chat(
                [LLMMessage.text("user", prompt)],
                tools=[],
                max_tokens=1024,
            )
            return resp.text or ""
        memory._llm_call = _hier_llm_call
        log.info("HierarchicalMemory: wired LLM distillation via %s", llm.name)

    # ── ObjectNav search: build describer + clear cache.
    describer = None
    if args.task == "objectnav-search":
        if not args.scene_graph:
            print("ERROR: --scene-graph is required for objectnav-search",
                  file=sys.stderr)
            sys.exit(2)
        from .scene_context import load_scene_graph
        from .describer import TargetDescriber
        sg_actors = load_scene_graph(args.scene_graph)
        log.info("loaded scene graph with %d actors", len(sg_actors))

        # Build the describer LLM (optionally decoupled from agent LLM).
        describer_short = args.describer_model or args.model
        describer_llm = make_llm(
            describer_short,
            model=args.describer_model_id or args.model_id,
            base_url=args.describer_base_url or args.base_url,
            api_key=args.describer_api_key or args.api_key,
        )

        def _describer_call(prompt: str) -> str:
            resp = describer_llm.chat(
                [LLMMessage.text("user", prompt)],
                tools=[],
                max_tokens=200,
            )
            return resp.text or ""

        describer = TargetDescriber(
            llm_call=_describer_call,
            scene_graph=sg_actors,
            landmark_radius_cm=5000.0,
            landmark_top_k=6,
            categories=("building", "tree"),
        )
        clear_objectnav_search_cache()

    # ── Multi-episode loop ───────────────────────────────────────────
    all_metrics: List[Dict[str, Any]] = []

    try:
        for ep_idx in range(args.n_episodes):
            seed = args.seed + ep_idx
            episode_extra = {}
            if args.task == "pointnav":
                if nav_interface is not None:
                    result = sample_pointnav_episode_navmesh(
                        ucv,
                        seed=seed, idx=ep_idx,
                        min_geodesic_cm=args.nav_min_cm,
                        max_geodesic_cm=args.nav_max_cm,
                        max_steps=args.max_steps,
                        build_navmesh=False,
                        nav_interface=nav_interface,
                    )
                    episode = result["episode"]
                    episode_extra = {
                        "start_heading_deg": result["start_heading_deg"],
                        "difficulty": result["difficulty"],
                        "gt_path_waypoints": result["gt_path_waypoints"],
                    }
                else:
                    episode = sample_pointnav_episode(
                        ucv, seed=seed,
                        target_distance_cm=args.target_distance,
                        max_steps=args.max_steps,
                    )
            elif args.task == "objectnav-search":
                # All episodes in one run share a single pre-spawned
                # batch of small objects (one spawn + one navmesh
                # rebuild on ep_idx=0, then per-episode cache lookups).
                result = sample_objectnav_search_episode(
                    ucv,
                    seed=args.seed,          # shared across episodes
                    idx=ep_idx,
                    describer=describer,
                    n_targets=max(args.n_search_targets, args.n_episodes),
                    nav_interface=nav_interface,
                    min_geodesic_cm=args.nav_min_cm,
                    max_geodesic_cm=args.nav_max_cm,
                    max_steps=args.max_steps,
                    build_navmesh=False,
                )
                episode = result["episode"]
                episode_extra = {
                    "start_heading_deg": result["start_heading_deg"],
                    "difficulty": result["difficulty"],
                    "gt_path_waypoints": result["gt_path_waypoints"],
                    "target_actor_name": result["target_actor_name"],
                    "task_prompt": result["prompt"],
                    "description_generator": result["description"].generator,
                }
            else:  # objectnav (classic, uses pre-existing scene actors)
                if not args.target_filter:
                    print("ERROR: --target-filter required for objectnav",
                          file=sys.stderr)
                    sys.exit(2)
                substr = args.target_filter
                if nav_interface is not None:
                    result = sample_objectnav_episode_navmesh(
                        ucv,
                        seed=seed, idx=ep_idx,
                        target_filter=lambda name, s=substr: s in name,
                        object_category=args.object_category,
                        max_steps=args.max_steps,
                        build_navmesh=False,
                        nav_interface=nav_interface,
                    )
                    episode = result["episode"]
                    episode_extra = {
                        "start_heading_deg": result["start_heading_deg"],
                        "difficulty": result["difficulty"],
                        "gt_path_waypoints": result["gt_path_waypoints"],
                        "target_actor_name": result.get("target_actor_name"),
                    }
                else:
                    episode = sample_objectnav_episode(
                        ucv, seed=seed,
                        target_filter=lambda name, s=substr: s in name,
                        object_category=args.object_category,
                        max_steps=args.max_steps,
                    )

            run_tag = (
                args.run_name
                or f"{llm.name}_{episode.episode_id}"
            )
            logger = EpisodeLogger(
                run_name=run_tag,
                save_frames=capture_rgb,
                annotate_frames=args.record_trajectory,
                meta={
                    "model": llm.name,
                    "model_id": llm.model,
                    "task": args.task,
                    "episode_id": episode.episode_id,
                    "episode_index": ep_idx,
                    "seed": seed,
                    "n_episodes": args.n_episodes,
                    "memory": args.memory,
                    "args": vars(args),
                    "task_source": "navmesh" if nav_interface else "legacy",
                    **episode_extra,
                },
            )

            try:
                metrics = run_episode(
                    env, llm, episode, logger,
                    memory=memory,
                    max_steps=args.max_steps,
                    vision_history_depth=args.vision_depth,
                    task_prompt_override=episode_extra.get("task_prompt"),
                )
            finally:
                logger.close()

            all_metrics.append(metrics)
            sr = metrics.get("SR", 0)
            spl = metrics.get("SPL", 0)
            cum_sr = sum(m.get("SR", 0) for m in all_metrics) / len(all_metrics)
            print(
                f"\n== episode {ep_idx+1}/{args.n_episodes}  seed={seed} ==\n"
                f"  SR={sr:.1f}  SPL={spl:.2f}  cumulative_SR={cum_sr:.2f}"
            )

        # ── Experiment summary ───────────────────────────────────────
        n = len(all_metrics)
        avg_sr  = sum(m.get("SR", 0)  for m in all_metrics) / max(n, 1)
        avg_spl = sum(m.get("SPL", 0) for m in all_metrics) / max(n, 1)
        print(
            f"\n{'='*50}\n"
            f"EXPERIMENT DONE: {n} episodes\n"
            f"  avg SR  = {avg_sr:.2f}\n"
            f"  avg SPL = {avg_spl:.2f}\n"
            f"  per-episode SR: {[m.get('SR',0) for m in all_metrics]}\n"
            f"{'='*50}"
        )
    finally:
        env.close()
        ucv.disconnect()


if __name__ == "__main__":
    main()
