"""Strategy memory: episode-level reflection that produces transferable navigation principles.

Unlike mem0 (per-step fact extraction, slow, noisy), this backend:

1. Records raw trajectory during an episode (zero LLM calls).
2. At episode end, asks the LLM to reflect on the FULL trajectory and
   distill 1-2 transferable navigation principles.
3. Stores at most `max_strategies` principles (sliding window).
4. On recall, returns ALL stored principles (they're few and high-quality).
5. Principles are injected into the system prompt as "Navigation Lessons",
   not mixed into the user turn.

Zero extra dependencies (no chromadb, no fastembed, no mem0).
One LLM call per episode (not per step).
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

log = logging.getLogger(__name__)

REFLECTION_PROMPT = """\
Extract 1-2 transferable navigation principles from this episode.

Trajectory:
{trajectory}

Outcome: {outcome}

Rules: general strategies (no specific coords), actionable, 1-2 sentences each.

Return ONLY a JSON array of strings, e.g.: ["principle 1", "principle 2"]"""


class StrategyMemory:
    """Episode-level strategy memory with LLM reflection."""

    name = "strategy"

    def __init__(
        self,
        path: str = "strategy_memory.json",
        max_strategies: int = 5,
        llm_call: Optional[Callable] = None,
        warmup: Optional[List[str]] = None,
    ) -> None:
        self._path = Path(path)
        self._max = max_strategies
        self._strategies: List[str] = []
        self._trajectory: List[str] = []
        self._llm_call = llm_call
        # Pinned warmup strategies (e.g. distilled L3 skills from prior
        # training). They are always returned in addition to learned
        # ones, never evicted, and never written to the persistence file.
        self._warmup: List[str] = list(warmup or [])
        self._load()

    def _load(self) -> None:
        if self._path.exists():
            try:
                data = json.loads(self._path.read_text(encoding="utf-8"))
                self._strategies = data.get("strategies", [])[-self._max:]
                log.info("StrategyMemory: loaded %d strategies from %s",
                         len(self._strategies), self._path)
            except Exception as exc:
                log.warning("StrategyMemory: failed to load %s: %s", self._path, exc)

    def _save(self) -> None:
        self._path.write_text(
            json.dumps({"strategies": self._strategies[-self._max:]}, indent=2),
            encoding="utf-8",
        )

    def insert(self, text: str, metadata: Optional[Dict[str, Any]] = None) -> None:
        """Buffer a step record into the current episode trajectory."""
        self._trajectory.append(text)

    def query(self, text: str, k: int = 5) -> List[str]:
        """Return warmup + learned strategies (warmup is always included)."""
        learned = list(self._strategies[-k:])
        return list(self._warmup) + learned

    def reset(self) -> None:
        """Clear trajectory buffer for new episode (strategies persist)."""
        self._trajectory = []

    def clear(self) -> None:
        """Wipe all strategies (for fresh experiments)."""
        self._strategies = []
        self._trajectory = []
        self._save()

    def reflect(self, outcome: str) -> Optional[str]:
        """Call LLM to reflect on the episode and extract principles.

        Should be called at episode end, AFTER all steps have been
        inserted. Returns the raw LLM response for logging.
        """
        if not self._llm_call or not self._trajectory:
            return None

        # Build a condensed trajectory (skip redundant info)
        traj_text = "\n".join(self._trajectory)

        prompt = REFLECTION_PROMPT.format(
            trajectory=traj_text,
            outcome=outcome,
        )

        try:
            raw = self._llm_call(prompt)
            principles = self._parse_principles(raw)
            if principles:
                for p in principles:
                    self._strategies.append(p)
                # Keep only the most recent max_strategies
                self._strategies = self._strategies[-self._max:]
                self._save()
                log.info("StrategyMemory: extracted %d principles, total=%d",
                         len(principles), len(self._strategies))
            return raw
        except Exception as exc:
            log.warning("StrategyMemory: reflection failed: %s", exc)
            return None

    def _parse_principles(self, raw: str) -> List[str]:
        """Parse JSON array from LLM response, tolerant of markdown/thinking."""
        import re
        text = raw.strip()
        # Strip <think>...</think> blocks
        text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL).strip()
        # Strip markdown code fences
        if "```" in text:
            lines = text.split("\n")
            lines = [l for l in lines if not l.strip().startswith("```")]
            text = "\n".join(lines).strip()
        # Find ALL JSON arrays and try each (last one is most likely the answer)
        candidates = list(re.finditer(r'\[(?:[^\[\]]*"[^"]*"[^\[\]]*)\]', text))
        for match in reversed(candidates):
            try:
                arr = json.loads(match.group())
                result = [str(s).strip() for s in arr if isinstance(s, str) and len(s.strip()) > 10]
                if result:
                    return result
            except json.JSONDecodeError:
                continue
        # Fallback: find last [...] bracket pair
        end = text.rfind("]")
        if end != -1:
            start = text.rfind("[", 0, end)
            if start != -1:
                try:
                    arr = json.loads(text[start:end + 1])
                    return [str(s).strip() for s in arr if isinstance(s, str) and len(s.strip()) > 10]
                except json.JSONDecodeError:
                    pass
        # Last resort: extract long quoted strings
        principles = re.findall(r'"([^"]{20,})"', text[-500:])
        if principles:
            log.info("StrategyMemory: fallback parsed %d principles", len(principles))
            return principles[:2]
        log.warning("StrategyMemory: no principles found in: %s", text[-200:])
        return []

    def get_system_prompt_section(self) -> str:
        """Format strategies as a system prompt section."""
        all_strategies = list(self._warmup) + list(self._strategies)
        if not all_strategies:
            return ""
        lines = "\n".join(f"  {i+1}. {s}" for i, s in enumerate(all_strategies))
        return (
            "\n\nNavigation Lessons (from previous episodes):\n"
            f"{lines}\n"
            "Apply these lessons to improve your navigation decisions."
        )
