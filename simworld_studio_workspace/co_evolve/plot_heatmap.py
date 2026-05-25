"""Plot a step-vs-difficulty heatmap similar to the attached reference.

Usage:
    python -m co_evolve.plot_heatmap --demo \
        --output runs/co_evolve/mixed_rollout_correctness_demo.png

    python -m co_evolve.plot_heatmap \
        --input path/to/heatmap.json \
        --output runs/co_evolve/my_heatmap.png

Input JSON format:
{
  "values": [[0, 10, 20], [5, 15, 25]],
  "x_label": "Step",
  "y_label": "Problem Difficulty",
  "colorbar_label": "Problems with Mixed Rollout Correctness (%)",
  "title": "Optional title"
}
"""
from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
from typing import Any, Sequence

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt


DEFAULT_X_LABEL = "Step"
DEFAULT_Y_LABEL = "Problem Difficulty"
DEFAULT_COLORBAR_LABEL = "Problems with Mixed Rollout Correctness (%)"
DEFAULT_CURRICULUM_X_LABEL = "Epoch"
DEFAULT_CURRICULUM_COLORBAR_LABEL = "Relative Curriculum Focus (%)"
DEFAULT_RAW_X_LABEL = "Generation"
DEFAULT_RAW_COLORBAR_LABEL = "Episode Success Rate (%)"


def load_heatmap_spec(path: str | Path) -> dict[str, Any]:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def load_curriculum_history(path: str | Path) -> list[dict[str, Any]]:
    history = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(history, list):
        raise ValueError("curriculum JSON must be a list of epoch records")
    return history


def load_coevolution_results(path: str | Path) -> list[dict[str, Any]]:
    results = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(results, list):
        raise ValueError("all_results JSON must be a list of generation records")
    return results


def build_zero_distinct_blues_cmap():
    return matplotlib.colors.LinearSegmentedColormap.from_list(
        "zero_distinct_blues",
        ["#e8f1fb", "#c6dbef", "#6baed6", "#2171b5", "#08306b"],
        N=256,
    )


def build_demo_heatmap(n_difficulties: int = 31, n_steps: int = 61) -> list[list[float]]:
    """Create a smooth demo surface with the same visual shape as the reference.

    The surface is intentionally synthetic: strong correctness in the lower-left,
    plus a diagonal ridge that moves toward higher difficulty as steps increase.
    """
    values: list[list[float]] = []
    for difficulty in range(n_difficulties):
        row: list[float] = []
        for step in range(n_steps):
            diagonal_center = 4.0 + 0.23 * step + 0.0015 * step * step
            diagonal_band = 58.0 * math.exp(
                -((difficulty - diagonal_center) ** 2) / (2.0 * 2.6 * 2.6)
            )
            lower_left_mass = 95.0 * math.exp(-difficulty / 8.5) * math.exp(-step / 18.0)
            shoulder = 22.0 * math.exp(
                -((difficulty - (diagonal_center - 4.5)) ** 2) / (2.0 * 4.0 * 4.0)
            ) * math.exp(-step / 90.0)

            # Small deterministic texture keeps the demo from looking too flat.
            texture = 2.5 * (math.sin(step / 4.8 + difficulty / 3.5) + 1.0)
            value = min(100.0, max(0.0, lower_left_mass + diagonal_band + shoulder + texture))
            row.append(value)
        values.append(row)
    return values


def _validate_values(values: Sequence[Sequence[float]]) -> list[list[float]]:
    matrix = [list(row) for row in values]
    if not matrix or not matrix[0]:
        raise ValueError("heatmap values must be a non-empty 2D matrix")
    width = len(matrix[0])
    for row in matrix:
        if len(row) != width:
            raise ValueError("all heatmap rows must have the same length")
    return matrix


def build_curriculum_heatmap(
    history: Sequence[dict[str, Any]],
    *,
    bin_size: float = 0.25,
    sigma: float = 0.4,
) -> tuple[list[list[float]], dict[str, list[float]]]:
    if not history:
        raise ValueError("curriculum history must contain at least one epoch")
    if bin_size <= 0:
        raise ValueError("bin_size must be positive")

    entries = sorted(history, key=lambda item: int(item["epoch"]))
    epochs = [float(int(item["epoch"])) for item in entries]
    difficulties = [float(item["difficulty"]) for item in entries]
    rolling_success = [float(item.get("rolling_sr", item.get("sr", 0.5))) for item in entries]

    max_difficulty = max(difficulties)
    y_max = math.ceil((max_difficulty + 0.5) / bin_size) * bin_size
    n_bins = int(round(y_max / bin_size)) + 1
    difficulty_bins = [index * bin_size for index in range(n_bins)]

    values: list[list[float]] = []
    for difficulty_bin in difficulty_bins:
        row: list[float] = []
        for difficulty, sr in zip(difficulties, rolling_success):
            frontier = 72.0 * math.exp(-((difficulty_bin - difficulty) ** 2) / (2.0 * sigma * sigma))
            mastered_tail = 24.0 * math.exp(-(difficulty - difficulty_bin) / 1.9) if difficulty_bin <= difficulty else 0.0
            baseline = 5.0 * math.exp(-difficulty_bin / max(1.0, y_max / 3.5))
            value = (frontier + mastered_tail + baseline) * (0.82 + 0.28 * sr)
            row.append(min(100.0, max(0.0, value)))
        values.append(row)

    x_ticks = _build_regular_ticks(epochs, step=5.0)
    y_ticks = _build_regular_ticks(difficulty_bins, step=1.0)
    return values, {
        "epochs": epochs,
        "difficulty_bins": difficulty_bins,
        "x_ticks": x_ticks,
        "y_ticks": y_ticks,
    }


def build_raw_results_heatmap(
    results: Sequence[dict[str, Any]],
    *,
    bin_size: float = 0.25,
) -> tuple[list[list[float]], dict[str, list[float]]]:
    if not results:
        raise ValueError("all_results must contain at least one generation")
    if bin_size <= 0:
        raise ValueError("bin_size must be positive")

    entries = sorted(results, key=lambda item: int(item["generation"]))
    generations = [float(int(item["generation"])) for item in entries]

    raw_difficulties: list[float] = []
    for item in entries:
        task_difficulties = item.get("task_difficulties", [])
        if task_difficulties:
            raw_difficulties.extend(float(task.get("total", 0.0)) for task in task_difficulties)
        else:
            raw_difficulties.append(float(item.get("difficulty_score", 0.0)))

    max_difficulty = max(raw_difficulties) if raw_difficulties else 0.0
    y_max = math.ceil((max_difficulty + 0.5) / bin_size) * bin_size
    n_bins = int(round(y_max / bin_size)) + 1
    difficulty_bins = [index * bin_size for index in range(n_bins)]
    values = [[-1.0 for _ in generations] for _ in difficulty_bins]

    for generation_index, item in enumerate(entries):
        task_difficulties = item.get("task_difficulties", [])
        episode_results = item.get("episode_results", [])
        n_pairs = min(len(task_difficulties), len(episode_results))
        bucketed_values: dict[int, list[float]] = {}

        if n_pairs > 0:
            for pair_index in range(n_pairs):
                difficulty = float(task_difficulties[pair_index].get("total", item.get("difficulty_score", 0.0)))
                sr_value = float(episode_results[pair_index].get("SR", 0.0)) * 100.0
                bucket_index = int(round(difficulty / bin_size))
                bucket_index = max(0, min(bucket_index, len(difficulty_bins) - 1))
                bucketed_values.setdefault(bucket_index, []).append(sr_value)
        else:
            difficulty = float(item.get("difficulty_score", 0.0))
            sr_value = float(item.get("sr", 0.0)) * 100.0
            bucket_index = int(round(difficulty / bin_size))
            bucket_index = max(0, min(bucket_index, len(difficulty_bins) - 1))
            bucketed_values[bucket_index] = [sr_value]

        for bucket_index, sr_values in bucketed_values.items():
            values[bucket_index][generation_index] = sum(sr_values) / len(sr_values)

    x_ticks = _build_regular_ticks(generations, step=5.0)
    y_ticks = _build_regular_ticks(difficulty_bins, step=1.0)
    return values, {
        "generations": generations,
        "difficulty_bins": difficulty_bins,
        "x_ticks": x_ticks,
        "y_ticks": y_ticks,
    }


def _build_regular_ticks(values: Sequence[float], *, step: float) -> list[float]:
    if not values:
        return []

    minimum = values[0]
    maximum = values[-1]
    first_tick = math.ceil(minimum / step) * step
    ticks: list[float] = []
    current = first_tick
    while current <= maximum + 1e-9:
        ticks.append(float(round(current, 6)))
        current += step

    if not ticks or abs(ticks[0] - minimum) > 1e-9:
        ticks.insert(0, float(round(minimum, 6)))
    if abs(ticks[-1] - maximum) > 1e-9:
        ticks.append(float(round(maximum, 6)))
    return ticks


def plot_step_difficulty_heatmap(
    values: Sequence[Sequence[float]],
    output_path: str | Path,
    *,
    x_label: str = DEFAULT_X_LABEL,
    y_label: str = DEFAULT_Y_LABEL,
    colorbar_label: str = DEFAULT_COLORBAR_LABEL,
    title: str | None = None,
    cmap: Any = "Blues",
    x_values: Sequence[float] | None = None,
    y_values: Sequence[float] | None = None,
    x_ticks: Sequence[float] | None = None,
    y_ticks: Sequence[float] | None = None,
    figsize: tuple[float, float] = (4.2, 5.0),
    vmin: float = 0.0,
    vmax: float = 100.0,
    under_color: str | None = None,
) -> Path:
    matrix = _validate_values(values)
    height = len(matrix)
    width = len(matrix[0])

    extent = [0, width - 1, 0, height - 1]
    if x_values is not None:
        if len(x_values) != width:
            raise ValueError("x_values length must match heatmap width")
        x_step = (x_values[1] - x_values[0]) if len(x_values) > 1 else 1.0
        extent[0] = x_values[0] - x_step / 2.0
        extent[1] = x_values[-1] + x_step / 2.0
    if y_values is not None:
        if len(y_values) != height:
            raise ValueError("y_values length must match heatmap height")
        y_step = (y_values[1] - y_values[0]) if len(y_values) > 1 else 1.0
        extent[2] = y_values[0] - y_step / 2.0
        extent[3] = y_values[-1] + y_step / 2.0

    cmap_obj = matplotlib.colormaps.get_cmap(cmap) if isinstance(cmap, str) else cmap
    if under_color is not None:
        cmap_obj = cmap_obj.copy()
        cmap_obj.set_under(under_color)

    fig, ax = plt.subplots(figsize=figsize)
    image = ax.imshow(
        matrix,
        origin="lower",
        aspect="auto",
        cmap=cmap_obj,
        vmin=vmin,
        vmax=vmax,
        interpolation="nearest",
        extent=extent,
    )

    ax.set_xlabel(x_label, fontsize=12)
    ax.set_ylabel(y_label, fontsize=12)
    ax.set_xticks(list(x_ticks) if x_ticks is not None else [0, (width - 1) // 2, width - 1])
    ax.set_yticks(list(y_ticks) if y_ticks is not None else [0, (height - 1) // 2, height - 1])
    ax.tick_params(labelsize=10)
    if title:
        ax.set_title(title, fontsize=13)

    colorbar = fig.colorbar(image, ax=ax, pad=0.05)
    colorbar.set_label(colorbar_label, fontsize=12)
    colorbar.ax.tick_params(labelsize=10)

    out_path = Path(output_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    fig.tight_layout()
    fig.savefig(out_path, dpi=180, bbox_inches="tight")
    plt.close(fig)
    return out_path


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="python -m co_evolve.plot_heatmap",
        description="Plot a step-vs-difficulty heatmap similar to the reference figure.",
    )
    parser.add_argument("--input", help="Path to a JSON file containing a 2D 'values' matrix")
    parser.add_argument("--demo", action="store_true", help="Generate a demo heatmap without input data")
    parser.add_argument("--curriculum-json", help="Path to curriculum history JSON with epoch and difficulty fields")
    parser.add_argument("--all-results", help="Path to live co_evolve all_results.json")
    parser.add_argument("--output", required=True, help="Where to save the PNG")
    parser.add_argument("--title", default=None)
    parser.add_argument("--x-label", default=DEFAULT_X_LABEL)
    parser.add_argument("--y-label", default=DEFAULT_Y_LABEL)
    parser.add_argument("--colorbar-label", default=DEFAULT_COLORBAR_LABEL)
    parser.add_argument("--bin-size", type=float, default=0.25, help="Difficulty bin size for curriculum heatmaps")
    args = parser.parse_args(argv)

    selected_modes = sum(bool(option) for option in (args.demo, args.input, args.curriculum_json, args.all_results))
    if selected_modes != 1:
        parser.error("choose exactly one of --demo, --input, --curriculum-json, or --all-results")

    if args.input:
        spec = load_heatmap_spec(args.input)
        values = spec["values"]
        x_label = spec.get("x_label", args.x_label)
        y_label = spec.get("y_label", args.y_label)
        colorbar_label = spec.get("colorbar_label", args.colorbar_label)
        title = spec.get("title", args.title)
        cmap = "Blues"
        x_values = None
        y_values = None
        x_ticks = None
        y_ticks = None
        figsize = (4.2, 5.0)
        under_color = None
    elif args.all_results:
        results = load_coevolution_results(args.all_results)
        values, axes = build_raw_results_heatmap(results, bin_size=args.bin_size)
        x_label = DEFAULT_RAW_X_LABEL if args.x_label == DEFAULT_X_LABEL else args.x_label
        y_label = args.y_label
        colorbar_label = (
            DEFAULT_RAW_COLORBAR_LABEL
            if args.colorbar_label == DEFAULT_COLORBAR_LABEL
            else args.colorbar_label
        )
        title = args.title or "Raw Episode Success by Difficulty Bin"
        cmap = build_zero_distinct_blues_cmap()
        x_values = axes["generations"]
        y_values = axes["difficulty_bins"]
        x_ticks = axes["x_ticks"]
        y_ticks = axes["y_ticks"]
        figsize = (5.8, 5.0)
        under_color = "#ffffff"
    elif args.curriculum_json:
        history = load_curriculum_history(args.curriculum_json)
        values, axes = build_curriculum_heatmap(history, bin_size=args.bin_size)
        x_label = DEFAULT_CURRICULUM_X_LABEL if args.x_label == DEFAULT_X_LABEL else args.x_label
        y_label = args.y_label
        colorbar_label = (
            DEFAULT_CURRICULUM_COLORBAR_LABEL
            if args.colorbar_label == DEFAULT_COLORBAR_LABEL
            else args.colorbar_label
        )
        title = args.title or "Curriculum Difficulty Frontier"
        cmap = "Blues"
        x_values = axes["epochs"]
        y_values = axes["difficulty_bins"]
        x_ticks = axes["x_ticks"]
        y_ticks = axes["y_ticks"]
        figsize = (5.6, 5.0)
        under_color = None
    else:
        values = build_demo_heatmap()
        x_label = args.x_label
        y_label = args.y_label
        colorbar_label = args.colorbar_label
        title = args.title
        cmap = "Blues"
        x_values = None
        y_values = None
        x_ticks = None
        y_ticks = None
        figsize = (4.2, 5.0)
        under_color = None

    out_path = plot_step_difficulty_heatmap(
        values,
        args.output,
        x_label=x_label,
        y_label=y_label,
        colorbar_label=colorbar_label,
        title=title,
        cmap=cmap,
        x_values=x_values,
        y_values=y_values,
        x_ticks=x_ticks,
        y_ticks=y_ticks,
        figsize=figsize,
        under_color=under_color,
    )
    print(f"Saved: {out_path}")


if __name__ == "__main__":
    main()