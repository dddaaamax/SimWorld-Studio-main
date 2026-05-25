"""Mock simulation to validate curriculum dynamics under old vs new reward.

This does NOT touch UE or a real LLM. It replaces:
  - Real nav agent with a stochastic model: SR = f(difficulty, cumulative_training,
    scene_familiarity) + Bernoulli(n_episodes) noise.
  - Real coding LLM with a rule-based policy that encodes the prompt's intent
    (pick next difficulty to maximize reward, respect monotone clamps).

Two conditions:
  old: reward = 1 - SR, no difficulty floor, n_eps=4.
  new: reward = Gaussian(SR;0.6) * (1 + 0.25*progress_bonus), diff floor
       prev-0.5, n_eps=8, scene persistence >= 3.

Outputs:
  - mock_sim_old.json / mock_sim_new.json
  - mock_sim_compare.png

Run:
  python -m co_evolve.simulate_curriculum --epochs 25 --seed 7
"""
from __future__ import annotations

import argparse
import json
import math
import random
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import List, Dict, Any

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

from .difficulty import compute_coding_reward as gaussian_reward


# ----------------------------------------------------------------------
# Mock nav agent: SR as a function of difficulty + training progress
# ----------------------------------------------------------------------

def _sigmoid(x: float) -> float:
    return 1.0 / (1.0 + math.exp(-x))


def mock_nav_sr(
    difficulty: float,
    cumulative_epochs: int,
    scene_familiarity: int,
    n_episodes: int,
    rng: random.Random,
) -> float:
    """Simulate nav agent SR for one epoch's batch.

    capability grows slowly with training (0.02/epoch) and faster with
    scene familiarity (0.06/same-scene repeat). At difficulty=0 capability
    near 0.9. At difficulty=5 a capability-5/10=0.5 agent has ~50% SR.
    """
    capability = 0.35 + 0.02 * cumulative_epochs + 0.06 * scene_familiarity
    capability = min(0.95, capability)
    # Gap model: SR is Bernoulli-mean = sigmoid(2.2 * (capability - d/10))
    p_success = _sigmoid(2.2 * (capability - difficulty / 10.0))
    p_success = max(0.02, min(0.97, p_success))
    wins = sum(1 for _ in range(n_episodes) if rng.random() < p_success)
    return wins / n_episodes


# ----------------------------------------------------------------------
# Reward functions
# ----------------------------------------------------------------------

def old_reward(sr: float, difficulty: float = 0.0, best_diff: float = 0.0) -> float:
    """Pre-fix reward: pushes SR toward 0."""
    if sr < 0.1:
        return 0.0
    return 1.0 - sr


def new_reward(sr: float, difficulty: float, best_diff: float) -> float:
    return gaussian_reward(sr, difficulty, best_diff)


# ----------------------------------------------------------------------
# Mock coding agent policies
# ----------------------------------------------------------------------

@dataclass
class EpochRecord:
    epoch: int
    difficulty: float
    sr: float
    rolling_sr: float
    reward: float
    scene_id: str
    scene_streak: int


def _rolling(history: List[EpochRecord], k: int = 5) -> float:
    if not history:
        return 0.0
    recent = history[-k:]
    return sum(r.sr for r in recent) / len(recent)


def policy_old(history: List[EpochRecord], prev_diff: float, best_diff: float) -> Dict[str, Any]:
    """Rule-based approximation of LLM under OLD prompt ('minimize SR').

    Greedy on 1-SR reward: pushes difficulty up whenever last SR > 0.1, drops
    hard when SR collapses. No monotone floor, no scene persistence.
    """
    last_sr = history[-1].sr if history else 0.5
    if last_sr < 0.15:
        # Task too hard, zero reward — reset hard
        new_diff = max(1.0, prev_diff - 1.5)
        action = "new_scene"
    elif last_sr > 0.5:
        # Room to push; in OLD reward, lower SR means higher reward
        new_diff = min(10.0, prev_diff + 1.0)
        action = "new_scene"
    else:
        new_diff = prev_diff + 0.3
        action = "modify_scene"
    return {"difficulty": new_diff, "action": action}


def policy_new(history: List[EpochRecord], prev_diff: float, best_diff: float) -> Dict[str, Any]:
    """Rule-based approximation of LLM under NEW prompt ('ZPD @ SR=0.6').

    Follows the exact policy from coding_agent.py's CODING_AGENT_PROMPT:
    act on rolling SR, small monotone steps, scene persistence >= 3.
    """
    rolling = _rolling(history, k=5)
    scene_streak = history[-1].scene_streak if history else 0

    can_change_scene = scene_streak >= 3 or rolling < 0.10 or rolling > 0.85

    if rolling > 0.75:
        step = 0.45
        action = "new_scene" if can_change_scene else "modify_scene"
    elif rolling > 0.45:
        step = 0.10  # small tweak, stay in ZPD
        action = "keep_scene"
    elif rolling > 0.20:
        step = 0.0  # hold, let agent catch up
        action = "keep_scene"
    else:
        step = -0.35
        action = "modify_scene" if can_change_scene else "keep_scene"

    new_diff = prev_diff + step
    # Hard clamp from loop.py: no more than -0.5 per epoch
    new_diff = max(new_diff, prev_diff - 0.5)
    new_diff = max(0.5, min(8.0, new_diff))
    return {"difficulty": new_diff, "action": action}


# ----------------------------------------------------------------------
# Simulation loop
# ----------------------------------------------------------------------

def run_simulation(
    n_epochs: int,
    reward_fn,
    policy_fn,
    n_episodes: int,
    seed: int,
    enforce_scene_persist: bool,
) -> List[EpochRecord]:
    rng = random.Random(seed)
    history: List[EpochRecord] = []
    prev_diff = 1.5
    best_diff = 0.0
    scene_id = "scene_000"
    scene_counter = 0
    scene_streak = 0

    for epoch in range(n_epochs):
        # Scene familiarity: how many consecutive epochs on this scene
        familiarity = scene_streak
        sr = mock_nav_sr(
            difficulty=prev_diff,
            cumulative_epochs=epoch,
            scene_familiarity=familiarity,
            n_episodes=n_episodes,
            rng=rng,
        )
        rolling = _rolling(history, k=5) if history else sr
        reward = reward_fn(sr, prev_diff, best_diff)
        if sr >= 0.2 and prev_diff > best_diff:
            best_diff = prev_diff

        rec = EpochRecord(
            epoch=epoch,
            difficulty=prev_diff,
            sr=sr,
            rolling_sr=rolling,
            reward=reward,
            scene_id=scene_id,
            scene_streak=scene_streak,
        )
        history.append(rec)

        # Pick next epoch's difficulty + action
        decision = policy_fn(history, prev_diff, best_diff)
        new_action = decision["action"]
        if enforce_scene_persist and new_action == "new_scene" and scene_streak < 3:
            new_action = "modify_scene"

        if new_action == "new_scene":
            scene_counter += 1
            scene_id = f"scene_{scene_counter:03d}"
            scene_streak = 1
        else:
            scene_streak += 1
        prev_diff = decision["difficulty"]

    return history


# ----------------------------------------------------------------------
# Metrics + plotting
# ----------------------------------------------------------------------

def summarize(history: List[EpochRecord]) -> Dict[str, float]:
    srs = [r.sr for r in history]
    diffs = [r.difficulty for r in history]
    rewards = [r.reward for r in history]

    def std(xs):
        m = sum(xs) / len(xs)
        return (sum((x - m) ** 2 for x in xs) / len(xs)) ** 0.5

    # Difficulty slope via simple OLS (epoch vs diff)
    n = len(diffs)
    if n > 1:
        xs = list(range(n))
        mx = sum(xs) / n
        my = sum(diffs) / n
        num = sum((xs[i] - mx) * (diffs[i] - my) for i in range(n))
        den = sum((xs[i] - mx) ** 2 for i in range(n))
        slope = num / den if den > 0 else 0.0
    else:
        slope = 0.0

    # Monotonic violations: how many epochs had diff < max_so_far - 0.5
    regressions = 0
    peak = diffs[0]
    for d in diffs:
        if d < peak - 0.5:
            regressions += 1
        if d > peak:
            peak = d

    zero_reward_epochs = sum(1 for r in rewards if r <= 0.01)
    in_zpd = sum(1 for s in srs if 0.45 <= s <= 0.75)

    return {
        "mean_sr": sum(srs) / len(srs),
        "std_sr": std(srs),
        "mean_reward": sum(rewards) / len(rewards),
        "std_reward": std(rewards),
        "mean_difficulty": sum(diffs) / len(diffs),
        "final_difficulty": diffs[-1],
        "max_difficulty": max(diffs),
        "difficulty_slope_per_epoch": slope,
        "regression_epochs": regressions,
        "zero_reward_epochs": zero_reward_epochs,
        "in_zpd_epochs": in_zpd,
        "n_unique_scenes": len({r.scene_id for r in history}),
    }


def plot_compare(old: List[EpochRecord], new: List[EpochRecord], out_path: Path):
    fig, axes = plt.subplots(3, 1, figsize=(11, 10), sharex=True)

    for label, hist, color in (("OLD reward (1-SR)", old, "#FF5722"),
                                ("NEW reward (Gaussian+progress)", new, "#2196F3")):
        gens = [r.epoch for r in hist]
        srs = [r.sr for r in hist]
        diffs = [r.difficulty for r in hist]
        rewards = [r.reward for r in hist]
        axes[0].plot(gens, srs, "o-", color=color, label=label, linewidth=2, markersize=5)
        axes[1].plot(gens, diffs, "s-", color=color, label=label, linewidth=2, markersize=5)
        axes[2].plot(gens, rewards, "^-", color=color, label=label, linewidth=2, markersize=5)

    axes[0].set_ylabel("Success Rate"); axes[0].set_ylim(-0.05, 1.05)
    axes[0].axhspan(0.45, 0.75, alpha=0.08, color="green")
    axes[0].set_title("SR — NEW should sit steady in green band, OLD should oscillate")
    axes[0].legend(loc="lower right"); axes[0].grid(alpha=0.3)

    axes[1].set_ylabel("Difficulty (0-10)")
    axes[1].set_title("Difficulty — NEW should climb monotonically, OLD zig-zags")
    axes[1].legend(loc="lower right"); axes[1].grid(alpha=0.3)

    axes[2].set_ylabel("Coding reward")
    axes[2].set_xlabel("Epoch")
    axes[2].set_title("Reward stability")
    axes[2].legend(loc="lower right"); axes[2].grid(alpha=0.3)

    plt.tight_layout()
    fig.savefig(out_path, dpi=130, bbox_inches="tight")
    plt.close(fig)


# ----------------------------------------------------------------------
# CLI
# ----------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--epochs", type=int, default=25)
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--out-dir", default="runs/co_evolve/sim_eval")
    args = ap.parse_args()

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    print(f"=== Curriculum simulation (epochs={args.epochs}, seed={args.seed}) ===\n")

    # Run multiple seeds to get a stable comparison
    seeds = [args.seed + i for i in range(5)]
    all_old, all_new = [], []
    for s in seeds:
        old = run_simulation(
            n_epochs=args.epochs, reward_fn=old_reward, policy_fn=policy_old,
            n_episodes=4, seed=s, enforce_scene_persist=False,
        )
        new = run_simulation(
            n_epochs=args.epochs, reward_fn=new_reward, policy_fn=policy_new,
            n_episodes=8, seed=s, enforce_scene_persist=True,
        )
        all_old.append(old)
        all_new.append(new)

    # Aggregate metrics
    def avg_metrics(runs):
        ms = [summarize(r) for r in runs]
        keys = ms[0].keys()
        return {k: sum(m[k] for m in ms) / len(ms) for k in keys}

    m_old = avg_metrics(all_old)
    m_new = avg_metrics(all_new)

    print(f"{'metric':<30} {'OLD':>10} {'NEW':>10}")
    print("-" * 54)
    for k in m_old:
        print(f"{k:<30} {m_old[k]:>10.3f} {m_new[k]:>10.3f}")

    # Save one representative run per condition for plotting
    (out_dir / "old.json").write_text(
        json.dumps([asdict(r) for r in all_old[0]], indent=2), encoding="utf-8")
    (out_dir / "new.json").write_text(
        json.dumps([asdict(r) for r in all_new[0]], indent=2), encoding="utf-8")
    (out_dir / "metrics.json").write_text(
        json.dumps({"old": m_old, "new": m_new}, indent=2), encoding="utf-8")

    plot_compare(all_old[0], all_new[0], out_dir / "compare.png")
    print(f"\nArtifacts: {out_dir}")


if __name__ == "__main__":
    main()
