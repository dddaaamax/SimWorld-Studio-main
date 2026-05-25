"""Aggregate per-episode summaries into a final ablation table.

Walks ``results/{model}/{modality}/{split}/map{NN}/w*/ep_*/summary.json``
and produces:

* ``results/aggregate.json`` — full nested summary
* ``results/table.csv``      — flat table (model, modality, split, SR, SPL, SoftSPL, n)
* console print in a readable table

Usage::

    python -m experiments.observation_modality_ablation.aggregate_results
"""

from __future__ import annotations

import csv
import json
import sys
from collections import defaultdict
from pathlib import Path

from .config import MODALITIES, MODELS, RESULTS_DIR


def _collect(root: Path) -> list:
    rows = []
    for model_tag in MODELS:
        for modality_tag in MODALITIES:
            for split_tag in ("seen", "unseen"):
                dirp = root / model_tag / modality_tag / split_tag
                if not dirp.exists():
                    continue
                ep_summaries = []
                for summary_path in dirp.rglob("ep_*/summary.json"):
                    try:
                        ep_summaries.append(json.loads(summary_path.read_text()))
                    except Exception:
                        pass
                rows.append({
                    "model": model_tag,
                    "modality": modality_tag,
                    "split": split_tag,
                    "n": len(ep_summaries),
                    "SR": (sum(1 for s in ep_summaries if s.get("SR", 0) > 0) / len(ep_summaries)) if ep_summaries else 0.0,
                    "SPL": (sum(s.get("SPL", 0) for s in ep_summaries) / len(ep_summaries)) if ep_summaries else 0.0,
                    "SoftSPL": (sum(s.get("SoftSPL", 0) for s in ep_summaries) / len(ep_summaries)) if ep_summaries else 0.0,
                    "avg_steps": (sum(s.get("steps", 0) for s in ep_summaries) / len(ep_summaries)) if ep_summaries else 0.0,
                })
    return rows


def main():
    rows = _collect(RESULTS_DIR)
    (RESULTS_DIR / "aggregate.json").write_text(json.dumps(rows, indent=2))

    csv_path = RESULTS_DIR / "table.csv"
    with csv_path.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["model", "modality", "split", "n", "SR", "SPL", "SoftSPL", "avg_steps"])
        w.writeheader()
        for r in rows:
            w.writerow(r)

    # Print table
    print(f"{'MODEL':<14}{'MODALITY':<12}{'SPLIT':<8}{'N':>4}  {'SR':>6}  {'SPL':>6}  {'SoftSPL':>8}  {'avg_steps':>10}")
    print("-" * 76)
    for r in rows:
        print(
            f"{r['model']:<14}{r['modality']:<12}{r['split']:<8}"
            f"{r['n']:>4}  {r['SR']:>6.3f}  {r['SPL']:>6.3f}  {r['SoftSPL']:>8.3f}  {r['avg_steps']:>10.1f}"
        )
    print(f"\nWritten: {RESULTS_DIR / 'aggregate.json'}")
    print(f"Written: {csv_path}")


if __name__ == "__main__":
    main()
