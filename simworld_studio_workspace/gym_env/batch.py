"""Multi-instance batch orchestrator.

Assumes one or more UE instances are already running on distinct
``UCV_PORT`` values.  Each worker subprocess connects to its assigned
port and runs an episode.

Why subprocesses (not threads): UnrealCV `unrealcv.Client` carries
state per process and the optional anthropic / openai SDKs do their
own connection pooling.  Subprocess isolation is the simplest correct
answer.

CLI::

    python -m gym_env.batch \
        --models claude,gpt \
        --ucv-ports 9000,9002 \
        --task pointnav --seed 42 --max-steps 40
"""

from __future__ import annotations

import argparse
import logging
import multiprocessing as mp
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

log = logging.getLogger(__name__)


@dataclass
class WorkerConfig:
    model: str
    ucv_port: int
    seed: int
    mcp_port: int = 55558
    start_pie: bool = True
    task: str = "pointnav"
    target_distance_cm: float = 2000.0
    target_filter: Optional[str] = None
    object_category: str = "OBJECT"
    max_steps: int = 40
    vision_depth: int = 3
    capture_rgb: bool = True
    run_name: Optional[str] = None
    extra: Dict[str, Any] = field(default_factory=dict)


def _worker(cfg: WorkerConfig) -> Dict[str, Any]:
    import logging as _l
    _l.basicConfig(
        level=_l.INFO,
        format=f"[w-{cfg.ucv_port}] %(asctime)s %(levelname)-5s %(name)s | %(message)s",
    )
    from .ucv_client import UCVClient
    from .mcp_client import MCPClient
    from .simworld_nav_env import SimWorldNavEnv
    from .episode_builder import sample_pointnav_episode, sample_objectnav_episode
    from .llm import make_llm
    from .runner import run_episode
    from .logger import EpisodeLogger

    ucv = UCVClient(port=cfg.ucv_port, name=f"env-{cfg.ucv_port}")
    mcp = MCPClient(port=cfg.mcp_port, name=f"env-{cfg.ucv_port}-mcp")
    if cfg.start_pie:
        try:
            mcp.start_pie(wait_seconds=5.0)
        except Exception as exc:
            print(f"WARN [{cfg.ucv_port}]: PIE start failed: {exc}")
    ucv.connect()
    if cfg.task == "pointnav":
        episode = sample_pointnav_episode(
            ucv, seed=cfg.seed,
            target_distance_cm=cfg.target_distance_cm,
            max_steps=cfg.max_steps,
        )
    else:
        substr = cfg.target_filter or ""
        episode = sample_objectnav_episode(
            ucv, seed=cfg.seed,
            target_filter=lambda name: substr in name,
            object_category=cfg.object_category,
            max_steps=cfg.max_steps,
        )

    env = SimWorldNavEnv(
        ucv_client=ucv,
        mcp_client=mcp,
        agent_name=f"GymNavAgent_{cfg.ucv_port}",
        capture_rgb=cfg.capture_rgb,
        ensure_pie=cfg.start_pie,
    )
    llm = make_llm(cfg.model)
    logger = EpisodeLogger(
        run_name=cfg.run_name or f"{llm.name}_p{cfg.ucv_port}_{episode.episode_id}",
        meta={
            "worker": cfg.ucv_port,
            "model": llm.name,
            "model_id": llm.model,
            "task": cfg.task,
            "episode_id": episode.episode_id,
            "config": cfg.__dict__,
        },
    )
    try:
        metrics = run_episode(
            env, llm, episode, logger,
            max_steps=cfg.max_steps,
            vision_history_depth=cfg.vision_depth,
        )
        return {"port": cfg.ucv_port, "model": cfg.model, **metrics}
    finally:
        env.close()
        logger.close()
        ucv.disconnect()


def run_batch(configs: List[WorkerConfig], n_parallel: int = 2) -> List[Dict[str, Any]]:
    log.info("starting batch: %d configs across %d workers", len(configs), n_parallel)
    ctx = mp.get_context("spawn")
    with ctx.Pool(processes=n_parallel) as pool:
        results = pool.map(_worker, configs)
    return results


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _csv(s: str) -> List[str]:
    return [x.strip() for x in s.split(",") if x.strip()]


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m gym_env.batch")
    p.add_argument("--models", default="claude",
                   help="Comma-separated LLM names (claude,gpt,gemini,qwen)")
    p.add_argument("--ucv-ports", default="9000",
                   help="Comma-separated UCV ports (one per UE instance)")
    p.add_argument("--seeds", default="42",
                   help="Comma-separated seeds")
    p.add_argument("--task", choices=["pointnav", "objectnav"], default="pointnav")
    p.add_argument("--target-distance", type=float, default=2000.0)
    p.add_argument("--target-filter", default=None)
    p.add_argument("--object-category", default="OBJECT")
    p.add_argument("--max-steps", type=int, default=40)
    p.add_argument("--vision-depth", type=int, default=3)
    p.add_argument("--no-rgb", action="store_true")
    p.add_argument("--parallel", type=int, default=2)
    return p


def main(argv: Optional[List[str]] = None) -> None:
    args = _build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-5s %(name)s | %(message)s",
    )
    models = _csv(args.models)
    ports = [int(p) for p in _csv(args.ucv_ports)]
    seeds = [int(s) for s in _csv(args.seeds)]

    configs: List[WorkerConfig] = []
    for i, m in enumerate(models):
        port = ports[i % len(ports)]
        seed = seeds[i % len(seeds)]
        configs.append(WorkerConfig(
            model=m,
            ucv_port=port,
            seed=seed,
            task=args.task,
            target_distance_cm=args.target_distance,
            target_filter=args.target_filter,
            object_category=args.object_category,
            max_steps=args.max_steps,
            vision_depth=args.vision_depth,
            capture_rgb=not args.no_rgb,
        ))

    results = run_batch(configs, n_parallel=args.parallel)
    print("\n== batch results ==")
    for r in results:
        print(r)


if __name__ == "__main__":
    main()
