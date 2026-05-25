"""Step 3: Compose train/test episode files for each ablation condition.

Reads per-map task files from tasks/ and creates:
  - conditions/test_episodes.json          (fixed across all conditions)
  - conditions/Nscenes/train_episodes.json (N = 1, 5, 10, 15)

Usage:
    python -m experiments.env_diversity_ablation.compose_conditions
"""

from __future__ import annotations

import json
import random
from pathlib import Path

from .config import (
    ALL_MAPS,
    TASKS_DIR,
    CONDITIONS_DIR,
    TEST_MAP_INDICES,
    TRAIN_MAP_INDICES,
    TRAIN_BUDGET,
    N_SCENES_CONDITIONS,
    CONDITION_SEED,
    map_label,
)


def load_map_tasks(map_idx: int) -> dict:
    """Load the task file for a map."""
    path = TASKS_DIR / f"{map_label(map_idx)}.json"
    if not path.exists():
        raise FileNotFoundError(f"No tasks for map {map_idx}: {path}")
    return json.loads(path.read_text())


def main():
    CONDITIONS_DIR.mkdir(parents=True, exist_ok=True)
    rng = random.Random(CONDITION_SEED)

    # ── 1. Build global test set ─────────────────────────────────────
    # From the 2 held-out test maps
    test_episodes = []
    for idx in TEST_MAP_INDICES:
        data = load_map_tasks(idx)
        for ep in data["episodes"]:
            ep["_source_map"] = map_label(idx)
            ep["_source_map_index"] = idx
            test_episodes.append(ep)

    # Also hold out 1 episode from each training map for per-scene eval
    train_map_pools = {}  # idx -> list of remaining episodes
    for idx in TRAIN_MAP_INDICES:
        data = load_map_tasks(idx)
        eps = data["episodes"]
        if len(eps) < 3:
            print(f"  WARNING: map {idx} has only {len(eps)} episodes, using all for training")
            train_map_pools[idx] = eps
            continue

        # Hold out the last episode for per-scene test
        held_out = eps[-1]
        held_out["_source_map"] = map_label(idx)
        held_out["_source_map_index"] = idx
        test_episodes.append(held_out)
        train_map_pools[idx] = eps[:-1]

    # Save test set
    test_path = CONDITIONS_DIR / "test_episodes.json"
    test_payload = {
        "split": "test",
        "n_episodes": len(test_episodes),
        "description": (
            f"{len(TEST_MAP_INDICES)} held-out maps + "
            f"1 held-out episode per training map"
        ),
        "episodes": test_episodes,
    }
    test_path.write_text(json.dumps(test_payload, indent=2))
    print(f"Test set: {len(test_episodes)} episodes -> {test_path}")

    # ── 2. Build training conditions ─────────────────────────────────
    # Available training maps (those with enough episodes)
    avail_train = [
        idx for idx in TRAIN_MAP_INDICES
        if idx in train_map_pools and len(train_map_pools[idx]) >= 2
    ]
    avail_train.sort()  # deterministic order
    print(f"Available training maps: {len(avail_train)}")

    for n_scenes in N_SCENES_CONDITIONS:
        if n_scenes > len(avail_train):
            print(f"  SKIP {n_scenes}-scene condition: only {len(avail_train)} maps available")
            continue

        cond_dir = CONDITIONS_DIR / f"{n_scenes}_scenes"
        cond_dir.mkdir(parents=True, exist_ok=True)

        # Sample which maps to use
        rng_cond = random.Random(CONDITION_SEED + n_scenes)
        selected_maps = sorted(rng_cond.sample(avail_train, n_scenes))

        # Distribute TRAIN_BUDGET across selected maps
        tasks_per_map = TRAIN_BUDGET // n_scenes
        remainder = TRAIN_BUDGET % n_scenes

        train_episodes = []
        map_allocation = {}

        for i, idx in enumerate(selected_maps):
            n = tasks_per_map + (1 if i < remainder else 0)
            pool = train_map_pools[idx]

            if len(pool) < n:
                print(f"  WARNING: map {idx} has {len(pool)} eps, need {n}. Using all.")
                n = len(pool)

            # Take the first n episodes (deterministic)
            chosen = pool[:n]
            for ep in chosen:
                ep["_source_map"] = map_label(idx)
                ep["_source_map_index"] = idx
                train_episodes.append(ep)

            map_allocation[map_label(idx)] = {
                "map_index": idx,
                "n_objects": ALL_MAPS[idx][1],
                "n_tasks": len(chosen),
            }

        # Shuffle training episodes (so the agent doesn't see all episodes
        # from one map in sequence)
        rng_shuffle = random.Random(CONDITION_SEED + n_scenes * 100)
        rng_shuffle.shuffle(train_episodes)

        # Save
        train_path = cond_dir / "train_episodes.json"
        train_payload = {
            "split": "train",
            "condition": f"{n_scenes}_scenes",
            "n_scenes": n_scenes,
            "n_episodes": len(train_episodes),
            "train_budget": TRAIN_BUDGET,
            "map_allocation": map_allocation,
            "episodes": train_episodes,
        }
        train_path.write_text(json.dumps(train_payload, indent=2))

        print(f"\n  Condition: {n_scenes} scenes")
        print(f"  Maps used: {selected_maps}")
        for lbl, info in map_allocation.items():
            print(f"    {lbl}: {info['n_tasks']} tasks ({info['n_objects']} objs)")
        print(f"  Total training episodes: {len(train_episodes)} -> {train_path}")

    print(f"\nDone! Conditions saved to {CONDITIONS_DIR}")


if __name__ == "__main__":
    main()
