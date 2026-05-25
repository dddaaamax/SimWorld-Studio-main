"""Per-map executor — amortises UE map-load cost across all (model, modality)
configurations for a single map.

Flow:
  1. UE is expected to already have the target map loaded as startup level
     (see launch_ue.sh + orchestrate_runs.sh).  We run with --skip-map-load.
  2. For each (model, modality) combination, call ``run_modality.run`` with
     map_indices = [<this map>].  Each call is resumable — it skips episodes
     whose ``summary.json`` already exists under
     ``results/{model}/{modality}/{split}/map{NN}/``.
  3. Targets are respawned once per (model, modality) combo using the
     pre-generated task JSON; agents are destroyed between combos to
     keep UE state clean.

Why we run (model × modality) inside a single map-load:
  * Loading any ablation map in UE costs ~60-90s.
  * There are 17 maps × 3 models × 3 modalities × 2 splits == 9 * 17
    outer iterations.  Re-loading the map every time would waste
    ~15 hours just on map-loads.  Inverting the loop amortises that
    cost down to 17 loads per UE instance (two UE instances in parallel).

Usage::

    python -m experiments.observation_modality_ablation.run_map_all_configs \\
        --map-index 3 --mcp-port 55558 --ucv-port 9002 [--only-split seen]
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from pathlib import Path

from .config import (
    MCP_HOST,
    MODALITIES,
    MODELS,
    RESULTS_DIR,
    TEST_MAP_INDICES,
    UCV_HOST,
)

log = logging.getLogger(__name__)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--map-index", type=int, required=True)
    p.add_argument("--mcp-port", type=int, default=55558)
    p.add_argument("--ucv-port", type=int, default=9002)
    p.add_argument("--mcp-host", default=MCP_HOST)
    p.add_argument("--ucv-host", default=UCV_HOST)
    p.add_argument("--only-model", default=None, choices=[None, *MODELS])
    p.add_argument("--only-modality", default=None,
                   choices=[None, *MODALITIES])
    p.add_argument("--only-split", default=None, choices=[None, "seen", "unseen"])
    args = p.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-5s %(name)s | %(message)s",
    )

    from gym_env.mcp_client import MCPClient
    from gym_env.ucv_client import UCVClient
    from gym_env.llm import make_llm
    from .run_modality import run as run_modality

    split = "unseen" if args.map_index in TEST_MAP_INDICES else "seen"
    if args.only_split is not None and args.only_split != split:
        print(f"[run_map] map {args.map_index} is split={split}; "
              f"--only-split={args.only_split} doesn't match — exiting")
        return

    # Connect once; run_modality re-uses these.
    mcp = MCPClient(host=args.mcp_host, port=args.mcp_port,
                    name=f"run-mcp-{args.ucv_port}")
    ucv = UCVClient(host=args.ucv_host, port=args.ucv_port,
                    name=f"run-ucv-{args.ucv_port}")

    models = [args.only_model] if args.only_model else list(MODELS)
    modalities = [args.only_modality] if args.only_modality else list(MODALITIES)

    print(f"=== map {args.map_index} split={split}  models={models}  modalities={modalities} ===")

    for model_tag in models:
        model_cfg = MODELS[model_tag]
        llm = make_llm(
            model_cfg["model"],
            model=model_cfg["model_id"],
            base_url=model_cfg["base_url"],
            api_key=model_cfg["api_key"],
            text_action_mode=True,
        )
        for modality_tag in modalities:
            print(f"\n--- {model_tag} / {modality_tag} / {split} / map{args.map_index:02d} ---")
            t0 = time.time()
            try:
                run_modality(
                    ucv, mcp, llm,
                    model_tag=model_tag,
                    modality_tag=modality_tag,
                    split_tag=split,
                    map_indices=[args.map_index],
                    results_root=RESULTS_DIR,
                    skip_map_load=True,  # we rely on the map being pre-loaded
                )
            except Exception:
                log.exception(
                    "run_modality crashed for %s/%s on map %d",
                    model_tag, modality_tag, args.map_index,
                )
            print(f"    elapsed: {time.time() - t0:.1f}s")


if __name__ == "__main__":
    main()
