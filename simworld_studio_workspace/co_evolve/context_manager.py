"""Global context manager for co-evolution.

Manages the information flow between coding agent and embodied agent:
  - Coding agent sees: nav agent's L2/L3 memory + performance history
  - Nav agent sees: only its own memory (L1/L2/L3)
  - Each agent's memory is independent but the context manager
    provides a unified view for the coding agent's prompt.
"""
from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional

log = logging.getLogger(__name__)


class CoEvolveContextManager:
    """Aggregates context from both agents for prompt construction."""

    def __init__(self):
        self.gen_results: List[Dict[str, Any]] = []
        self._all_episode_results: List[Dict[str, Any]] = []

    def add_generation(self, gen_record: Dict[str, Any]):
        self.gen_results.append(gen_record)
        self._all_episode_results.extend(gen_record.get("episode_results", []))

    def get_nav_context_for_coding_agent(self, nav_memory) -> Dict[str, str]:
        """Extract nav agent's memory state for the coding agent to see.

        Returns dict with keys: strategies, l3_skills, performance_summary
        """
        strategies = []
        l3_section = ""

        if hasattr(nav_memory, 'query'):
            strategies = nav_memory.query("", k=10)

        if hasattr(nav_memory, 'get_system_prompt_section'):
            l3_section = nav_memory.get_system_prompt_section()

        # Build performance summary — LAST EPOCH ONLY (no historical context).
        # The coding agent should react to fresh evidence; cumulative history
        # was causing it to anchor on stale failure narratives.
        perf_lines = []
        if self.gen_results:
            r = self.gen_results[-1]
            sr = r.get("sr", 0)
            spl = r.get("spl", 0)
            diff = r.get("difficulty_score", 0)
            scene = r.get("scene_id", "?")
            perf_lines.append(
                f"Epoch {r.get('generation','?')}: SR={sr:.0%} SPL={spl:.3f} "
                f"diff={diff:.1f} scene={scene} "
                f"path={r.get('min_path_cm',0):.0f}cm"
            )

        # Rolling / EMA SR — coding agent sees smoothed signal, not a single noisy datapoint.
        # With n=4 eps/gen, a single SR has only 5 possible values; any one reading is
        # mostly noise. EMA over last 5 epochs gives the coding agent a stable feedback
        # signal to act on.
        rolling_sr = 0.0
        ema_sr = 0.0
        if self.gen_results:
            recent = self.gen_results[-5:]
            rolling_sr = sum(r.get("sr", 0) for r in recent) / len(recent)
            alpha = 0.4
            ema_sr = self.gen_results[0].get("sr", 0)
            for r in self.gen_results[1:]:
                ema_sr = alpha * r.get("sr", 0) + (1 - alpha) * ema_sr

        # How many consecutive epochs has rolling SR been out of the ZPD band?
        ZPD_LO, ZPD_HI = 0.5, 0.75
        out_of_band_streak = 0
        last_sr_in_band = None
        for r in reversed(self.gen_results):
            sr_i = r.get("sr", 0)
            in_band = ZPD_LO <= sr_i <= ZPD_HI
            if last_sr_in_band is None:
                last_sr_in_band = in_band
            if in_band == last_sr_in_band:
                if not in_band:
                    out_of_band_streak += 1
            else:
                break

        # Scene stability — how many epochs on current scene
        current_scene_streak = 0
        if self.gen_results:
            last_scene = self.gen_results[-1].get("scene_id", "?")
            for r in reversed(self.gen_results):
                if r.get("scene_id", "?") == last_scene:
                    current_scene_streak += 1
                else:
                    break

        if self.gen_results:
            last_sr = self.gen_results[-1].get("sr", 0)
            rolling_summary = (
                f"Rolling-5 SR={rolling_sr:.2f}, EMA SR={ema_sr:.2f}, "
                f"last-epoch SR={last_sr:.2f}. "
                f"Out-of-ZPD streak={out_of_band_streak}. "
                f"Current scene kept for {current_scene_streak} epoch(s)."
            )
        else:
            rolling_summary = "(no data yet)"

        # Failure analysis
        recent_eps = self._all_episode_results[-12:]
        failures = [e for e in recent_eps if e.get("SR", 0) == 0]
        successes = [e for e in recent_eps if e.get("SR", 0) > 0]
        fail_summary = f"{len(failures)}/{len(recent_eps)} recent episodes failed"
        if failures:
            avg_steps = sum(e.get("steps", 0) for e in failures) / len(failures)
            fail_summary += f" (avg {avg_steps:.0f} steps before failure)"

        return {
            "strategies": "\n".join(f"  {i+1}. {s}" for i, s in enumerate(strategies)) if strategies else "(none)",
            "l3_skills": l3_section or "(none)",
            "performance_history": "\n".join(perf_lines) if perf_lines else "(no data)",
            "rolling_summary": rolling_summary,
            "rolling_sr": rolling_sr,
            "ema_sr": ema_sr,
            "out_of_band_streak": out_of_band_streak,
            "current_scene_streak": current_scene_streak,
            "failure_summary": fail_summary,
        }

    def get_recent_episodes(self, n: int = 8) -> List[Dict[str, Any]]:
        return self._all_episode_results[-n:]
