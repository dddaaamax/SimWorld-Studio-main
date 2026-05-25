"""Step 4: Run the ablation study — train + test for each condition.

Supports --resume to continue from where a previous run left off.
Episode results are saved incrementally; memory state is checkpointed
after each epoch.

Usage:
    python -m experiments.env_diversity_ablation.run_ablation \
        --condition 1 [5 10 15] \
        [--epochs 2] [--dry-run] [--resume]
"""

from __future__ import annotations

import argparse
import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional, Dict

from .config import (
    ALL_MAPS,
    CONDITIONS_DIR,
    RESULTS_DIR,
    TEST_MAP_INDICES,
    MCP_HOST,
    MCP_PORT,
    UCV_HOST,
    UCV_PORT,
    LLM_MODEL,
    LLM_MODEL_ID,
    LLM_BASE_URL,
    LLM_API_KEY,
    N_EPOCHS,
    MAX_STEPS,
    MEMORY_BACKEND,
    ue_asset_path,
    map_label,
)

log = logging.getLogger(__name__)

WAVE_SIZE = 5


# ── Helpers ──────────────────────────────────────────────────────────────

def load_condition_episodes(n_scenes: int) -> dict:
    cond_dir = CONDITIONS_DIR / f"{n_scenes}_scenes"
    train = json.loads((cond_dir / "train_episodes.json").read_text())
    test = json.loads((CONDITIONS_DIR / "test_episodes.json").read_text())
    return {"train": train, "test": test}


def load_map_in_ue(mcp, asset_path: str, *, skip_load: bool = False) -> bool:
    from .generate_tasks import load_map_in_ue as _load_map
    return _load_map(mcp, asset_path, skip_load=skip_load)


def group_episodes_by_map(episodes: list) -> dict:
    groups = {}
    for ep in episodes:
        idx = ep.get("_source_map_index", -1)
        groups.setdefault(idx, []).append(ep)
    return groups


def _summarize(results: list) -> dict:
    n = len(results)
    if n == 0:
        return {"n": 0, "SR": 0, "SPL": 0, "SoftSPL": 0}
    return {
        "n": n,
        "SR": sum(1 for r in results if r.get("SR", 0) > 0) / n,
        "SPL": sum(r.get("SPL", 0) for r in results) / n,
        "SoftSPL": sum(r.get("SoftSPL", 0) for r in results) / n,
        "avg_reward": sum(r.get("cumulative_reward", 0) for r in results) / n,
        "avg_steps": sum(r.get("steps", r.get("n_steps", 0)) for r in results) / n,
    }


def _collect_existing_results(batch_dir: Path) -> Dict[str, dict]:
    """Scan batch_dir for already-completed episode summaries."""
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


def _connect_ucv(ucv, max_attempts=15):
    """Hard reconnect UCV with retries."""
    time.sleep(5)
    for attempt in range(max_attempts):
        try:
            ucv.hard_reconnect()
            status = ucv.send("vget /unrealcv/status")
            if "error" not in status.lower():
                log.info("UCV connected and responsive: %s", status[:80])
                return True
        except Exception as exc:
            log.warning("UCV reconnect attempt %d: %s", attempt + 1, exc)
            time.sleep(3)
    return False


def _run_episodes_on_map(
    ucv, mcp, llm, nav_eps, *, memory, batch_dir_base, max_steps,
    existing_results=None,
):
    """Run episodes on the currently-loaded map, with resume support.

    Uses ghost agent parallelism within waves. Memory isolation is
    handled inside run_wave via per-agent trajectory tracking.

    Returns list of result dicts (including any reloaded from disk).
    """
    from nav_task.episode import NavigationEpisode
    from gym_env.batch_runner import run_wave

    existing = existing_results or {}
    todo_eps = []
    done_results = []
    for ep in nav_eps:
        ep_id = ep.episode_id
        if ep_id in existing:
            log.info("  SKIP (already done): %s", ep_id)
            done_results.append(existing[ep_id])
        else:
            todo_eps.append(ep)

    if not todo_eps:
        log.info("  All %d episodes already completed, skipping", len(nav_eps))
        return done_results

    log.info("  %d/%d episodes to run (%d already done)",
             len(todo_eps), len(nav_eps), len(done_results))

    # Run in waves with agent reuse on same map
    n_waves = (len(todo_eps) + WAVE_SIZE - 1) // WAVE_SIZE
    new_results = []
    for wave_start in range(0, len(todo_eps), WAVE_SIZE):
        wave_eps = todo_eps[wave_start:wave_start + WAVE_SIZE]
        wave_id = wave_start // WAVE_SIZE
        is_first = wave_start == 0
        is_last = wave_id == n_waves - 1
        log.info("  wave %d/%d: %d episodes (offset %d)",
                 wave_id, n_waves, len(wave_eps), wave_start)
        results, _ = run_wave(
            ucv, mcp, llm, wave_eps,
            max_steps=max_steps,
            vision_depth=3,
            memory=memory,
            wandb_run=None,
            global_step=0,
            batch_dir=batch_dir_base / f"w{wave_id}",
            save_frames=False,
            capture_rgb=True,
            reuse_agents=not is_first,
            skip_destroy=not is_last,
        )
        new_results.extend(results)

    return done_results + new_results


# ── Main runner ──────────────────────────────────────────────────────────

def run_condition(
    n_scenes: int,
    n_epochs: int,
    mcp,
    ucv,
    llm,
    *,
    dry_run: bool = False,
    resume: bool = False,
    only_map_idx: Optional[int] = None,
    only_split: Optional[str] = None,
    skip_map_load: bool = False,
):
    """Run train + test for one condition, with resume support."""
    from nav_task.episode import NavigationEpisode
    from gym_env.memory import build_memory, ReadOnlyMemory

    data = load_condition_episodes(n_scenes)
    train_data = data["train"]
    test_data = data["test"]

    # Stable run directory (no timestamp — allows resume)
    run_id = f"ablation_{n_scenes}scenes"
    run_dir = RESULTS_DIR / run_id
    run_dir.mkdir(parents=True, exist_ok=True)

    # Save experiment metadata
    meta_path = run_dir / "meta.json"
    meta = {
        "run_id": run_id,
        "condition": f"{n_scenes}_scenes",
        "n_train_episodes": train_data["n_episodes"],
        "n_test_episodes": test_data["n_episodes"],
        "n_epochs": n_epochs,
        "map_allocation": train_data.get("map_allocation", {}),
        "model": LLM_MODEL,
        "model_id": LLM_MODEL_ID,
        "memory": MEMORY_BACKEND,
        "max_steps": MAX_STEPS,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    meta_path.write_text(json.dumps(meta, indent=2))

    if dry_run:
        print(f"[DRY RUN] Condition {n_scenes} scenes:")
        print(f"  Train: {train_data['n_episodes']} episodes")
        print(f"  Test: {test_data['n_episodes']} episodes")
        print(f"  Epochs: {n_epochs}")
        print(f"  Output: {run_dir}")
        return

    # Build memory (persists across epochs). Keep the path under run_dir so
    # different models (via ABLATION_RESULTS_SUBDIR) don't cross-contaminate
    # strategies — default path is cwd-relative which would share one file
    # across all models running from the same workspace.
    memory = build_memory(
        MEMORY_BACKEND,
        agent_id=f"ablation_{n_scenes}scenes",
        config={"path": str(run_dir / "strategy_memory.json")},
        llm_model=LLM_MODEL_ID,
        llm_base_url=LLM_BASE_URL,
        llm_api_key=LLM_API_KEY,
    )

    # Check if any epochs are already complete (resume)
    all_epoch_results = []
    start_epoch = 0
    if resume:
        for e in range(n_epochs):
            summary_path = run_dir / f"epoch{e}_summary.json"
            if summary_path.exists():
                log.info("Resuming: epoch %d already complete, loading", e)
                all_epoch_results.append(json.loads(summary_path.read_text()))
                start_epoch = e + 1
            else:
                break

    for epoch in range(start_epoch, n_epochs):
        print(f"\n{'='*60}")
        print(f"EPOCH {epoch}/{n_epochs} — Condition: {n_scenes} scenes")
        print(f"{'='*60}")

        # ── Training ─────────────────────────────────────────────────
        train_eps = train_data["episodes"]
        map_groups = group_episodes_by_map(train_eps)
        if only_split == "test":
            map_groups = {}  # skip train phase
        elif only_map_idx is not None:
            map_groups = {k: v for k, v in map_groups.items() if k == only_map_idx}

        epoch_train_results = []
        for map_idx, eps in sorted(map_groups.items()):
            asset = ue_asset_path(map_idx)
            print(f"\n  Loading training map {map_idx} ({len(eps)} episodes)...")

            # Collect already-done episodes for this map
            batch_base = run_dir / f"epoch{epoch}_train_map{map_idx:02d}"
            existing = _collect_existing_results(batch_base) if resume else {}

            if len(existing) >= len(eps):
                log.info("  All episodes for map %d already done, skipping map load", map_idx)
                epoch_train_results.extend(existing.values())
                n_succ = sum(1 for r in existing.values() if r.get("SR", 0) > 0)
                print(f"    Train map {map_idx}: SR={n_succ}/{len(existing)} (resumed)")
                continue

            if not load_map_in_ue(mcp, asset, skip_load=skip_map_load):
                log.error("Failed to load map %d, skipping", map_idx)
                continue

            if not _connect_ucv(ucv):
                log.error("UCV failed to connect, skipping map %d", map_idx)
                continue

            nav_eps = [NavigationEpisode.from_dict(ep) for ep in eps]
            map_results = _run_episodes_on_map(
                ucv, mcp, llm, nav_eps,
                memory=memory,
                batch_dir_base=batch_base,
                max_steps=MAX_STEPS,
                existing_results=existing,
            )
            epoch_train_results.extend(map_results)

            n_succ = sum(1 for r in map_results if r.get("SR", 0) > 0)
            avg_spl = sum(r.get("SPL", 0) for r in map_results) / len(map_results) if map_results else 0
            print(f"    Train map {map_idx}: SR={n_succ}/{len(map_results)}, SPL={avg_spl:.3f}")

        # ── Testing (frozen memory) ─────────────────────────────────
        test_eps = test_data["episodes"]
        test_map_groups = group_episodes_by_map(test_eps)
        if only_split == "train":
            test_map_groups = {}  # skip test phase
        elif only_map_idx is not None:
            test_map_groups = {k: v for k, v in test_map_groups.items() if k == only_map_idx}
        ro_memory = ReadOnlyMemory(memory)

        epoch_test_results = []
        for map_idx, eps in sorted(test_map_groups.items()):
            asset = ue_asset_path(map_idx)
            print(f"\n  Loading test map {map_idx} ({len(eps)} episodes)...")

            batch_base = run_dir / f"epoch{epoch}_test_map{map_idx:02d}"
            existing = _collect_existing_results(batch_base) if resume else {}

            if len(existing) >= len(eps):
                log.info("  All test episodes for map %d already done", map_idx)
                epoch_test_results.extend(existing.values())
                n_succ = sum(1 for r in existing.values() if r.get("SR", 0) > 0)
                print(f"    Test map {map_idx}: SR={n_succ}/{len(existing)} (resumed)")
                continue

            if not load_map_in_ue(mcp, asset, skip_load=skip_map_load):
                log.error("Failed to load test map %d, skipping", map_idx)
                continue

            if not _connect_ucv(ucv):
                log.error("UCV failed to connect for test map %d, skipping", map_idx)
                continue

            nav_eps = [NavigationEpisode.from_dict(ep) for ep in eps]
            map_results = _run_episodes_on_map(
                ucv, mcp, llm, nav_eps,
                memory=ro_memory,
                batch_dir_base=batch_base,
                max_steps=MAX_STEPS,
                existing_results=existing,
            )
            epoch_test_results.extend(map_results)

            n_succ = sum(1 for r in map_results if r.get("SR", 0) > 0)
            avg_spl = sum(r.get("SPL", 0) for r in map_results) / len(map_results) if map_results else 0
            print(f"    Test map {map_idx}: SR={n_succ}/{len(map_results)}, SPL={avg_spl:.3f}")

        # ── Epoch summary (saved immediately) ────────────────────────
        epoch_summary = {
            "epoch": epoch,
            "train": _summarize(epoch_train_results),
            "test": _summarize(epoch_test_results),
        }
        all_epoch_results.append(epoch_summary)

        # Skip summary write when only processing a subset — the stats would
        # only reflect the current map, not the full epoch, and the stale
        # summary would short-circuit --resume on next iter.
        is_partial = (only_map_idx is not None) or (only_split is not None)
        if not is_partial:
            (run_dir / f"epoch{epoch}_summary.json").write_text(
                json.dumps(epoch_summary, indent=2)
            )

        print(f"\n  Epoch {epoch} summary:")
        print(f"    Train: SR={epoch_summary['train']['SR']:.3f} SPL={epoch_summary['train']['SPL']:.3f} SoftSPL={epoch_summary['train']['SoftSPL']:.3f}")
        print(f"    Test:  SR={epoch_summary['test']['SR']:.3f} SPL={epoch_summary['test']['SPL']:.3f} SoftSPL={epoch_summary['test']['SoftSPL']:.3f}")

    # ── Final summary ────────────────────────────────────────────────
    final = {
        "run_id": run_id,
        "condition": f"{n_scenes}_scenes",
        "epochs": all_epoch_results,
    }
    is_partial = (only_map_idx is not None) or (only_split is not None)
    if not is_partial:
        (run_dir / "final_results.json").write_text(json.dumps(final, indent=2))
    print(f"\nCondition {n_scenes} scenes complete. Results: {run_dir}")
    return final


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--condition", type=int, nargs="+", default=None,
                        help="Which conditions to run (1, 5, 10, 15). Default: all.")
    parser.add_argument("--epochs", type=int, default=N_EPOCHS)
    parser.add_argument("--resume", action="store_true",
                        help="Resume from existing results (skip completed episodes)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print plan without running")
    parser.add_argument("--ucv-host", default=UCV_HOST)
    parser.add_argument("--ucv-port", type=int, default=UCV_PORT)
    parser.add_argument("--mcp-host", default=MCP_HOST)
    parser.add_argument("--mcp-port", type=int, default=MCP_PORT)
    # Per-map UE-restart workflow: UE is launched with target map as startup
    # level; this flag tells run_ablation to skip load_map (a no-op that
    # wedges headless UE on first load) and process only the single map
    # indicated by --only-map-idx in the specified split.
    parser.add_argument("--only-map-idx", type=int, default=None,
                        help="Process only episodes from this map index")
    parser.add_argument("--only-split", choices=["train", "test"], default=None,
                        help="Process only train or test for --only-map-idx")
    parser.add_argument("--skip-map-load", action="store_true",
                        help="UE already has target map loaded; skip load_map call")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-5s %(name)s | %(message)s",
    )

    conditions = args.condition or [1, 5, 10, 15]

    if args.dry_run:
        for n in conditions:
            run_condition(n, args.epochs, None, None, None, dry_run=True)
        return

    from gym_env.mcp_client import MCPClient
    from gym_env.ucv_client import UCVClient
    from gym_env.llm import make_llm

    mcp = MCPClient(host=args.mcp_host, port=args.mcp_port)
    ucv = UCVClient(host=args.ucv_host, port=args.ucv_port)
    llm = make_llm(LLM_MODEL, model=LLM_MODEL_ID, base_url=LLM_BASE_URL, api_key=LLM_API_KEY,
                    text_action_mode=True)

    all_results = {}
    for n in conditions:
        result = run_condition(
            n, args.epochs, mcp, ucv, llm, resume=args.resume,
            only_map_idx=args.only_map_idx,
            only_split=args.only_split,
            skip_map_load=args.skip_map_load,
        )
        all_results[f"{n}_scenes"] = result

    # Comparative summary
    print("\n" + "=" * 70)
    print("ABLATION RESULTS SUMMARY")
    print("=" * 70)
    print(f"{'Condition':<15} {'Train SR':>10} {'Train SPL':>10} {'Test SR':>10} {'Test SPL':>10}")
    print("-" * 70)
    for cond, res in sorted(all_results.items()):
        if res and res.get("epochs"):
            last = res["epochs"][-1]
            print(
                f"{cond:<15} "
                f"{last['train']['SR']:>10.3f} "
                f"{last['train']['SPL']:>10.3f} "
                f"{last['test']['SR']:>10.3f} "
                f"{last['test']['SPL']:>10.3f}"
            )

    # Save combined results
    combined_path = RESULTS_DIR / "ablation_combined.json"
    combined_path.write_text(json.dumps(all_results, indent=2))
    print(f"\nCombined results: {combined_path}")


if __name__ == "__main__":
    main()
