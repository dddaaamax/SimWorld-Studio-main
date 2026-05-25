"""Plot SR / SoftSPL learning curves: memory vs no-memory.

Usage::

    PYTHONPATH=$TASK_GEN_DIR \
        python -m gym_env.plot_learning_curve
"""

from __future__ import annotations

import glob
import json
import os
import sys
from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np


def load_runs(pattern: str) -> list:
    """Load summary.json from all matching run dirs, sorted by time."""
    dirs = sorted(glob.glob(pattern))
    results = []
    for d in dirs:
        sp = os.path.join(d, "summary.json")
        if os.path.exists(sp):
            with open(sp, encoding="utf-8") as f:
                data = json.load(f)
                data["run_dir"] = d
                results.append(data)
    return results


def cumulative_sr(runs: list) -> list:
    """Compute cumulative SR after each episode."""
    total = 0
    curve = []
    for i, r in enumerate(runs):
        total += r.get("SR", 0)
        curve.append(total / (i + 1))
    return curve


def main():
    os.chdir(Path(__file__).resolve().parent.parent)

    # Load both conditions
    no_mem = load_runs("runs/*qwen_batch*")
    with_mem = load_runs("runs/*qwen_with_memory*")

    if not no_mem and not with_mem:
        print("No run data found!", file=sys.stderr)
        sys.exit(1)

    fig, axes = plt.subplots(1, 2, figsize=(14, 5))

    # ── Panel 1: SoftSPL per episode ──
    ax = axes[0]
    if no_mem:
        sspl_no = [r.get("SoftSPL", 0) for r in no_mem]
        ax.plot(range(1, len(sspl_no) + 1), sspl_no,
                "o--", color="#d62728", label="No Memory", markersize=8)
    if with_mem:
        sspl_mem = [r.get("SoftSPL", 0) for r in with_mem]
        ax.plot(range(1, len(sspl_mem) + 1), sspl_mem,
                "s-", color="#2ca02c", label="With Memory", markersize=8)
    ax.set_xlabel("Episode", fontsize=12)
    ax.set_ylabel("SoftSPL", fontsize=12)
    ax.set_title("SoftSPL per Episode", fontsize=14)
    ax.legend(fontsize=11)
    ax.set_ylim(-0.05, 1.05)
    ax.grid(True, alpha=0.3)

    # ── Panel 2: Cumulative reward ──
    ax = axes[1]
    if no_mem:
        rew_no = [r.get("cumulative_reward", 0) for r in no_mem]
        ax.plot(range(1, len(rew_no) + 1), rew_no,
                "o--", color="#d62728", label="No Memory", markersize=8)
    if with_mem:
        rew_mem = [r.get("cumulative_reward", 0) for r in with_mem]
        ax.plot(range(1, len(rew_mem) + 1), rew_mem,
                "s-", color="#2ca02c", label="With Memory", markersize=8)
    ax.set_xlabel("Episode", fontsize=12)
    ax.set_ylabel("Cumulative Reward", fontsize=12)
    ax.set_title("Episode Reward", fontsize=14)
    ax.legend(fontsize=11)
    ax.grid(True, alpha=0.3)

    fig.suptitle("Qwen3-VL-30B Navigation: Memory vs No Memory",
                 fontsize=15, fontweight="bold")
    plt.tight_layout()

    out = "runs/learning_curve.png"
    fig.savefig(out, dpi=150, bbox_inches="tight")
    print(f"Saved to {out}")
    plt.close()

    # Print table
    print("\n=== Per-Episode Comparison ===")
    print(f"{'Ep':>3}  {'No-Mem SoftSPL':>15}  {'Mem SoftSPL':>15}  "
          f"{'No-Mem Reward':>14}  {'Mem Reward':>12}")
    max_ep = max(len(no_mem), len(with_mem))
    for i in range(max_ep):
        nm_s = f"{no_mem[i]['SoftSPL']:.3f}" if i < len(no_mem) else "—"
        wm_s = f"{with_mem[i]['SoftSPL']:.3f}" if i < len(with_mem) else "—"
        nm_r = f"{no_mem[i]['cumulative_reward']:+.0f}" if i < len(no_mem) else "—"
        wm_r = f"{with_mem[i]['cumulative_reward']:+.0f}" if i < len(with_mem) else "—"
        print(f"{i+1:>3}  {nm_s:>15}  {wm_s:>15}  {nm_r:>14}  {wm_r:>12}")


if __name__ == "__main__":
    main()
