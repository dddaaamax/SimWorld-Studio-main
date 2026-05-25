"""Feedback composers: format embodied agent data for the coding agent.

All feedback is verbalized (natural language). The coding agent prompt
receives this as context to make informed task design decisions.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

log = logging.getLogger(__name__)


def format_performance_history(gen_results: List[Dict[str, Any]]) -> str:
    """Format the per-generation results into a readable history table."""
    if not gen_results:
        return "(no history yet)"

    lines = ["Gen | SR    | SPL   | AvgSteps | PathRange(cm) | Difficulty"]
    lines.append("----|-------|-------|----------|---------------|----------")
    for r in gen_results[-8:]:  # last 8 generations
        sr = r.get("sr", 0)
        spl = r.get("spl", 0)
        avg_steps = r.get("avg_steps", 0)
        min_p = r.get("min_path_cm", 0)
        max_p = r.get("max_path_cm", 0)
        diff = r.get("difficulty_score", 0)
        gen = r.get("generation", "?")
        lines.append(
            f" {gen:>2} | {sr:.0%} | {spl:.3f} | {avg_steps:>8.1f} | "
            f"{min_p:.0f}-{max_p:.0f} | {diff:.2f}"
        )
    return "\n".join(lines)


def format_strategies(strategies: List[str]) -> str:
    """Format the nav agent's learned strategies."""
    if not strategies:
        return "(no strategies learned yet)"
    return "\n".join(f"  {i+1}. {s}" for i, s in enumerate(strategies))


def format_failure_patterns(episode_results: List[Dict[str, Any]]) -> str:
    """Extract and format failure patterns from recent episode results."""
    failures = [r for r in episode_results if r.get("SR", 0) == 0]
    if not failures:
        return "(no failures in recent generation)"

    lines = []
    for i, f in enumerate(failures[:5]):  # max 5 failure descriptions
        ep_id = f.get("episode_id", "?")
        steps = f.get("steps", 0)
        reason = f.get("ended_reason", "unknown")
        d_goal = f.get("path_length_cm", "?")
        lines.append(
            f"  - Episode {ep_id}: {reason} after {steps} steps, "
            f"path walked: {d_goal}cm"
        )

    n_fail = len(failures)
    n_total = len(episode_results)
    lines.insert(0, f"Failed {n_fail}/{n_total} episodes:")
    return "\n".join(lines)


def format_generation_summary(
    generation: int,
    sr: float,
    spl: float,
    avg_steps: float,
    difficulty: float,
    task_design_reasoning: str,
) -> str:
    """One-line summary for logging."""
    return (
        f"Gen {generation}: SR={sr:.0%} SPL={spl:.3f} "
        f"steps={avg_steps:.1f} difficulty={difficulty:.2f} "
        f"| {task_design_reasoning}"
    )
