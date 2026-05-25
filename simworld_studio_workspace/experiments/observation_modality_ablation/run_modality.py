"""Observation-modality ablation — runner.

One invocation = one (model, modality, split) trio running across all
maps in the split.  Inside: for each map we spawn the pre-picked target
object(s), run one episode per task, save results incrementally.

Resume semantics: every ``ep_<episode_id>_<agent>/summary.json`` under
the per-run results dir counts as done — those are skipped on rerun.

Usage::

    python -m experiments.observation_modality_ablation.run_modality \\
        --model qwen25_9b --modality depth --split seen \\
        --mcp-port 55558 --ucv-port 9002

The script spawns/teleports one ghost agent per wave (wave size = 1
for this ablation since each map has only 3 episodes and each target
has its own goal — ghost parallelism would require multiple targets
per wave which we don't have).
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from .config import (
    ALL_MAPS,
    EPISODES_PER_MAP,
    MAX_STEPS,
    MCP_HOST,
    MODALITIES,
    MODELS,
    RESULTS_DIR,
    TASKS_DIR,
    TEST_MAP_INDICES,
    TRAIN_MAP_INDICES,
    UCV_HOST,
    map_label,
    ue_asset_path,
)

log = logging.getLogger(__name__)


_HUMANOID_BP = "/Game/TrafficSystem/Pedestrian/Base_User_Agent.Base_User_Agent_C"
_DEFAULT_SPAWN_Z = 110.0


# ── Episode loader ────────────────────────────────────────────────────


def _load_tasks(map_idx: int) -> Optional[dict]:
    path = TASKS_DIR / f"{map_label(map_idx)}.json"
    if not path.exists():
        log.warning("no tasks file for map %d (%s)", map_idx, path)
        return None
    return json.loads(path.read_text())


# ── Map load helper (reuses generate_tasks) ──────────────────────────

def _load_map(mcp, asset_path: str, *, skip_load: bool = False) -> bool:
    from .generate_tasks import load_map_in_ue
    return load_map_in_ue(mcp, asset_path, skip_load=skip_load)


def _connect_ucv(ucv, max_attempts: int = 15) -> bool:
    time.sleep(3)
    for attempt in range(max_attempts):
        try:
            ucv.hard_reconnect()
            status = ucv.send("vget /unrealcv/status")
            if "error" not in status.lower():
                log.info("UCV connected: %s", status[:80])
                return True
        except Exception as exc:
            log.warning("UCV reconnect attempt %d: %s", attempt + 1, exc)
            time.sleep(3)
    return False


# ── Target respawn ────────────────────────────────────────────────────

def _respawn_targets_on_map(ucv, episodes: List[dict]) -> List[dict]:
    """Respawn each episode's target object at its recorded xy.

    Returns the episodes whose target successfully materialised.  The
    runner will only execute those.  We also rebuild the navmesh after
    spawning so path queries include the new obstacles.
    """
    from gym_env.object_pool import get_pool  # noqa: F401 — ensure pool is imported
    ok = []
    for ep in episodes:
        actor = ep["_target_actor_name"]
        asset = ep["_target_asset_path"]
        kind = ep["_target_kind"]
        x, y, z = ep["_target_xy_z"]
        try:
            if kind == "blueprint":
                ucv.spawn_bp_asset(asset, actor, location=(x, y, z),
                                   auto_repair_collision=False)
            else:
                ucv.spawn_static_mesh(asset, actor, location=(x, y, z))
        except Exception as exc:
            log.error("target respawn FAILED for %s: %s", actor, exc)
            continue
        ok.append(ep)
    time.sleep(2.0)
    live = set(ucv.vget_objects())
    final = [ep for ep in ok if ep["_target_actor_name"] in live]
    missed = [ep["_target_actor_name"] for ep in ok if ep not in final]
    if missed:
        log.warning("target missing after spawn verify: %s", missed)

    # Rebuild navmesh so geodesic stays consistent with generate_tasks.
    from nav_task.navmesh_interface import NavmeshNavigationInterface
    nav = NavmeshNavigationInterface(ucv)
    for _ in range(3):
        resp = nav.build_navmesh()
        if "error" not in str(resp).lower():
            break
        time.sleep(2)
    return final


def _cleanup_targets(ucv, episodes: List[dict]) -> None:
    for ep in episodes:
        try:
            ucv.destroy_actor(ep["_target_actor_name"])
        except Exception:
            pass


# ── Resume helper ─────────────────────────────────────────────────────

def _collect_existing(batch_dir: Path) -> Dict[str, dict]:
    found = {}
    if not batch_dir.exists():
        return found
    for summary_path in batch_dir.rglob("summary.json"):
        try:
            s = json.loads(summary_path.read_text())
            ep_id = s.get("episode_id")
            if ep_id:
                found[ep_id] = s
        except Exception:
            pass
    return found


# ── Runner ────────────────────────────────────────────────────────────

def run(
    ucv, mcp, llm, *,
    model_tag: str,
    modality_tag: str,
    split_tag: str,
    map_indices: List[int],
    results_root: Path,
    skip_map_load: bool = False,
):
    """Run all (map × episode) pairs for this (model, modality, split)."""
    from nav_task.episode import NavigationEpisode
    from gym_env.batch_runner import run_wave
    from gym_env.memory import NullMemory

    modality = MODALITIES[modality_tag]
    capture_rgb = bool(modality["capture_rgb"])
    capture_depth = bool(modality["capture_depth"])
    image_kind = modality["image_kind"]

    run_dir = results_root / model_tag / modality_tag / split_tag
    run_dir.mkdir(parents=True, exist_ok=True)
    meta_path = run_dir / "meta.json"
    meta = {
        "run_id": f"{model_tag}__{modality_tag}__{split_tag}",
        "model_tag": model_tag,
        "modality_tag": modality_tag,
        "split_tag": split_tag,
        "capture_rgb": capture_rgb,
        "capture_depth": capture_depth,
        "image_kind": image_kind,
        "model_id": MODELS[model_tag]["model_id"],
        "base_url": MODELS[model_tag]["base_url"],
        "map_indices": map_indices,
        "max_steps": MAX_STEPS,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    meta_path.write_text(json.dumps(meta, indent=2))

    all_results = []

    for map_idx in map_indices:
        batch_base = run_dir / f"map{map_idx:02d}"
        existing = _collect_existing(batch_base)

        data = _load_tasks(map_idx)
        if data is None:
            continue
        all_eps = data["episodes"]
        if len(existing) >= len(all_eps):
            log.info("map %d: all %d episodes already done (resume)",
                     map_idx, len(existing))
            all_results.extend(existing.values())
            continue

        # Load map (skip on the very first iteration if UE was launched
        # with this map — caller controls via --skip-map-load).
        if not _load_map(mcp, ue_asset_path(map_idx), skip_load=skip_map_load):
            log.error("map %d: load failed, skipping", map_idx)
            continue
        skip_map_load = False  # only apply once

        if not _connect_ucv(ucv):
            log.error("map %d: UCV reconnect failed, skipping", map_idx)
            continue

        # Respawn targets
        live_eps = _respawn_targets_on_map(ucv, all_eps)
        if not live_eps:
            log.error("map %d: no targets alive after respawn", map_idx)
            continue

        todo = [ep for ep in live_eps if ep["episode_id"] not in existing]
        if not todo:
            all_results.extend(existing.values())
            _cleanup_targets(ucv, live_eps)
            continue

        log.info("map %d: running %d/%d episodes (single wave)",
                 map_idx, len(todo), len(live_eps))

        nav_eps = [NavigationEpisode.from_dict({
            k: v for k, v in ep.items() if not k.startswith("_")
        }) for ep in todo]

        # All remaining episodes run in a single wave — one ghost
        # agent per episode, concurrent execution within the wave.
        # This avoids respawn-camera-index churn across episodes
        # on the same map.
        done_this_map = list(existing.values())
        results, _ = run_wave(
            ucv, mcp, llm, nav_eps,
            max_steps=MAX_STEPS,
            vision_depth=3,
            memory=NullMemory(),
            wandb_run=None,
            global_step=0,
            batch_dir=batch_base,
            save_frames=False,
            capture_rgb=capture_rgb,
            capture_depth=capture_depth,
            image_kind=image_kind,
            reuse_agents=False,
            skip_destroy=False,
        )
        done_this_map.extend(results)
        _write_map_summary(batch_base, map_idx, done_this_map, modality_tag, split_tag)

        all_results.extend(done_this_map)
        _cleanup_targets(ucv, live_eps)

        # Per-map progress line
        n_succ = sum(1 for r in done_this_map if r.get("SR", 0) > 0)
        log.info("  map %d summary: SR=%d/%d", map_idx, n_succ, len(done_this_map))

    # ── Overall summary ──────────────────────────────────────────────
    n = len(all_results)
    summary = {
        "run_id": meta["run_id"],
        "n_episodes": n,
        "SR": sum(1 for r in all_results if r.get("SR", 0) > 0) / n if n else 0.0,
        "SPL": sum(r.get("SPL", 0) for r in all_results) / n if n else 0.0,
        "SoftSPL": sum(r.get("SoftSPL", 0) for r in all_results) / n if n else 0.0,
        "avg_steps": sum(r.get("steps", 0) for r in all_results) / n if n else 0.0,
        "finished_at": datetime.now(timezone.utc).isoformat(),
    }
    (run_dir / "final_summary.json").write_text(json.dumps(summary, indent=2))
    print("\n=== RUN SUMMARY ===")
    for k, v in summary.items():
        print(f"  {k}: {v}")
    return summary


def _write_map_summary(batch_base: Path, map_idx: int, results: list,
                       modality_tag: str, split_tag: str) -> None:
    n = len(results)
    s = {
        "map_index": map_idx,
        "modality": modality_tag,
        "split": split_tag,
        "n_episodes": n,
        "SR": sum(1 for r in results if r.get("SR", 0) > 0) / n if n else 0.0,
        "SPL": sum(r.get("SPL", 0) for r in results) / n if n else 0.0,
        "SoftSPL": sum(r.get("SoftSPL", 0) for r in results) / n if n else 0.0,
        "finished_at": datetime.now(timezone.utc).isoformat(),
    }
    batch_base.mkdir(parents=True, exist_ok=True)
    (batch_base / "map_summary.json").write_text(json.dumps(s, indent=2))


# ── Main ──────────────────────────────────────────────────────────────


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--model", required=True, choices=list(MODELS))
    p.add_argument("--modality", required=True, choices=list(MODALITIES))
    p.add_argument("--split", required=True, choices=["seen", "unseen"])
    p.add_argument("--mcp-port", type=int, default=55558)
    p.add_argument("--ucv-port", type=int, default=9002)
    p.add_argument("--mcp-host", default=MCP_HOST)
    p.add_argument("--ucv-host", default=UCV_HOST)
    p.add_argument("--map-index", type=int, default=None,
                   help="Override: run only this map (must be in the --split)")
    p.add_argument("--skip-map-load", action="store_true",
                   help="Skip load for first map (UE already has it loaded)")
    args = p.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-5s %(name)s | %(message)s",
    )

    from gym_env.mcp_client import MCPClient
    from gym_env.ucv_client import UCVClient
    from gym_env.llm import make_llm

    model_cfg = MODELS[args.model]
    llm = make_llm(
        model_cfg["model"],
        model=model_cfg["model_id"],
        base_url=model_cfg["base_url"],
        api_key=model_cfg["api_key"],
        text_action_mode=True,
    )

    mcp = MCPClient(host=args.mcp_host, port=args.mcp_port, name=f"run-mcp-{args.ucv_port}")
    ucv = UCVClient(host=args.ucv_host, port=args.ucv_port, name=f"run-ucv-{args.ucv_port}")

    # Determine which maps to run for this split
    if args.map_index is not None:
        map_indices = [args.map_index]
    elif args.split == "seen":
        map_indices = TRAIN_MAP_INDICES
    else:
        map_indices = TEST_MAP_INDICES

    run(
        ucv, mcp, llm,
        model_tag=args.model,
        modality_tag=args.modality,
        split_tag=args.split,
        map_indices=map_indices,
        results_root=RESULTS_DIR,
        skip_map_load=args.skip_map_load,
    )


if __name__ == "__main__":
    main()
