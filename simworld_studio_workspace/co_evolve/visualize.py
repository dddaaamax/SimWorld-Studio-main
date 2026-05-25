"""Visualize co-evolution experiment results.

Usage:
    python -m co_evolve.visualize runs/co_evolve/coevolve_XXXXXXXX_XXXXXX
    python -m co_evolve.visualize runs/co_evolve/coevolve_XXXXXXXX_XXXXXX/all_results.json
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.ticker as mticker


def load_results(path: str) -> list:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def resolve_results_path(path_arg: str) -> Path:
    path = Path(path_arg)
    if path.is_file():
        return path
    if not path.is_dir():
        raise FileNotFoundError(f"No such file or directory: {path}")

    for candidate in ("all_results.json", "all_results_final.json", "all_results_mid.json"):
        candidate_path = path / candidate
        if candidate_path.exists():
            return candidate_path
    raise FileNotFoundError(f"No all_results*.json found under {path}")


def plot_coevolution(results: list, output_dir: Path):
    """Create a multi-panel figure showing co-evolution dynamics."""
    gens = [r["generation"] for r in results]
    srs = [r["sr"] for r in results]
    spls = [r["spl"] for r in results]
    diffs = [r.get("difficulty_score", 0) for r in results]
    avg_steps = [r["avg_steps"] for r in results]

    fig, axes = plt.subplots(2, 2, figsize=(12, 8))
    fig.suptitle("Co-Evolution: Coding Agent × Embodied Agent", fontsize=16, y=0.98)

    # 1. Success Rate over generations
    ax = axes[0, 0]
    ax.plot(gens, srs, "o-", color="#2196F3", linewidth=2, markersize=6, label="SR")
    ax.axhspan(0.25, 0.75, alpha=0.1, color="green", label="ZPD target")
    ax.set_xlabel("Generation")
    ax.set_ylabel("Success Rate")
    ax.set_title("Nav Agent Success Rate")
    ax.set_ylim(-0.05, 1.05)
    ax.yaxis.set_major_formatter(mticker.PercentFormatter(1.0))
    ax.legend(loc="lower right")
    ax.grid(True, alpha=0.3)

    # 2. Difficulty over generations
    ax = axes[0, 1]
    ax.plot(gens, diffs, "s-", color="#FF5722", linewidth=2, markersize=6)
    ax.set_xlabel("Generation")
    ax.set_ylabel("Difficulty (0-10)")
    ax.set_title("Curriculum Difficulty")
    max_diff = max(diffs) if diffs else 10
    ax.set_ylim(-0.2, max(max_diff * 1.2, 5))
    ax.grid(True, alpha=0.3)

    # 3. SPL over generations
    ax = axes[1, 0]
    ax.plot(gens, spls, "^-", color="#4CAF50", linewidth=2, markersize=6)
    ax.set_xlabel("Generation")
    ax.set_ylabel("SPL")
    ax.set_title("Nav Agent Path Quality (SPL)")
    ax.set_ylim(-0.05, 1.05)
    ax.grid(True, alpha=0.3)

    # 4. Average steps over generations
    ax = axes[1, 1]
    ax.plot(gens, avg_steps, "D-", color="#FF9800", linewidth=2, markersize=6)
    ax.set_xlabel("Generation")
    ax.set_ylabel("Avg Steps")
    ax.set_title("Average Steps per Episode")
    ax.grid(True, alpha=0.3)

    plt.tight_layout(rect=[0, 0, 1, 0.95])
    out_path = output_dir / "coevolution_results.png"
    fig.savefig(out_path, dpi=150, bbox_inches="tight")
    plt.close(fig)
    print(f"Saved: {out_path}")
    return out_path


def plot_sr_vs_difficulty(results: list, output_dir: Path):
    """Single clean figure: SR and difficulty on same axes."""
    gens = [r["generation"] for r in results]
    srs = [r["sr"] for r in results]
    diffs = [r.get("difficulty_score", 0) for r in results]

    fig, ax1 = plt.subplots(figsize=(10, 5))

    color_sr = "#2196F3"
    color_diff = "#FF5722"

    ax1.plot(gens, srs, "o-", color=color_sr, linewidth=2.5, markersize=8, label="Success Rate")
    ax1.set_xlabel("Generation", fontsize=13)
    ax1.set_ylabel("Success Rate", color=color_sr, fontsize=13)
    ax1.tick_params(axis="y", labelcolor=color_sr)
    ax1.set_ylim(-0.05, 1.05)
    ax1.yaxis.set_major_formatter(mticker.PercentFormatter(1.0))

    ax2 = ax1.twinx()
    ax2.plot(gens, diffs, "s--", color=color_diff, linewidth=2, markersize=7, label="Difficulty")
    ax2.set_ylabel("Difficulty (0-10)", color=color_diff, fontsize=13)
    ax2.tick_params(axis="y", labelcolor=color_diff)
    max_diff = max(diffs) if diffs else 10
    ax2.set_ylim(-0.2, max(max_diff * 1.2, 5))

    # ZPD band on SR axis
    ax1.axhspan(0.25, 0.75, alpha=0.08, color="green")

    lines1, labels1 = ax1.get_legend_handles_labels()
    lines2, labels2 = ax2.get_legend_handles_labels()
    ax1.legend(lines1 + lines2, labels1 + labels2, loc="upper left", fontsize=11)

    ax1.set_title("Co-Evolution: Success Rate vs Task Difficulty", fontsize=14)
    ax1.grid(True, alpha=0.3)

    out_path = output_dir / "sr_vs_difficulty.png"
    fig.savefig(out_path, dpi=150, bbox_inches="tight")
    plt.close(fig)
    print(f"Saved: {out_path}")
    return out_path


def main():
    if len(sys.argv) < 2:
        print("Usage: python -m co_evolve.visualize <run_dir_or_results_json>")
        sys.exit(1)

    results_path = resolve_results_path(sys.argv[1])
    results = load_results(str(results_path))
    output_dir = results_path.parent

    plot_coevolution(results, output_dir)
    plot_sr_vs_difficulty(results, output_dir)
    print(f"\nAll plots saved to: {output_dir}")


if __name__ == "__main__":
    main()
