"""Compress consecutive step records into meaningful navigation events.

The compressor detects patterns in L1 step sequences and produces
structured Event objects for L2 episodic memory.  Each event captures
a coherent behaviour segment — not a single step.

Supported event types
---------------------
* **alignment**   — Agent turns to face the goal (bearing converges).
* **approach**    — Agent moves forward with sustained distance reduction.
* **oscillation** — Agent alternates turns without progress (stuck spinning).
* **stuck**       — Agent moves forward but distance doesn't change (obstacle).
* **backtrack**   — Agent moves forward but distance increases (wrong direction).
* **final_approach** — Last segment where agent gets within success distance.

Usage::

    from .semantic_reward import StepRecord
    compressor = EventCompressor()
    events = compressor.compress(step_records)
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import List, Optional

from .semantic_reward import StepRecord

log = logging.getLogger(__name__)


@dataclass
class Event:
    """A compressed navigation event spanning one or more steps."""

    event_type: str
    steps: List[int]           # step indices covered
    actions: List[str]         # action sequence
    start_distance_cm: float
    end_distance_cm: float
    net_progress_cm: float     # positive = got closer
    bearing_start_deg: float
    bearing_end_deg: float
    lesson: str

    def to_text(self) -> str:
        """One-line summary for L2 storage."""
        direction = "closer" if self.net_progress_cm > 0 else "farther"
        return (
            f"[{self.event_type}] steps {self.steps[0]}-{self.steps[-1]}: "
            f"{' → '.join(self.actions)} | "
            f"d_goal {self.start_distance_cm:.0f}→{self.end_distance_cm:.0f}cm "
            f"({abs(self.net_progress_cm):.0f}cm {direction}). "
            f"{self.lesson}"
        )

    def to_dict(self) -> dict:
        return {
            "event_type": self.event_type,
            "steps": self.steps,
            "actions": self.actions,
            "start_distance_cm": self.start_distance_cm,
            "end_distance_cm": self.end_distance_cm,
            "net_progress_cm": self.net_progress_cm,
            "bearing_start_deg": self.bearing_start_deg,
            "bearing_end_deg": self.bearing_end_deg,
            "lesson": self.lesson,
        }


class EventCompressor:
    """Segment a list of StepRecords into higher-level Events."""

    def __init__(
        self,
        oscillation_window: int = 4,
        stuck_threshold_cm: float = 30.0,
        approach_min_steps: int = 2,
    ) -> None:
        self.oscillation_window = oscillation_window
        self.stuck_threshold_cm = stuck_threshold_cm
        self.approach_min_steps = approach_min_steps

    def compress(self, records: List[StepRecord]) -> List[Event]:
        """Compress a full episode of step records into events."""
        if not records:
            return []

        events: List[Event] = []
        i = 0
        n = len(records)

        while i < n:
            # Try each detector in priority order
            event, consumed = self._try_oscillation(records, i)
            if event is None:
                event, consumed = self._try_stuck(records, i)
            if event is None:
                event, consumed = self._try_alignment(records, i)
            if event is None:
                event, consumed = self._try_approach(records, i)
            if event is None:
                event, consumed = self._try_backtrack(records, i)
            if event is None:
                # Fallback: single-step event
                r = records[i]
                event = Event(
                    event_type="single",
                    steps=[r.step],
                    actions=[r.action],
                    start_distance_cm=r.prev_distance_cm,
                    end_distance_cm=r.distance_cm,
                    net_progress_cm=r.delta_cm,
                    bearing_start_deg=r.bearing_deg,
                    bearing_end_deg=r.bearing_deg,
                    lesson=r.semantic,
                )
                consumed = 1

            events.append(event)
            i += consumed

        # Tag final approach
        if events and records[-1].distance_cm < 300:
            events[-1].event_type = "final_approach"

        return events

    # ── Detectors ────────────────────────────────────────────────────

    def _try_oscillation(
        self, records: List[StepRecord], start: int
    ) -> tuple:
        """Detect alternating turns with no net distance change."""
        window = records[start:start + self.oscillation_window]
        if len(window) < 3:
            return None, 0

        turns = [r for r in window if r.action in ("TURN_LEFT", "TURN_RIGHT")]
        if len(turns) < 3:
            return None, 0

        # Check for alternation: L-R-L or R-L-R
        alternations = sum(
            1 for j in range(len(turns) - 1)
            if turns[j].action != turns[j + 1].action
        )
        if alternations < 2:
            return None, 0

        # Check no net progress
        net = window[0].prev_distance_cm - window[-1].distance_cm
        if abs(net) > self.stuck_threshold_cm * 2:
            return None, 0

        n = len(window)
        return Event(
            event_type="oscillation",
            steps=[r.step for r in window],
            actions=[r.action for r in window],
            start_distance_cm=window[0].prev_distance_cm,
            end_distance_cm=window[-1].distance_cm,
            net_progress_cm=net,
            bearing_start_deg=window[0].bearing_deg,
            bearing_end_deg=window[-1].bearing_deg,
            lesson=(
                "Oscillation detected: alternating turns without progress. "
                "Commit to one turn direction until bearing is within ±30°, "
                "then move forward."
            ),
        ), n

    def _try_stuck(
        self, records: List[StepRecord], start: int
    ) -> tuple:
        """Detect consecutive MOVE_FORWARD with no distance change."""
        window: List[StepRecord] = []
        for r in records[start:start + 5]:
            if r.action == "MOVE_FORWARD" and abs(r.delta_cm) < self.stuck_threshold_cm:
                window.append(r)
            else:
                break

        if len(window) < 2:
            return None, 0

        net = window[0].prev_distance_cm - window[-1].distance_cm
        return Event(
            event_type="stuck",
            steps=[r.step for r in window],
            actions=[r.action for r in window],
            start_distance_cm=window[0].prev_distance_cm,
            end_distance_cm=window[-1].distance_cm,
            net_progress_cm=net,
            bearing_start_deg=window[0].bearing_deg,
            bearing_end_deg=window[-1].bearing_deg,
            lesson=(
                "Stuck: forward motion blocked (obstacle or wall). "
                "Turn to find a clear path before continuing forward."
            ),
        ), len(window)

    def _try_alignment(
        self, records: List[StepRecord], start: int
    ) -> tuple:
        """Detect a sequence of turns that brings bearing closer to 0."""
        window: List[StepRecord] = []
        for r in records[start:start + 6]:
            if r.action in ("TURN_LEFT", "TURN_RIGHT"):
                window.append(r)
            else:
                break

        if len(window) < 2:
            return None, 0

        # Check that bearing magnitude is decreasing (converging to aligned)
        start_abs = abs(window[0].bearing_deg)
        end_abs = abs(window[-1].bearing_deg)
        if end_abs >= start_abs:
            return None, 0

        net = window[0].prev_distance_cm - window[-1].distance_cm
        return Event(
            event_type="alignment",
            steps=[r.step for r in window],
            actions=[r.action for r in window],
            start_distance_cm=window[0].prev_distance_cm,
            end_distance_cm=window[-1].distance_cm,
            net_progress_cm=net,
            bearing_start_deg=window[0].bearing_deg,
            bearing_end_deg=window[-1].bearing_deg,
            lesson=(
                f"Alignment: turned from bearing {window[0].bearing_deg:+.0f}° "
                f"to {window[-1].bearing_deg:+.0f}°. "
                "Good: align before moving forward."
            ),
        ), len(window)

    def _try_approach(
        self, records: List[StepRecord], start: int
    ) -> tuple:
        """Detect consecutive MOVE_FORWARD with sustained progress."""
        window: List[StepRecord] = []
        for r in records[start:start + 10]:
            if r.action == "MOVE_FORWARD" and r.delta_cm > 0:
                window.append(r)
            else:
                break

        if len(window) < self.approach_min_steps:
            return None, 0

        net = window[0].prev_distance_cm - window[-1].distance_cm
        return Event(
            event_type="approach",
            steps=[r.step for r in window],
            actions=[r.action for r in window],
            start_distance_cm=window[0].prev_distance_cm,
            end_distance_cm=window[-1].distance_cm,
            net_progress_cm=net,
            bearing_start_deg=window[0].bearing_deg,
            bearing_end_deg=window[-1].bearing_deg,
            lesson=(
                f"Good approach: {len(window)} forward steps, "
                f"covered {net:.0f}cm. "
                "Keep moving forward when bearing is small and distance decreasing."
            ),
        ), len(window)

    def _try_backtrack(
        self, records: List[StepRecord], start: int
    ) -> tuple:
        """Detect MOVE_FORWARD that increases distance (wrong direction)."""
        window: List[StepRecord] = []
        for r in records[start:start + 5]:
            if r.action == "MOVE_FORWARD" and r.delta_cm < -self.stuck_threshold_cm:
                window.append(r)
            else:
                break

        if len(window) < 1:
            return None, 0

        net = window[0].prev_distance_cm - window[-1].distance_cm
        return Event(
            event_type="backtrack",
            steps=[r.step for r in window],
            actions=[r.action for r in window],
            start_distance_cm=window[0].prev_distance_cm,
            end_distance_cm=window[-1].distance_cm,
            net_progress_cm=net,
            bearing_start_deg=window[0].bearing_deg,
            bearing_end_deg=window[-1].bearing_deg,
            lesson=(
                f"Wrong direction: moved forward but distance increased by "
                f"{abs(net):.0f}cm. Bearing was {window[0].bearing_bin}. "
                "Turn toward goal before moving forward."
            ),
        ), len(window)
