"""Rule-based semantic reward interpreter.

Converts raw step data (action, bearing, distance, reward) into
structured Situation-Action-Outcome-Lesson (SAOL) text.  Zero LLM
calls — pure heuristics.

The interpreter does NOT replace the numeric reward; it produces a
human-readable annotation that is stored in L1 working memory so
upstream consumers (L2 event compressor, L3 skill distiller, and
the agent's own prompt) get causal explanations instead of bare
numbers.

Example output::

    [aligned, medium] MOVE_FORWARD → progress -295cm (good).
    Moving forward when aligned is efficient.

Usage::

    interp = SemanticRewardInterpreter()
    record = interp.interpret(step_data)
    print(record.semantic)   # SAOL text
    print(record.situation)  # structured situation dict
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Dict, Optional


# ── Bearing bins ──────────────────────────────────────────────────────
# Bearing is the angle from the agent's facing direction to the goal.
# Positive = goal is to the left, negative = goal is to the right.
# (Habitat convention: counter-clockwise positive.)

_BEARING_BINS = [
    (-180, -90, "behind_right"),
    (-90,  -45, "far_right"),
    (-45,  -15, "slight_right"),
    (-15,   15, "aligned"),
    (15,    45, "slight_left"),
    (45,    90, "far_left"),
    (90,   180, "behind_left"),
]

# ── Distance bins (cm) ───────────────────────────────────────────────

_DISTANCE_BINS = [
    (0,    200,  "very_close"),
    (200,  500,  "close"),
    (500,  1500, "medium"),
    (1500, 1e9,  "far"),
]


def classify_bearing(bearing_deg: float) -> str:
    """Map bearing in degrees to a named bin."""
    # Normalise to [-180, 180]
    b = ((bearing_deg + 180) % 360) - 180
    for lo, hi, name in _BEARING_BINS:
        if lo <= b < hi:
            return name
    return "aligned"


def classify_distance(distance_cm: float) -> str:
    """Map distance in cm to a named bin."""
    for lo, hi, name in _DISTANCE_BINS:
        if lo <= distance_cm < hi:
            return name
    return "far"


def classify_outcome(delta_cm: float) -> str:
    """Classify distance change as progress / regress / neutral."""
    if delta_cm > 30:
        return "progress"
    if delta_cm < -30:
        return "regress"
    return "neutral"


# ── Step record dataclass ────────────────────────────────────────────

@dataclass
class StepRecord:
    """One interpreted navigation step."""

    step: int
    action: str
    bearing_deg: float
    bearing_bin: str
    distance_cm: float
    distance_bin: str
    prev_distance_cm: float
    delta_cm: float           # positive = got closer
    outcome: str              # progress / regress / neutral
    reward: float
    yaw_deg: float
    semantic: str             # Full SAOL text
    situation_key: str        # Discrete key for L2 matching


# ── Lesson generation ────────────────────────────────────────────────

def _generate_lesson(action: str, bearing_bin: str, distance_bin: str,
                     outcome: str, delta_cm: float) -> str:
    """Produce a one-sentence transferable lesson from the step."""

    # STOP lessons
    if action == "STOP":
        if distance_bin == "very_close":
            return "Correct: STOP when very close to goal."
        return f"Premature STOP — still {distance_bin} from goal."

    # MOVE_FORWARD lessons
    if action == "MOVE_FORWARD":
        if outcome == "progress":
            if bearing_bin == "aligned":
                return "Moving forward when aligned is efficient."
            return f"Forward progress despite {bearing_bin} bearing — path may curve toward goal."
        if outcome == "regress":
            if bearing_bin in ("far_left", "far_right", "behind_left", "behind_right"):
                return f"Wrong: moving forward when bearing is {bearing_bin} increases distance. Turn first."
            return "Forward motion increased distance — possible obstacle or wrong heading."
        # neutral
        if bearing_bin in ("far_left", "far_right", "behind_left", "behind_right"):
            return f"Forward with {bearing_bin} bearing yielded no progress. Turn toward goal first."
        return "No significant progress — may need to adjust heading."

    # TURN lessons
    if action in ("TURN_LEFT", "TURN_RIGHT"):
        if outcome == "neutral":
            # Check if the turn was in the right direction
            turn_toward = (
                (action == "TURN_LEFT" and "left" in bearing_bin) or
                (action == "TURN_RIGHT" and "right" in bearing_bin)
            )
            if turn_toward:
                return f"Good: turning toward goal (bearing was {bearing_bin})."
            if bearing_bin == "aligned":
                return "Unnecessary turn — was already aligned. Move forward instead."
            return f"Turned away from goal (bearing was {bearing_bin}). Turn the other way."
        if outcome == "progress":
            return "Turn resulted in distance reduction — good realignment."
        # regress after turn is unusual but possible
        return "Turn increased distance — unusual, check heading."

    return ""


# ── Main interpreter ─────────────────────────────────────────────────

class SemanticRewardInterpreter:
    """Convert raw step data into a structured StepRecord with SAOL text.

    All logic is rule-based.  No LLM calls, no external dependencies.
    """

    def interpret(
        self,
        *,
        step: int,
        action: str,
        bearing_deg: float,
        distance_cm: float,
        prev_distance_cm: float,
        reward: float,
        yaw_deg: float = 0.0,
    ) -> StepRecord:
        """Interpret one step and return a StepRecord."""

        delta_cm = prev_distance_cm - distance_cm  # positive = closer
        bearing_bin = classify_bearing(bearing_deg)
        distance_bin = classify_distance(distance_cm)
        outcome = classify_outcome(delta_cm)

        situation_key = f"{bearing_bin}|{distance_bin}"
        lesson = _generate_lesson(action, bearing_bin, distance_bin, outcome, delta_cm)

        # Build SAOL one-liner
        outcome_desc = {
            "progress": f"progress {delta_cm:+.0f}cm (good)",
            "regress":  f"regress {delta_cm:+.0f}cm (bad)",
            "neutral":  "no change",
        }[outcome]

        semantic = (
            f"[{bearing_bin}, {distance_bin}] {action} → {outcome_desc}. "
            f"{lesson}"
        )

        return StepRecord(
            step=step,
            action=action,
            bearing_deg=bearing_deg,
            bearing_bin=bearing_bin,
            distance_cm=distance_cm,
            distance_bin=distance_bin,
            prev_distance_cm=prev_distance_cm,
            delta_cm=delta_cm,
            outcome=outcome,
            reward=reward,
            yaw_deg=yaw_deg,
            semantic=semantic,
            situation_key=situation_key,
        )
