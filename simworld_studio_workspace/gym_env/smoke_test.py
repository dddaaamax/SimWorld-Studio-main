"""End-to-end smoke test for gym_env without an LLM.

Runs the full pipeline against a live UE editor:

  1. Start PIE via MCP (if not already running).
  2. Connect UnrealCV.
  3. Spawn the humanoid agent.
  4. Build a PointNav episode at a fixed offset.
  5. Run a hardcoded action sequence
     (MOVE_FORWARD x N, TURN_RIGHT, MOVE_FORWARD, STOP).
  6. Print rewards, distances, final SR/SPL.

Run from the SimWorld-Studio-Dev/simworld_studio_workspace directory:

    python -m gym_env.smoke_test --steps 8

Useful flags:

    --no-rgb            skip RGB capture (faster, no PIL needed)
    --no-start-pie      assume PIE already running
    --target-distance   PointNav target distance in cm (default 1500)
    --log-level DEBUG   show every UCV command
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path

# Allow running as a module from the workspace dir without install
_HERE = Path(__file__).resolve()
_WORKSPACE = _HERE.parents[1]
if str(_WORKSPACE) not in sys.path:
    sys.path.insert(0, str(_WORKSPACE))
# nav_task lives inside simworld_studio_workspace/ — no external task_gen needed
_TASK_GEN = Path(os.environ.get("TASK_GEN_DIR", _WORKSPACE))
if _TASK_GEN.exists() and str(_TASK_GEN) not in sys.path:
    sys.path.insert(0, str(_TASK_GEN))

from gym_env import (  # noqa: E402
    EpisodeLogger,
    MCPClient,
    SimWorldNavEnv,
    UCVClient,
    sample_pointnav_episode,
)


def _make_action_sequence(n_steps: int):
    """A deterministic mix of forward + turns to exercise every action."""
    seq = []
    for i in range(n_steps):
        if i % 4 == 3:
            seq.append({"tool": "TURN_RIGHT"})
        else:
            seq.append({"tool": "MOVE_FORWARD"})
    seq.append({"tool": "STOP"})
    return seq


def main(argv=None):
    p = argparse.ArgumentParser(prog="gym_env.smoke_test")
    p.add_argument("--ucv-host", default="127.0.0.1")
    p.add_argument("--ucv-port", type=int, default=9002)
    p.add_argument("--mcp-host", default="127.0.0.1")
    p.add_argument("--mcp-port", type=int, default=55558)
    p.add_argument("--no-start-pie", action="store_true")
    p.add_argument("--no-rgb", action="store_true")
    p.add_argument("--steps", type=int, default=6)
    p.add_argument("--target-distance", type=float, default=1500.0)
    p.add_argument("--seed", type=int, default=42)
    p.add_argument("--agent-name", default="GymNavAgent_0")
    p.add_argument("--log-level", default="INFO")
    args = p.parse_args(argv)

    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-5s %(name)s | %(message)s",
    )
    log = logging.getLogger("smoke")

    # ---- 1. PIE + UCV ----------------------------------------------------
    mcp = MCPClient(host=args.mcp_host, port=args.mcp_port, name="smoke-mcp")
    if not args.no_start_pie:
        log.info("starting PIE via MCP...")
        try:
            mcp.start_pie(wait_seconds=5.0)
        except Exception as exc:
            log.error("PIE start failed: %s — proceeding anyway", exc)

    ucv = UCVClient(host=args.ucv_host, port=args.ucv_port, name="smoke-ucv")
    ucv.connect()
    objs = ucv.vget_objects()
    log.info("UCV connected; scene actors=%d", len(objs))

    # ---- 2. Build episode ------------------------------------------------
    episode = sample_pointnav_episode(
        ucv,
        seed=args.seed,
        target_distance_cm=args.target_distance,
        max_steps=args.steps + 2,
    )
    log.info("episode: %s start=%s goal=%s",
             episode.episode_id, episode.start_position, episode.goal_position)

    # ---- 3. Env reset ----------------------------------------------------
    env = SimWorldNavEnv(
        ucv_client=ucv,
        mcp_client=mcp,
        agent_name=args.agent_name,
        capture_rgb=not args.no_rgb,
        ensure_pie=not args.no_start_pie,
    )
    logger = EpisodeLogger(
        run_name=f"smoke_{episode.episode_id}",
        meta={
            "model": "scripted",
            "task": "pointnav",
            "episode_id": episode.episode_id,
            "args": vars(args),
        },
        save_frames=not args.no_rgb,
    )

    try:
        obs, info = env.reset(episode)
        logger.log_step(0, None, obs, 0.0, False, False, info)
        log.info("reset OK; agent_xy=%s d_goal=?", info["agent_xy"])

        # ---- 4. Step through scripted sequence --------------------------
        actions = _make_action_sequence(args.steps)
        for t, action in enumerate(actions, start=1):
            obs, reward, done, trunc, info = env.step(action)
            logger.log_step(t, action, obs, reward, done, trunc, info)
            print(
                f"[t={t}] {action['tool']:<13} reward={reward:+.3f}  "
                f"d_goal={info['distance_to_goal_cm']:.0f}cm  "
                f"pos=({info['agent_xy'][0]:+.0f},{info['agent_xy'][1]:+.0f})  "
                f"done={done} trunc={trunc}"
            )
            if done or trunc:
                break

        metrics = info.get("metrics") or {}
        if metrics:
            print("\n== final metrics ==")
            for k, v in metrics.items():
                print(f"  {k}: {v}")
        logger.log_summary(metrics or {"note": "loop ended without done"})
        print(f"\nrun dir: {logger.dir}")

    finally:
        env.close()
        logger.close()
        ucv.disconnect()


if __name__ == "__main__":
    main()
