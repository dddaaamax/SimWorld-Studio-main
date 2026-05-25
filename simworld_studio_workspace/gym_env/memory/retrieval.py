"""Structured situation-based memory retrieval.

Retrieval for L2 episodic memory is NOT embedding-based.  Instead we
discretise the current situation into a key (bearing_bin × distance_bin)
and do exact-match lookup, then rank by relevance score.

This is deliberate: navigation situations are low-dimensional and
discrete bins give deterministic, debuggable retrieval with zero
external dependencies (no vector DB, no embedder).

Usage::

    retriever = SituationRetriever()
    results = retriever.query(
        patterns=l2_patterns,
        bearing_deg=45.0,
        distance_cm=800.0,
        k=3,
    )
"""

from __future__ import annotations

import logging
from typing import Dict, List, Optional

from .semantic_reward import classify_bearing, classify_distance

log = logging.getLogger(__name__)


# ── L2 pattern data structure ────────────────────────────────────────

class SAOPattern:
    """A Situation-Action-Outcome pattern stored in L2 episodic memory.

    Attributes:
        situation_key: Discretised situation, e.g. "far_right|medium".
        action: The action taken (MOVE_FORWARD, TURN_LEFT, etc.).
        outcome: "progress" / "regress" / "neutral".
        count: How many times this pattern has been observed.
        total_delta_cm: Sum of delta_cm across observations.
        lesson: Best lesson text for this pattern.
    """

    __slots__ = ("situation_key", "action", "outcome", "count",
                 "total_delta_cm", "lesson")

    def __init__(
        self,
        situation_key: str,
        action: str,
        outcome: str,
        count: int = 1,
        total_delta_cm: float = 0.0,
        lesson: str = "",
    ) -> None:
        self.situation_key = situation_key
        self.action = action
        self.outcome = outcome
        self.count = count
        self.total_delta_cm = total_delta_cm
        self.lesson = lesson

    @property
    def avg_delta_cm(self) -> float:
        return self.total_delta_cm / max(self.count, 1)

    @property
    def pattern_key(self) -> str:
        """Unique key for dedup: situation + action + outcome."""
        return f"{self.situation_key}|{self.action}|{self.outcome}"

    def merge(self, other: "SAOPattern") -> None:
        """Merge another observation of the same pattern."""
        self.count += other.count
        self.total_delta_cm += other.total_delta_cm
        # Keep the lesson with more evidence
        if other.count > self.count // 2:
            self.lesson = other.lesson

    def to_text(self) -> str:
        """Render as a concise memory string for prompt injection."""
        bearing, distance = self.situation_key.split("|")
        avg = self.avg_delta_cm
        return (
            f"When {bearing} & {distance}: {self.action} → {self.outcome} "
            f"(avg {avg:+.0f}cm, seen {self.count}x). {self.lesson}"
        )

    def to_dict(self) -> dict:
        return {
            "situation_key": self.situation_key,
            "action": self.action,
            "outcome": self.outcome,
            "count": self.count,
            "total_delta_cm": self.total_delta_cm,
            "lesson": self.lesson,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "SAOPattern":
        return cls(**d)


# ── Retriever ────────────────────────────────────────────────────────

# Situation keys considered "adjacent" for fuzzy matching
_BEARING_ADJACENCY = {
    "aligned":       ["slight_left", "slight_right"],
    "slight_left":   ["aligned", "far_left"],
    "slight_right":  ["aligned", "far_right"],
    "far_left":      ["slight_left", "behind_left"],
    "far_right":     ["slight_right", "behind_right"],
    "behind_left":   ["far_left", "behind_right"],
    "behind_right":  ["far_right", "behind_left"],
}

_DISTANCE_ADJACENCY = {
    "very_close": ["close"],
    "close":      ["very_close", "medium"],
    "medium":     ["close", "far"],
    "far":        ["medium"],
}


class SituationRetriever:
    """Retrieve relevant L2 patterns by structured situation matching."""

    def query(
        self,
        patterns: Dict[str, SAOPattern],
        bearing_deg: float,
        distance_cm: float,
        k: int = 3,
    ) -> List[SAOPattern]:
        """Return the top-k most relevant patterns for the current situation.

        Matching strategy:
        1. Exact situation_key match → score 1.0
        2. Adjacent bearing OR distance → score 0.5
        3. Rank by score × count (frequent patterns are more reliable)
        """
        bearing_bin = classify_bearing(bearing_deg)
        distance_bin = classify_distance(distance_cm)
        exact_key = f"{bearing_bin}|{distance_bin}"

        # Build candidate set with relevance scores
        scored: List[tuple] = []  # (score, pattern)

        adj_bearings = set(_BEARING_ADJACENCY.get(bearing_bin, []))
        adj_distances = set(_DISTANCE_ADJACENCY.get(distance_bin, []))

        for pat in patterns.values():
            p_bearing, p_distance = pat.situation_key.split("|")

            if pat.situation_key == exact_key:
                score = 1.0
            elif p_bearing == bearing_bin and p_distance in adj_distances:
                score = 0.6
            elif p_distance == distance_bin and p_bearing in adj_bearings:
                score = 0.6
            elif p_bearing in adj_bearings and p_distance in adj_distances:
                score = 0.3
            else:
                continue

            # Weight by observation count (log scale to avoid domination)
            import math
            relevance = score * (1 + math.log1p(pat.count))
            scored.append((relevance, pat))

        scored.sort(key=lambda x: x[0], reverse=True)
        return [pat for _, pat in scored[:k]]
