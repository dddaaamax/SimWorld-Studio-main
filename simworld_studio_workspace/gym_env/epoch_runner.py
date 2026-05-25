"""Epoch-based experiment runner with train/test split and WandB logging.

Workflow
--------
1. Generate fixed train and test episode sets (deterministic from seed).
2. For each epoch:
   a. Run all train episodes (memory accumulates across episodes).
   b. Run all test episodes (memory is read-only; no reset between test eps).
   c. Log per-episode and aggregate metrics to WandB.
3. After all epochs, log final summary.

Two conditions:
  --memory none   : no memory, pure VLM baseline
  --memory mem0   : mem0-backed memory that persists across episodes/epochs

Usage
-----
    python -m gym_env.epoch_runner \
        --model claude --memory mem0 \
        --train-size 2 --test-size 5 --epochs 5 \
        --ucv-port 9002 --mcp-port 55561

WandB logs: RGB frames, LLM reasoning, memory contents, per-step metrics.
"""

from __future__ import annotations

import argparse
import io
import json
import logging
import math
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import numpy as np

from nav_task.episode import NavigationEpisode

from .action_space import nav_tool_schemas
from .episode_builder import sample_pointnav_episode
from .llm import LLMClient, LLMMessage, make_llm
from .logger import EpisodeLogger
from .memory import AgentMemory, NullMemory, build_memory
from .runner import _NAV_SYSTEM_PROMPT, _build_user_text, _strip_images
from .simworld_nav_env import SimWorldNavEnv

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Episode set generation
# ---------------------------------------------------------------------------

def generate_episode_set(
    ucv,
    *,
    base_seed: int,
    count: int,
    target_distance_cm: float = 2000.0,
    max_steps: int = 40,
    label: str = "episodes",
) -> List[NavigationEpisode]:
    """Generate a deterministic set of PointNav episodes."""
    episodes = []
    for i in range(count):
        ep = sample_pointnav_episode(
            ucv,
            seed=base_seed + i,
            idx=i,
            target_distance_cm=target_distance_cm,
            max_steps=max_steps,
        )
        episodes.append(ep)
        log.info(
            "%s[%d]: %s start=(%.0f,%.0f) goal=(%.0f,%.0f)",
            label, i, ep.episode_id,
            ep.start_position.x, ep.start_position.y,
            ep.goal_position.x, ep.goal_position.y,
        )
    return episodes


# ---------------------------------------------------------------------------
# WandB helpers
# ---------------------------------------------------------------------------

def _rgb_to_wandb_image(rgb: np.ndarray, caption: str = ""):
    """Convert H x W x 3 uint8 array to wandb.Image."""
    import wandb
    from PIL import Image
    img = Image.fromarray(rgb)
    return wandb.Image(img, caption=caption)


def _init_wandb(args, train_episodes, test_episodes):
    """Initialize WandB run and log config."""
    import wandb
    config = {
        "model": args.model,
        "model_id": args.model_id,
        "memory": args.memory,
        "train_size": args.train_size,
        "test_size": args.test_size,
        "epochs": args.epochs,
        "max_steps": args.max_steps,
        "target_distance_cm": args.target_distance,
        "seed": args.seed,
        "ucv_port": args.ucv_port,
        "mcp_port": args.mcp_port,
        "vision_depth": args.vision_depth,
        "train_seeds": [ep.seed for ep in train_episodes],
        "test_seeds": [ep.seed for ep in test_episodes],
    }
    run_name = args.run_name or f"{args.model}_{args.memory}_e{args.epochs}"
    run = wandb.init(
        project=args.wandb_project,
        name=run_name,
        config=config,
        tags=[args.model, args.memory, f"train{args.train_size}", f"test{args.test_size}"],
    )
    return run


# ---------------------------------------------------------------------------
# Single episode runner with WandB step logging
# ---------------------------------------------------------------------------

def run_episode_with_logging(
    env: SimWorldNavEnv,
    llm: LLMClient,
    episode: NavigationEpisode,
    logger: EpisodeLogger,
    *,
    memory: AgentMemory,
    max_steps: int = 40,
    vision_history_depth: int = 1,
    max_tokens: int = 1024,
    history_l1: int = 5,
    history_l2: int = 10,
    wandb_run=None,
    global_step_offset: int = 0,
    split: str = "train",
    epoch: int = 0,
    episode_idx: int = 0,
    reset_memory: bool = True,
) -> Tuple[Dict[str, Any], int]:
    """Run one episode. Returns (metrics, steps_taken).

    If reset_memory is True, calls memory.reset() at episode start
    (clears trajectory buffer for strategy memory).
    """
    import wandb
    from .memory.strategy_backend import StrategyMemory

    obs, info = env.reset(episode)
    logger.log_step(0, None, obs, 0.0, False, False, info)

    if reset_memory:
        memory.reset()

    # Build system prompt: base + strategy lessons (if any)
    system_text = _NAV_SYSTEM_PROMPT
    if isinstance(memory, StrategyMemory):
        lessons = memory.get_system_prompt_section()
        if lessons:
            system_text += lessons

    history: List[LLMMessage] = [
        LLMMessage.text("system", system_text),
    ]

    final_metrics: Dict[str, Any] = {}
    total_steps = 0
    strategies_text = ""
    if isinstance(memory, StrategyMemory):
        strategies_text = "\n".join(memory.query("", k=5))

    for t in range(1, max_steps + 1):
        user_text = _build_user_text(info, obs)
        # Strategy memory: NO recall injection into user prompt.
        # Strategies are already in the system prompt.
        # For other memory types, prepend recalled items.
        recalled_text = ""
        if not isinstance(memory, StrategyMemory):
            recalled = memory.query(user_text, k=5)
            if recalled:
                recalled_text = "\n".join(f"- {m}" for m in recalled)
                user_text = f"Relevant past experience:\n{recalled_text}\n\n{user_text}"

        # Attach image
        rgb = obs.get("rgb")
        if rgb is not None and getattr(llm, "name", "") != "claude-sdk":
            history.append(LLMMessage.user_with_image(user_text, rgb))
        else:
            history.append(LLMMessage.text("user", user_text))
        _strip_images(history, vision_history_depth)
        from .runner import _truncate_history
        history[:] = _truncate_history(history, l1_keep=history_l1, l2_keep=history_l2)

        # LLM call
        resp = llm.chat(history, nav_tool_schemas(), max_tokens=max_tokens)
        logger.log_llm(t, llm.name, resp)

        thought = (resp.text or "").strip()
        reasoning = resp.reasoning or ""
        for tc in resp.tool_calls:
            print(f"  [{split} ep{episode_idx} t={t}] {tc.name}")

        if not resp.tool_calls:
            log.info("LLM returned no tool calls; ending episode")
            break

        history.append(LLMMessage(
            role="assistant",
            content=[{"type": "text", "text": resp.text or ""}],
            tool_calls=resp.tool_calls,
        ))

        done_now = False
        for tc in resp.tool_calls:
            prev_d = info.get("distance_to_goal_cm")
            obs, reward, done, truncated, info = env.step(tc.to_action_dict())
            logger.log_step(t, tc.to_action_dict(), obs, reward, done, truncated, info)
            total_steps += 1

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

            # Buffer step into trajectory (strategy memory) or insert directly (others)
            new_d = info.get("distance_to_goal_cm")
            delta = (prev_d - new_d) if (prev_d is not None and new_d is not None) else 0.0
            step_record = (
                f"t={t} {tc.name} d_goal:{prev_d:.0f}->{new_d:.0f}cm "
                f"(delta={delta:+.0f}) reward={reward:+.3f} yaw={obs['agent_yaw_deg']:+.0f}"
            )
            memory.insert(step_record)

            # WandB per-step logging
            if wandb_run:
                step_log = {
                    f"{split}/step_reward": reward,
                    f"{split}/distance_to_goal": info["distance_to_goal_cm"],
                    f"{split}/cumulative_reward": info.get("cumulative_reward", 0),
                    f"{split}/action": tc.name,
                    f"{split}/epoch": epoch,
                    f"{split}/episode_idx": episode_idx,
                    f"{split}/step": t,
                }
                if rgb is not None:
                    caption = (
                        f"e{epoch} {split}[{episode_idx}] t={t} "
                        f"{tc.name} r={reward:+.2f} d={info['distance_to_goal_cm']:.0f}cm"
                    )
                    step_log[f"{split}/observation"] = _rgb_to_wandb_image(rgb, caption)
                if reasoning:
                    step_log[f"{split}/reasoning"] = wandb.Html(f"<pre>{reasoning[:2000]}</pre>")
                if thought:
                    step_log[f"{split}/thought"] = wandb.Html(f"<pre>{thought[:2000]}</pre>")
                if strategies_text:
                    step_log[f"{split}/active_strategies"] = wandb.Html(f"<pre>{strategies_text}</pre>")
                wandb.log(step_log, step=global_step_offset + total_steps)

            if done or truncated:
                final_metrics = info.get("metrics", {}) or {}
                done_now = True
                break
        if done_now:
            break

    # End-of-episode: strategy reflection (1 LLM call for the whole episode)
    sr = final_metrics.get("SR", 0)
    path_cm = final_metrics.get("path_length_cm", 0)
    cum_r = final_metrics.get("cumulative_reward", 0)
    outcome = (
        f"SUCCESS in {env.step_count} steps, path={path_cm:.0f}cm, reward={cum_r:+.0f}"
        if sr > 0
        else f"FAIL after {env.step_count} steps, final d_goal={info.get('distance_to_goal_cm', '?')}cm, reward={cum_r:+.0f}"
    )

    if isinstance(memory, StrategyMemory):
        reflection = memory.reflect(outcome)
        if reflection and wandb_run:
            wandb.log({
                f"{split}/reflection": wandb.Html(f"<pre>{reflection[:3000]}</pre>"),
                f"{split}/strategies_after": wandb.Html(
                    f"<pre>{chr(10).join(memory.query('', k=5))}</pre>"
                ),
            }, step=global_step_offset + total_steps)

    logger.log_summary(final_metrics or {"note": "loop exited without done"})
    return final_metrics, total_steps


# ---------------------------------------------------------------------------
# Epoch runner
# ---------------------------------------------------------------------------

def run_epoch(
    env: SimWorldNavEnv,
    llm: LLMClient,
    episodes: List[NavigationEpisode],
    memory: AgentMemory,
    *,
    split: str,
    epoch: int,
    max_steps: int = 40,
    vision_depth: int = 3,
    wandb_run=None,
    global_step: int = 0,
    capture_rgb: bool = True,
    run_root: str = "runs",
) -> Tuple[Dict[str, float], int]:
    """Run all episodes in a split. Returns (agg_metrics, updated_global_step)."""
    all_metrics: List[Dict[str, Any]] = []

    for ep_idx, episode in enumerate(episodes):
        run_tag = f"epoch{epoch}_{split}_{episode.episode_id}"
        logger = EpisodeLogger(
            run_name=run_tag,
            root=run_root,
            save_frames=capture_rgb,
            annotate_frames=True,
            meta={
                "model": llm.name,
                "split": split,
                "epoch": epoch,
                "episode_idx": ep_idx,
                "episode_id": episode.episode_id,
                "memory": "enabled" if not isinstance(memory, NullMemory) else "none",
            },
        )
        try:
            # Train: don't reset memory between episodes (accumulate).
            # Test: also don't reset (use accumulated train knowledge).
            metrics, steps = run_episode_with_logging(
                env, llm, episode, logger,
                memory=memory,
                max_steps=max_steps,
                vision_history_depth=vision_depth,
                wandb_run=wandb_run,
                global_step_offset=global_step,
                split=split,
                epoch=epoch,
                episode_idx=ep_idx,
                reset_memory=False,
            )
            global_step += steps
        finally:
            logger.close()

        all_metrics.append(metrics)
        sr = metrics.get("SR", 0)
        spl = metrics.get("SPL", 0)
        print(
            f"  {split}[{ep_idx}] SR={sr:.0f} SPL={spl:.2f} "
            f"steps={steps} cum_r={metrics.get('cumulative_reward', 0):+.1f}"
        )

    # Aggregate
    n = len(all_metrics) or 1
    agg = {
        f"{split}/SR": sum(m.get("SR", 0) for m in all_metrics) / n,
        f"{split}/SPL": sum(m.get("SPL", 0) for m in all_metrics) / n,
        f"{split}/SoftSPL": sum(m.get("SoftSPL", 0) for m in all_metrics) / n,
        f"{split}/cumulative_reward": sum(m.get("cumulative_reward", 0) for m in all_metrics) / n,
    }
    return agg, global_step


# ---------------------------------------------------------------------------
# CLI & main
# ---------------------------------------------------------------------------

def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m gym_env.epoch_runner",
        description="Epoch-based nav experiment with train/test split and WandB.",
    )
    # LLM
    p.add_argument("--model", default="claude")
    p.add_argument("--model-id", default=None)
    p.add_argument("--base-url", default=None)
    p.add_argument("--api-key", default=None)
    # Experiment
    p.add_argument("--train-size", type=int, default=2)
    p.add_argument("--test-size", type=int, default=5)
    p.add_argument("--epochs", type=int, default=5)
    p.add_argument("--max-steps", type=int, default=40)
    p.add_argument("--target-distance", type=float, default=2000.0)
    p.add_argument("--seed", type=int, default=42)
    p.add_argument("--vision-depth", type=int, default=3)
    # Memory
    p.add_argument("--memory", default="none", choices=["none", "text", "mem0", "strategy"])
    # UE connection
    p.add_argument("--ucv-host", default="127.0.0.1")
    p.add_argument("--ucv-port", type=int, default=9002)
    p.add_argument("--mcp-host", default="127.0.0.1")
    p.add_argument("--mcp-port", type=int, default=55558)
    p.add_argument("--no-start-pie", action="store_true")
    p.add_argument("--agent-name", default="GymNavAgent_0")
    # WandB
    p.add_argument("--wandb-project", default="simworld-nav")
    p.add_argument("--wandb-key", default=None)
    p.add_argument("--run-name", default=None)
    # Misc
    p.add_argument("--log-level", default="INFO")
    return p


def main(argv: Optional[List[str]] = None) -> None:
    args = _build_parser().parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-5s %(name)s | %(message)s",
    )

    # WandB login
    if args.wandb_key:
        os.environ["WANDB_API_KEY"] = args.wandb_key
    import wandb

    from .ucv_client import UCVClient
    from .mcp_client import MCPClient

    # Connect
    ucv = UCVClient(host=args.ucv_host, port=args.ucv_port, name="epoch-ucv")
    mcp = MCPClient(host=args.mcp_host, port=args.mcp_port, name="epoch-mcp")

    if not args.no_start_pie:
        try:
            mcp.start_pie(wait_seconds=5.0)
        except Exception as exc:
            print(f"WARN: PIE auto-start failed ({exc}); proceeding", file=sys.stderr)
    ucv.connect()

    # Generate fixed episode sets
    print("\n=== Generating episode sets ===")
    train_episodes = generate_episode_set(
        ucv, base_seed=args.seed, count=args.train_size,
        target_distance_cm=args.target_distance,
        max_steps=args.max_steps, label="train",
    )
    test_episodes = generate_episode_set(
        ucv, base_seed=args.seed + 1000, count=args.test_size,
        target_distance_cm=args.target_distance,
        max_steps=args.max_steps, label="test",
    )

    # Init WandB
    wb_run = _init_wandb(args, train_episodes, test_episodes)

    # Build LLM + memory + env
    llm = make_llm(args.model, model=args.model_id,
                   base_url=args.base_url, api_key=args.api_key)
    memory = build_memory(
        args.memory, agent_id=args.agent_name,
        llm_model=args.model_id, llm_base_url=args.base_url,
        llm_api_key=args.api_key,
    )
    # Wire up LLM call for strategy memory reflection
    from .memory.strategy_backend import StrategyMemory
    if isinstance(memory, StrategyMemory):
        def _strategy_llm_call(prompt: str) -> str:
            resp = llm.chat(
                [LLMMessage.text("user", prompt)],
                tools=[],
                max_tokens=512,
            )
            return resp.text or ""
        memory._llm_call = _strategy_llm_call
        log.info("StrategyMemory: wired LLM reflection via %s", llm.name)

    env = SimWorldNavEnv(
        ucv_client=ucv, mcp_client=mcp,
        agent_name=args.agent_name,
        capture_rgb=True,
        ensure_pie=not args.no_start_pie,
    )

    # Log episode configs as tables
    train_table = wandb.Table(
        columns=["idx", "episode_id", "seed", "start_x", "start_y", "goal_x", "goal_y", "distance"],
        data=[
            [i, ep.episode_id, ep.seed,
             ep.start_position.x, ep.start_position.y,
             ep.goal_position.x, ep.goal_position.y,
             ep.reference_path.shortest_path_length_cm]
            for i, ep in enumerate(train_episodes)
        ],
    )
    test_table = wandb.Table(
        columns=["idx", "episode_id", "seed", "start_x", "start_y", "goal_x", "goal_y", "distance"],
        data=[
            [i, ep.episode_id, ep.seed,
             ep.start_position.x, ep.start_position.y,
             ep.goal_position.x, ep.goal_position.y,
             ep.reference_path.shortest_path_length_cm]
            for i, ep in enumerate(test_episodes)
        ],
    )
    wandb.log({"train_episodes": train_table, "test_episodes": test_table})

    # === Epoch loop ===
    global_step = 0
    try:
        for epoch in range(args.epochs):
            print(f"\n{'='*60}")
            print(f"  EPOCH {epoch + 1}/{args.epochs}")
            print(f"{'='*60}")

            # --- Train ---
            print(f"\n--- Train ({args.train_size} episodes) ---")
            train_metrics, global_step = run_epoch(
                env, llm, train_episodes, memory,
                split="train", epoch=epoch,
                max_steps=args.max_steps,
                vision_depth=args.vision_depth,
                wandb_run=wb_run,
                global_step=global_step,
            )

            # --- Test ---
            print(f"\n--- Test ({args.test_size} episodes) ---")
            test_metrics, global_step = run_epoch(
                env, llm, test_episodes, memory,
                split="test", epoch=epoch,
                max_steps=args.max_steps,
                vision_depth=args.vision_depth,
                wandb_run=wb_run,
                global_step=global_step,
            )

            # Log epoch-level metrics
            epoch_log = {"epoch": epoch}
            epoch_log.update(train_metrics)
            epoch_log.update(test_metrics)
            wandb.log(epoch_log, step=global_step)

            print(f"\n  Epoch {epoch+1} results:")
            print(f"    Train: SR={train_metrics['train/SR']:.2f}  SPL={train_metrics['train/SPL']:.2f}")
            print(f"    Test:  SR={test_metrics['test/SR']:.2f}  SPL={test_metrics['test/SPL']:.2f}")

    except KeyboardInterrupt:
        print("\nInterrupted by user.")
    finally:
        env.close()
        ucv.disconnect()
        wandb.finish()
        print("\nExperiment complete. WandB run finished.")


if __name__ == "__main__":
    main()
