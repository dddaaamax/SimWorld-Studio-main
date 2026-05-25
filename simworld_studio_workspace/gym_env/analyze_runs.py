"""Post-hoc analysis of gym_env experiment runs.

Reads ``runs/<dir>/episode.jsonl`` + ``llm_raw.jsonl`` + ``summary.json``
and prints a structured report: per-step reasoning, action trace,
distance curve, and final metrics.

Usage::

    PYTHONPATH=$TASK_GEN_DIR \
        python -m gym_env.analyze_runs runs/20260410_*claude*
"""

from __future__ import annotations

import glob
import json
import math
import os
import sys
from pathlib import Path
from typing import List


def analyze_run(run_dir: str) -> dict:
    """Parse one run directory and return a structured summary."""
    run_dir = Path(run_dir)
    summary_path = run_dir / "summary.json"
    episode_path = run_dir / "episode.jsonl"
    llm_path = run_dir / "llm_raw.jsonl"
    frames_dir = run_dir / "frames"

    result = {"run": run_dir.name, "steps": [], "metrics": {}, "frames": []}

    # Metrics
    if summary_path.exists():
        result["metrics"] = json.loads(summary_path.read_text(encoding="utf-8"))

    # Frames
    if frames_dir.exists():
        result["frames"] = sorted(f.name for f in frames_dir.glob("*.png"))

    # Episode steps
    steps_by_t = {}
    if episode_path.exists():
        for line in episode_path.read_text(encoding="utf-8").strip().splitlines():
            d = json.loads(line)
            t = d["t"]
            action = (d.get("action") or {}).get("tool", "START")
            obs = d.get("obs", {})
            pg = obs.get("pointgoal_with_gps_compass")
            info = d.get("info", {})
            step = {
                "t": t,
                "action": action,
                "reward": d.get("reward", 0),
                "d_goal": info.get("distance_to_goal_cm"),
                "cum_reward": info.get("cumulative_reward"),
                "bearing_deg": round(math.degrees(pg[1]), 1) if pg else None,
                "distance_cm": round(pg[0], 1) if pg else None,
            }
            steps_by_t[t] = step

    # LLM reasoning
    if llm_path.exists():
        for line in llm_path.read_text(encoding="utf-8").strip().splitlines():
            d = json.loads(line)
            t = d["t"]
            if t in steps_by_t:
                text = d.get("text", "") or ""
                # Extract first 200 chars of reasoning
                reasoning = text.strip()[:200]
                steps_by_t[t]["reasoning"] = reasoning
                steps_by_t[t]["tokens_in"] = d.get("usage", {}).get("input_tokens")
                steps_by_t[t]["tokens_out"] = d.get("usage", {}).get("output_tokens")

    result["steps"] = [steps_by_t[t] for t in sorted(steps_by_t.keys())]
    return result


def print_report(results: List[dict]) -> None:
    """Print a human-readable report."""
    print("=" * 70)
    print(f"  EXPERIMENT ANALYSIS — {len(results)} run(s)")
    print("=" * 70)

    all_sr = []
    for r in results:
        m = r["metrics"]
        sr = m.get("SR", 0)
        spl = m.get("SPL", 0)
        sspl = m.get("SoftSPL", 0)
        path = m.get("path_length_cm", 0)
        cum = m.get("cumulative_reward", 0)
        all_sr.append(sr)

        print(f"\n{'─'*70}")
        print(f"  {r['run']}")
        print(f"  SR={sr:.0f}  SPL={spl:.2f}  SoftSPL={sspl:.2f}  "
              f"path={path:.0f}cm  reward={cum:+.0f}")
        print(f"  frames: {len(r['frames'])} PNGs")
        print(f"{'─'*70}")

        # Step-by-step trace
        print(f"  {'t':>3}  {'action':<15} {'d_goal':>8} {'bearing':>8} "
              f"{'reward':>8} {'reasoning'}")
        print(f"  {'─'*3}  {'─'*15} {'─'*8} {'─'*8} {'─'*8} {'─'*40}")

        for s in r["steps"]:
            t = s["t"]
            act = s["action"]
            d = f"{s['d_goal']:.0f}" if s.get("d_goal") is not None else "?"
            b = f"{s['bearing_deg']:+.0f}°" if s.get("bearing_deg") is not None else "?"
            rew = f"{s['reward']:+.2f}" if s.get("reward") else ""
            reason = s.get("reasoning", "")
            # Truncate reasoning for display
            if len(reason) > 60:
                reason = reason[:57] + "..."
            print(f"  {t:>3}  {act:<15} {d:>8} {b:>8} {rew:>8} {reason}")

    # Aggregate
    n = len(all_sr)
    avg_sr = sum(all_sr) / max(n, 1)
    print(f"\n{'='*70}")
    print(f"  AGGREGATE: {n} episodes")
    print(f"  avg SR = {avg_sr:.2f}")
    print(f"  per-episode SR: {all_sr}")
    print(f"{'='*70}")


def main():
    patterns = sys.argv[1:] if len(sys.argv) > 1 else ["runs/*"]
    dirs = []
    for p in patterns:
        dirs.extend(sorted(glob.glob(p)))
    dirs = [d for d in dirs if os.path.isdir(d) and os.path.exists(os.path.join(d, "summary.json"))]

    if not dirs:
        print("No completed run directories found.", file=sys.stderr)
        sys.exit(1)

    results = [analyze_run(d) for d in dirs]
    print_report(results)


if __name__ == "__main__":
    main()
