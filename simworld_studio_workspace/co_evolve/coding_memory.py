"""Coding agent hierarchical memory with adversarial reward tracking.

Three levels (mirrors embodied agent's HierarchicalMemory):
  L1 (Working): last N design records (raw data)
  L2 (Episodic): patterns — which designs got high coding_reward
  L3 (Skills): distilled curriculum design principles

Adversarial reward: coding_reward = 1 - nav_sr (but 0 if nav_sr < 0.1)
High coding_reward = agent struggled but still learned (ZPD sweet spot).
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from .difficulty import compute_coding_reward

log = logging.getLogger(__name__)


@dataclass
class DesignRecord:
    epoch: int
    scene_id: str
    n_objects: int
    task_type: str
    min_path_cm: float
    max_path_cm: float
    difficulty_score: float
    nav_sr: float
    coding_reward: float
    reasoning: str


REFLECT_PROMPT = """\
You are analyzing your history as a curriculum designer for a navigation agent.

## REWARD SHAPE (updated — ignore any principles that reference reward=1-SR)
Reward = Gaussian(SR; peak=0.60, sigma=0.20) × (1 + 0.25·progress_bonus)
  - Peak reward is at SR≈0.60. SR=0.25 gives reward≈0.29 (NOT 0.75).
  - progress_bonus rewards raising difficulty at/above the running best.
  - SR<0.10 → reward=0 (catastrophic). SR>0.85 → reward<0.2 (too easy).
  - Optimal: SR in [0.45, 0.75] AT the highest difficulty the agent can still handle.

Your goal: keep SR in [0.45, 0.75] while difficulty MONOTONICALLY rises.
Principles that endorse SR<0.3 or advocate cyclic difficulty resets are WRONG under this reward.

## Your recent design records:
{history}

## Your current design principles:
{principles}

Based on the outcomes, extract 1-3 NEW curriculum design principles.
If any existing principle contradicts the new reward shape, write a replacement.
Focus on:
- Which difficulty increments actually kept SR in [0.45, 0.75]?
- When does adding an object cause SR to collapse <0.2 vs stay in band?
- How many epochs should a single scene be held before escalating?
- What's the best progression: path length vs object count vs blocked ratio?

Return ONLY a JSON array: ["principle 1", "principle 2"]"""


class CodingAgentMemory:
    """Hierarchical memory for the coding agent."""

    def __init__(
        self,
        path: str = "coding_memory.json",
        max_l3_principles: int = 8,
        reflect_every_n: int = 3,
        llm_call: Optional[Callable[[str], str]] = None,
    ):
        self._path = Path(path)
        self._max_l3 = max_l3_principles
        self._reflect_every = reflect_every_n
        self._llm_call = llm_call

        # L1: raw records (last 30)
        self.history: List[DesignRecord] = []
        # L3: distilled principles
        self.principles: List[str] = []
        self._load()

    def _load(self):
        if self._path.exists():
            try:
                data = json.loads(self._path.read_text(encoding="utf-8"))
                self.principles = data.get("principles", [])[-self._max_l3:]
                for h in data.get("history", []):
                    self.history.append(DesignRecord(**h))
                log.info("CodingMemory: loaded %d principles, %d records",
                         len(self.principles), len(self.history))
            except Exception as exc:
                log.warning("CodingMemory load failed: %s", exc)

    def _save(self):
        data = {
            "principles": self.principles[-self._max_l3:],
            "history": [
                {
                    "epoch": r.epoch, "scene_id": r.scene_id,
                    "n_objects": r.n_objects, "task_type": r.task_type,
                    "min_path_cm": r.min_path_cm, "max_path_cm": r.max_path_cm,
                    "difficulty_score": r.difficulty_score,
                    "nav_sr": r.nav_sr, "coding_reward": r.coding_reward,
                    "reasoning": r.reasoning,
                }
                for r in self.history[-30:]
            ],
        }
        self._path.write_text(json.dumps(data, indent=2), encoding="utf-8")

    def record(self, epoch: int, spec, nav_sr: float, difficulty_score: float):
        """Record a design + outcome.

        Reward uses the running best difficulty so the progress bonus reflects
        curriculum ascent, not a single epoch.
        """
        best_diff = max(
            (r.difficulty_score for r in self.history if r.nav_sr >= 0.2),
            default=0.0,
        )
        coding_reward = compute_coding_reward(
            nav_sr=nav_sr,
            difficulty=difficulty_score,
            best_difficulty=best_diff,
        )
        self.history.append(DesignRecord(
            epoch=epoch,
            scene_id=getattr(spec, 'scene_id', '?'),
            n_objects=len(getattr(spec, 'objects', [])),
            task_type=getattr(spec, 'task_type', 'pointnav'),
            min_path_cm=spec.min_path_cm,
            max_path_cm=spec.max_path_cm,
            difficulty_score=difficulty_score,
            nav_sr=nav_sr,
            coding_reward=coding_reward,
            reasoning=getattr(spec, 'reasoning', '')[:80],
        ))
        self._save()
        log.info("CodingMemory: epoch=%d difficulty=%.2f (best=%.2f) nav_sr=%.2f coding_reward=%.2f",
                 epoch, difficulty_score, best_diff, nav_sr, coding_reward)

    def maybe_reflect(self, epoch: int) -> bool:
        """Reflect every N epochs."""
        if not self._llm_call or len(self.history) < 3:
            return False
        if (epoch + 1) % self._reflect_every != 0:
            return False

        prompt = REFLECT_PROMPT.format(
            history=self._format_history(),
            principles=self._format_principles(),
        )

        try:
            raw = self._llm_call(prompt)
            new = self._parse_json_array(raw)
            if new:
                self.principles.extend(new)
                self.principles = self.principles[-self._max_l3:]
                self._save()
                log.info("CodingMemory: reflected, now %d principles", len(self.principles))
                return True
        except Exception as exc:
            log.warning("CodingMemory reflect failed: %s", exc)
        return False

    def get_prompt_section(self) -> str:
        """Format for injection into coding agent prompt."""
        parts = []

        # Recent performance summary
        if self.history:
            recent = self.history[-5:]
            avg_reward = sum(r.coding_reward for r in recent) / len(recent)
            avg_difficulty = sum(r.difficulty_score for r in recent) / len(recent)
            best = max(recent, key=lambda r: r.coding_reward)
            parts.append(
                f"Recent avg coding_reward: {avg_reward:.2f} "
                f"(target: 0.25-0.75, higher=harder tasks that agent partially solves)\n"
                f"Recent avg difficulty: {avg_difficulty:.0f}/100\n"
                f"Best design: epoch {best.epoch}, difficulty={best.difficulty_score:.0f}, "
                f"nav_sr={best.nav_sr:.0%}, reward={best.coding_reward:.2f}"
            )

        # L3 principles
        if self.principles:
            lines = "\n".join(f"  {i+1}. {p}" for i, p in enumerate(self.principles))
            parts.append(f"Design principles:\n{lines}")

        if not parts:
            return ""
        return "\n\n## YOUR DESIGN EXPERIENCE\n" + "\n\n".join(parts)

    def _format_history(self) -> str:
        lines = []
        for r in self.history[-10:]:
            reward_label = "GOOD" if 0.25 <= r.coding_reward <= 0.75 else (
                "TOO HARD" if r.coding_reward == 0 else
                "TOO EASY" if r.coding_reward < 0.25 else "GREAT"
            )
            lines.append(
                f"  Epoch {r.epoch}: diff={r.difficulty_score:.0f} "
                f"objects={r.n_objects} path=[{r.min_path_cm:.0f},{r.max_path_cm:.0f}] "
                f"nav_sr={r.nav_sr:.0%} reward={r.coding_reward:.2f} [{reward_label}]"
            )
        return "\n".join(lines) if lines else "(no history)"

    def _format_principles(self) -> str:
        if not self.principles:
            return "(none yet)"
        return "\n".join(f"  {i+1}. {p}" for i, p in enumerate(self.principles))

    @staticmethod
    def _parse_json_array(raw: str) -> List[str]:
        text = raw.strip()
        # Strip all thinking formats
        text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL).strip()
        text = re.sub(r"^.*?</think>", "", text, flags=re.DOTALL).strip()
        if "```" in text:
            text = "\n".join(l for l in text.split("\n") if not l.strip().startswith("```"))
        end = text.rfind("]")
        if end == -1:
            return []
        start = text.rfind("[", 0, end)
        if start == -1:
            return []
        try:
            arr = json.loads(text[start:end + 1])
            return [str(s).strip() for s in arr if isinstance(s, str) and s.strip()]
        except json.JSONDecodeError:
            return []
