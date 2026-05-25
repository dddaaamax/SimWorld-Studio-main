"""Command-line interface for the navigation task generator.

Usage
-----
PointNav (default):
    python -m nav_task --map roads.json --seed 42 --n-episodes 10 --output episodes.json

ObjectNav:
    python -m nav_task --map roads.json --task objectnav --category TRASH \\
        --elements elements.json --seed 42 --n-episodes 5 --output objnav.json

Train/test split (deterministic slice of the seeded generation order):
    python -m nav_task --map roads.json --seed 42 --n-episodes 30 \\
        --split 22,8 --train-out train.json --test-out test.json

Output format: n == 1 → single JSON object; n > 1 → JSON array.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .interface import UnrealCVNavigationInterface
from .generator import NavigationTaskGenerator


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m nav_task",
        description="Generate navigation task episodes from a SimWorld roads.json map.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("--map", required=True, metavar="PATH",
                   help="Path to roads.json")
    p.add_argument("--seed", type=int, default=42,
                   help="Master RNG seed")
    p.add_argument("--n-episodes", type=int, default=1, dest="n_episodes",
                   help="Number of episodes to generate")
    p.add_argument("--output", metavar="PATH", default="-",
                   help="Output file path (- = stdout). Ignored when --split is set.")
    p.add_argument("--split", metavar="N_TRAIN,N_TEST", default=None,
                   help=("Split the generated episodes into train/test by deterministic "
                         "slice (first N_TRAIN → train, next N_TEST → test). "
                         "Requires --train-out and --test-out; N_TRAIN+N_TEST must "
                         "equal --n-episodes."))
    p.add_argument("--train-out", metavar="PATH", default=None,
                   help="Output path for the training split (used with --split).")
    p.add_argument("--test-out", metavar="PATH", default=None,
                   help="Output path for the test split (used with --split).")
    p.add_argument("--min-path-length", type=float, default=1000.0,
                   dest="min_path_length",
                   help="Minimum path length in cm")
    p.add_argument("--max-path-length", type=float, default=None,
                   dest="max_path_length",
                   help="Maximum path length in cm (default: no limit)")
    p.add_argument("--max-retries", type=int, default=50, dest="max_retries",
                   help="Max resampling attempts per episode")
    p.add_argument("--sidewalk-offset", type=float, default=500.0,
                   dest="sidewalk_offset",
                   help="Sidewalk-offset in cm passed to Map")
    # ObjectNav flags
    p.add_argument("--task", choices=["pointnav", "objectnav"], default="pointnav",
                   help="Task type")
    p.add_argument("--category", metavar="NAME", default=None,
                   help="Object category for ObjectNav (e.g. TRASH, VEGETATION)")
    p.add_argument("--elements", metavar="PATH", default=None,
                   help="Path to elements.json (required for ObjectNav)")
    return p


def _parse_split(s: str, n_episodes: int) -> tuple[int, int]:
    try:
        parts = [int(x.strip()) for x in s.split(",")]
    except ValueError as exc:
        raise SystemExit(f"--split expects 'N_TRAIN,N_TEST', got {s!r}") from exc
    if len(parts) != 2:
        raise SystemExit(f"--split expects exactly two integers, got {s!r}")
    n_train, n_test = parts
    if n_train < 0 or n_test < 0:
        raise SystemExit("--split counts must be non-negative")
    if n_train + n_test != n_episodes:
        raise SystemExit(
            f"--split {n_train}+{n_test}={n_train + n_test} does not match "
            f"--n-episodes {n_episodes}"
        )
    return n_train, n_test


def _write_split(
    episodes: list,
    path: str,
    *,
    split: str,
    seed: int,
    map_file: str,
    index_range: tuple[int, int],
) -> None:
    """Write a split file with a small header + episode array."""
    payload = {
        "schema_version": "1.0",
        "split": split,
        "seed": seed,
        "map_file": map_file,
        "index_range": list(index_range),  # [start, end) in the full generation order
        "n_episodes": len(episodes),
        "episodes": [ep.to_dict() for ep in episodes],
    }
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, indent=2))
    print(f"Wrote {len(episodes)} episode(s) to {out} (split={split})", file=sys.stderr)


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)

    if args.task == "objectnav":
        if args.category is None:
            build_parser().error("--category is required for --task objectnav")
        if args.elements is None:
            build_parser().error("--elements is required for --task objectnav")

    if args.split is not None:
        if not args.train_out or not args.test_out:
            build_parser().error("--split requires both --train-out and --test-out")

    roads_file = str(Path(args.map).resolve())
    elements_file = str(Path(args.elements).resolve()) if args.elements else None

    interface = UnrealCVNavigationInterface(
        roads_file=roads_file,
        sidewalk_offset=args.sidewalk_offset,
        elements_file=elements_file,
    )
    generator = NavigationTaskGenerator(
        interface=interface,
        roads_file=roads_file,
        min_path_length_cm=args.min_path_length,
        max_path_length_cm=args.max_path_length,
        max_retries=args.max_retries,
    )

    if args.task == "objectnav":
        episodes = generator.generate_objectnav(
            seed=args.seed,
            object_category=args.category,
            n_episodes=args.n_episodes,
        )
    else:
        episodes = generator.generate(seed=args.seed, n_episodes=args.n_episodes)

    if args.split is not None:
        n_train, n_test = _parse_split(args.split, args.n_episodes)
        train_eps = episodes[:n_train]
        test_eps = episodes[n_train:n_train + n_test]
        _write_split(
            train_eps, args.train_out,
            split="train", seed=args.seed, map_file=roads_file,
            index_range=(0, n_train),
        )
        _write_split(
            test_eps, args.test_out,
            split="test", seed=args.seed, map_file=roads_file,
            index_range=(n_train, n_train + n_test),
        )
        return

    payload = (
        episodes[0].to_dict()
        if args.n_episodes == 1
        else [ep.to_dict() for ep in episodes]
    )
    json_str = json.dumps(payload, indent=2)

    if args.output == "-":
        print(json_str)
    else:
        out = Path(args.output)
        out.write_text(json_str)
        print(
            f"Wrote {args.n_episodes} episode(s) to {out}",
            file=sys.stderr,
        )
