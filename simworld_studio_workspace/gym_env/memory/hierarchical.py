"""Three-level hierarchical memory for navigation agents.

Architecture
------------
::

    ┌──────────────────────────────────────────────────────┐
    │  L3  Skill Memory  (always in system prompt)         │
    │  ≤10 transferable rules, distilled from L2           │
    ├──────────────────────────────────────────────────────┤
    │  L2  Episodic Memory  (retrieved by situation match)  │
    │  SAO patterns with observation counts, persisted     │
    ├──────────────────────────────────────────────────────┤
    │  L1  Working Memory  (current episode, sliding win)  │
    │  Semantic step records (SAOL format)                  │
    └──────────────────────────────────────────────────────┘

Data flow
---------
* **Each step**: raw data → SemanticRewardInterpreter → L1.append()
* **query()**: L3 (all) + L2 (top-k by situation) + L1 (recent N)
* **Episode end (compact)**: L1 → EventCompressor → SAO patterns → L2.merge()
* **Every N episodes (distill)**: L2 top patterns → L3 rule extraction

Prompt layout
-------------
::

    System Prompt:
      task description (fixed)
      action instructions (fixed)
      L3 skills (full, ≤10 rules)

    User Message:
      L2 episodic (top-3 by situation)
      L1 working (last 3-5 steps)
      current observation (bearing, distance, position)
      RGB image

The runner calls ``get_system_prompt_section()`` for L3 and
``query()`` for L2+L1.  If the runner doesn't call
``get_system_prompt_section()``, ``query()`` returns all three
levels combined.

Rethinking
----------
``check_rethink()`` detects online failure patterns (oscillation,
stuck, backtracking) from the last few L1 records.  When triggered,
it returns a rethink prompt that the runner should inject into the
conversation to override the agent's current plan.

LLM distillation
-----------------
Pass ``llm_call`` to the constructor to enable LLM-based L2→L3
distillation.  When set, ``_distill_skills()`` sends the top L2
patterns to the LLM for higher-quality skill extraction.  Falls
back to rule-based extraction when ``llm_call`` is None.

Usage::

    memory = HierarchicalMemory(persist_dir="./nav_memory")
    memory.reset()  # new episode

    # Each step:
    memory.insert(step_text, metadata={...})

    # Before LLM call — check rethink:
    rethink = memory.check_rethink()
    if rethink:
        # inject rethink.prompt into conversation
        pass

    # Before LLM call — retrieve:
    system_section = memory.get_system_prompt_section()  # L3
    recalled = memory.query(user_text, k=3)              # L2 + L1

    # Episode end:
    memory.end_episode(success=True, total_steps=20)
"""

from __future__ import annotations

import json
import logging
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from .semantic_reward import SemanticRewardInterpreter, StepRecord
from .event_compressor import EventCompressor, Event
from .retrieval import SAOPattern, SituationRetriever

log = logging.getLogger(__name__)

# Defaults
_L1_WINDOW = 5          # Recent steps kept in working memory
_L2_MAX_PATTERNS = 200  # Max L2 patterns before pruning
_L3_MAX_SKILLS = 10     # Max L3 rules
_DISTILL_EVERY_N = 2    # Distill L3 every N episodes
_RETHINK_WINDOW = 4     # Steps to look back for rethink detection


# ── Rethink signal ───────────────────────────────────────────────────

@dataclass
class RethinkSignal:
    """Returned by check_rethink() when a failure pattern is detected."""
    reason: str           # "oscillation" / "stuck" / "backtrack"
    prompt: str           # Text to inject into the LLM conversation
    steps_affected: int   # How many recent steps show the pattern


# ── L3 distillation prompt ───────────────────────────────────────────
#
# Goal: produce *meta-skills* that teach the agent HOW to observe and
# reason, not direct "when X do Y" answers.  Direct answers short-
# circuit learning and feel like cheating — the agent should still
# have to think, but with better pointers to what to attend to.

_DISTILL_PROMPT = """\
You are analyzing navigation experience data from an embodied agent \
operating in a 3D city scene.  The agent receives: a first-person RGB \
image, a distance to goal, and a bearing value whose sign convention \
is NOT pre-declared.  Its only actions are MOVE_FORWARD, TURN_LEFT, \
TURN_RIGHT, STOP.

Below are Situation-Action-Outcome patterns the agent has observed so \
far.  Each row shows: discretized situation (bearing bin × distance \
bin), action taken, outcome (progress / regress / neutral), average \
distance change in cm, and observation count.

{patterns_text}

Your task: write {max_skills} **meta-skills** that help the agent \
reason better in FUTURE episodes.  A meta-skill teaches the agent \
HOW to observe, compare, and reason — not the direct answer.

STRICT RULES (very important):

1. **DO NOT state the bearing sign convention directly.**
   Bad: "Positive bearing means right, negative means left."
   Good: "Check how TURN_RIGHT changes the bearing value in your \
   own observations — the direction it shifts tells you which sign \
   means 'right'."

2. **DO NOT give action-specific commands like 'if bearing>45 TURN_RIGHT'.**
   Bad: "When bearing > 45, TURN_RIGHT."
   Good: "If |bearing| is large, one TURN direction will decrease \
   |bearing| and the other will increase it — pick the one that \
   decreases it and stick with it."

3. **Teach detection of failure modes**, not preset recoveries.
   Good: "If your last 3 actions alternated between TURN_LEFT and \
   TURN_RIGHT without distance improving, you are oscillating — stop \
   and commit to whichever single direction made bearing decrease."

4. **Point at evidence in the observation**, not at fixed answers.
   Good: "Before deciding a turn direction, compare current bearing \
   to the previous step's bearing under the previous action. Use the \
   delta to infer which action moves bearing toward 0."

5. Each skill must be one or two sentences, imperative voice, \
   actionable.

6. Cover different meta-skills: direction inference, progress \
   verification, oscillation detection, approach/stop judgment, \
   memory usage.  Do not repeat the same advice.

Return ONLY a JSON array of strings:
["meta-skill 1", "meta-skill 2", ...]"""


class HierarchicalMemory:
    """Three-level hierarchical memory backend.

    Implements the AgentMemory protocol (insert / query / reset)
    so it can be used as a drop-in replacement in the runner.
    """

    name = "hierarchical"

    def __init__(
        self,
        persist_dir: str = "./nav_memory",
        l1_window: int = _L1_WINDOW,
        l2_max: int = _L2_MAX_PATTERNS,
        l3_max: int = _L3_MAX_SKILLS,
        distill_every: int = _DISTILL_EVERY_N,
        llm_call: Optional[Callable[[str], str]] = None,
    ) -> None:
        self._dir = Path(persist_dir)
        self._dir.mkdir(parents=True, exist_ok=True)

        self._l1_window = l1_window
        self._l2_max = l2_max
        self._l3_max = l3_max
        self._distill_every = distill_every
        self._llm_call = llm_call  # Optional: (prompt) -> response text

        # Components
        self._interpreter = SemanticRewardInterpreter()
        self._compressor = EventCompressor()
        self._retriever = SituationRetriever()

        # L1: working memory (episode-scoped)
        self._l1: List[StepRecord] = []
        # Episode-level lessons (end-of-episode summaries)
        self._episode_lessons: List[Dict[str, Any]] = []

        # L2: episodic memory (cross-episode, persisted)
        self._l2: Dict[str, SAOPattern] = {}
        self._load_l2()

        # L3: skill memory (distilled rules, persisted)
        self._l3: List[str] = []
        self._load_l3()

        # Episode counter for distillation scheduling
        self._episode_count = self._load_episode_count()

        # Track latest bearing/distance for retrieval context
        self._current_bearing: float = 0.0
        self._current_distance: float = 0.0

        # Guards L2 merge, L3 distill, episode counter, and JSON persistence
        # so concurrent ghost views can batch-update safely.
        self._write_lock = threading.Lock()

        log.info(
            "HierarchicalMemory: L2=%d patterns, L3=%d skills, "
            "episodes=%d, persist=%s",
            len(self._l2), len(self._l3),
            self._episode_count, self._dir,
        )

    # ══════════════════════════════════════════════════════════════════
    # AgentMemory protocol
    # ══════════════════════════════════════════════════════════════════

    def insert(self, text: str, metadata: Optional[Dict[str, Any]] = None) -> None:
        """Record one navigation step into L1 working memory.

        The ``metadata`` dict MUST contain the keys needed by the
        semantic interpreter: step, action, bearing_deg, distance_cm,
        prev_distance_cm, reward, yaw_deg.

        If metadata is missing or incomplete, falls back to storing
        the raw text (backward-compatible with existing runner).

        Records tagged with ``event_type == "episode_summary"`` are
        stored separately as lessons — they are NOT per-step records
        and should not pollute the SAO pattern extraction.
        """
        meta = metadata or {}

        # Episode-level summaries (success/fail lessons) — store as
        # a special lesson that bypasses step-level SAO extraction.
        if meta.get("event_type") == "episode_summary":
            log.debug("L1 insert (episode summary): %s", text[:80])
            self._episode_lessons.append({
                "text": text,
                "success": bool(meta.get("success", False)),
            })
            return

        # Try structured interpretation
        required = {"step", "action", "bearing_deg", "distance_cm",
                    "prev_distance_cm", "reward"}
        if required.issubset(meta.keys()):
            record = self._interpreter.interpret(
                step=int(meta["step"]),
                action=str(meta["action"]),
                bearing_deg=float(meta["bearing_deg"]),
                distance_cm=float(meta["distance_cm"]),
                prev_distance_cm=float(meta["prev_distance_cm"]),
                reward=float(meta["reward"]),
                yaw_deg=float(meta.get("yaw_deg", 0.0)),
            )
            self._l1.append(record)
            self._current_bearing = record.bearing_deg
            self._current_distance = record.distance_cm
            log.debug("L1 insert [step %d]: %s", record.step, record.semantic)
        else:
            # Fallback: create a minimal record from raw text
            record = StepRecord(
                step=int(meta.get("step", len(self._l1) + 1)),
                action=str(meta.get("action", "UNKNOWN")),
                bearing_deg=0.0,
                bearing_bin="aligned",
                distance_cm=float(meta.get("d_goal_cm", 0)),
                distance_bin="medium",
                prev_distance_cm=float(meta.get("d_goal_cm", 0)),
                delta_cm=float(meta.get("delta_cm", 0)),
                outcome="neutral",
                reward=float(meta.get("reward", 0)),
                yaw_deg=0.0,
                semantic=text,
                situation_key="aligned|medium",
            )
            self._l1.append(record)
            log.debug("L1 insert (fallback): %s", text[:80])

    def query(self, text: str, k: int = 5) -> List[str]:
        """Return relevant memories for the current situation.

        Returns a combined list: L2 episodic (by situation) + L1 recent.
        L3 skills are NOT included here — use get_system_prompt_section()
        to inject them into the system prompt.

        If the runner doesn't call get_system_prompt_section(), L3
        is prepended to the results as a fallback.
        """
        results: List[str] = []

        # L2: retrieve by current situation
        if self._l2:
            matched = self._retriever.query(
                self._l2,
                bearing_deg=self._current_bearing,
                distance_cm=self._current_distance,
                k=min(k, 3),
            )
            for pat in matched:
                results.append(f"[experience] {pat.to_text()}")

        # L1: recent steps (sliding window)
        recent = self._l1[-self._l1_window:]
        for rec in recent:
            results.append(f"[recent] {rec.semantic}")

        return results

    def reset(self) -> None:
        """Called at the start of each episode.

        Compacts L1 → L2 from the previous episode (if any),
        then clears L1 for the new episode.
        """
        if self._l1:
            self._compact_episode()
        self._l1 = []
        self._episode_lessons = []
        self._current_bearing = 0.0
        self._current_distance = 0.0

    # ══════════════════════════════════════════════════════════════════
    # Extended API (beyond AgentMemory protocol)
    # ══════════════════════════════════════════════════════════════════

    def get_system_prompt_section(self) -> str:
        """Return L3 skills formatted for system prompt injection.

        The runner should append this to the system prompt.  If
        there are no skills yet, returns empty string.
        """
        if not self._l3:
            return ""
        lines = "\n".join(f"  {i+1}. {s}" for i, s in enumerate(self._l3))
        return (
            "\n\nNavigation Skills (learned from past episodes):\n"
            f"{lines}\n"
            "Apply these skills to navigate more efficiently."
        )

    def end_episode(
        self,
        success: bool = False,
        total_steps: int = 0,
        final_distance_cm: float = 0.0,
    ) -> None:
        """Explicitly signal episode end.  Triggers L1→L2 compaction.

        Can be called by the runner after run_episode() for richer
        metadata.  If not called, reset() will do a basic compaction.
        """
        self._compact_episode(
            success=success,
            total_steps=total_steps,
            final_distance_cm=final_distance_cm,
        )
        self._l1 = []
        self._episode_lessons = []

    def get_stats(self) -> Dict[str, Any]:
        """Return memory statistics for logging."""
        return {
            "l1_size": len(self._l1),
            "l2_patterns": len(self._l2),
            "l3_skills": len(self._l3),
            "episodes": self._episode_count,
        }

    def check_rethink(self) -> Optional[RethinkSignal]:
        """Check recent L1 records for online failure patterns.

        Returns a RethinkSignal if the agent should reconsider its
        current strategy, or None if everything looks normal.

        The runner should call this before each LLM query and, if
        triggered, inject the prompt into the conversation as a
        system-level hint.

        Detects:
        - **oscillation**: alternating TURN_LEFT/TURN_RIGHT with no
          distance progress (agent is spinning in place).
        - **stuck**: consecutive MOVE_FORWARD with ≤30cm delta
          (agent is blocked by an obstacle).
        - **backtrack**: consecutive MOVE_FORWARD with increasing
          distance (agent is walking away from goal).
        """
        recent = self._l1[-_RETHINK_WINDOW:]
        if len(recent) < 3:
            return None

        actions = [r.action for r in recent]
        deltas = [r.delta_cm for r in recent]

        # ── Oscillation: alternating turns, no progress ──────────
        turns = [r for r in recent if r.action in ("TURN_LEFT", "TURN_RIGHT")]
        if len(turns) >= 3:
            alternations = sum(
                1 for j in range(len(turns) - 1)
                if turns[j].action != turns[j + 1].action
            )
            net_progress = sum(r.delta_cm for r in recent)
            if alternations >= 2 and abs(net_progress) < 60:
                bearing = recent[-1].bearing_deg
                distance = recent[-1].distance_cm
                action_seq = " → ".join(r.action for r in recent)
                return RethinkSignal(
                    reason="oscillation",
                    steps_affected=len(recent),
                    prompt=(
                        f"OBSERVATION: Your last {len(recent)} actions were: "
                        f"{action_seq}. Net distance change: {net_progress:+.0f}cm "
                        f"(no progress). Current bearing to goal: {bearing:+.0f}°, "
                        f"distance: {distance:.0f}cm. "
                        f"Your current approach is not working. "
                        f"Rethink your strategy before choosing the next action."
                    ),
                )

        # ── Stuck: forward but no distance change ────────────────
        forward_recent = [r for r in recent if r.action == "MOVE_FORWARD"]
        if len(forward_recent) >= 2:
            stuck_steps = [r for r in forward_recent if abs(r.delta_cm) < 30]
            if len(stuck_steps) >= 2:
                bearing = recent[-1].bearing_deg
                distance = recent[-1].distance_cm
                deltas = [f"{r.delta_cm:+.0f}" for r in stuck_steps]
                return RethinkSignal(
                    reason="stuck",
                    steps_affected=len(stuck_steps),
                    prompt=(
                        f"OBSERVATION: You moved forward {len(stuck_steps)} times "
                        f"but distance barely changed (deltas: {', '.join(deltas)}cm). "
                        f"Something is blocking your path. "
                        f"Current bearing: {bearing:+.0f}°, distance: {distance:.0f}cm. "
                        f"Rethink your strategy before choosing the next action."
                    ),
                )

        # ── Backtrack: forward but distance increasing ───────────
        if len(forward_recent) >= 2:
            regressing = [r for r in forward_recent if r.delta_cm < -30]
            if len(regressing) >= 2:
                bearing = recent[-1].bearing_deg
                distance = recent[-1].distance_cm
                total_regress = sum(r.delta_cm for r in regressing)
                return RethinkSignal(
                    reason="backtrack",
                    steps_affected=len(regressing),
                    prompt=(
                        f"OBSERVATION: You moved forward {len(regressing)} times "
                        f"but distance to goal INCREASED by {abs(total_regress):.0f}cm. "
                        f"You are moving away from the goal. "
                        f"Current bearing: {bearing:+.0f}°, distance: {distance:.0f}cm. "
                        f"Rethink your strategy before choosing the next action."
                    ),
                )

        return None

    # ══════════════════════════════════════════════════════════════════
    # Internal: L1 → L2 compaction
    # ══════════════════════════════════════════════════════════════════

    def _compact_episode(
        self,
        success: bool = False,
        total_steps: int = 0,
        final_distance_cm: float = 0.0,
    ) -> None:
        """Compress L1 step records into L2 SAO patterns (single-agent path)."""
        self._merge_l1_into_l2(
            self._l1,
            success=success,
            total_steps=total_steps,
            final_distance_cm=final_distance_cm,
        )

    def _merge_l1_into_l2(
        self,
        l1_records: List[StepRecord],
        *,
        success: bool = False,
        total_steps: int = 0,
        final_distance_cm: float = 0.0,
        distill: bool = True,
    ) -> None:
        """Thread-safe L1 → L2 merge used by both the single-agent path and
        per-ghost forks.  Holds the write lock across L2/L3 mutation + persist.
        """
        if not l1_records:
            return

        with self._write_lock:
            self._episode_count += 1
            log.info(
                "Compacting episode %d: %d steps → L2",
                self._episode_count, len(l1_records),
            )

            # Step 1: compress steps into events
            events = self._compressor.compress(l1_records)
            log.info("  Compressed into %d events", len(events))

            # Step 2: extract SAO patterns from individual steps
            for rec in l1_records:
                pat = SAOPattern(
                    situation_key=rec.situation_key,
                    action=rec.action,
                    outcome=rec.outcome,
                    count=1,
                    total_delta_cm=rec.delta_cm,
                    lesson=rec.semantic,
                )
                key = pat.pattern_key
                if key in self._l2:
                    self._l2[key].merge(pat)
                else:
                    self._l2[key] = pat

            # Step 3: also store event-level lessons
            for event in events:
                if event.event_type in ("oscillation", "stuck", "backtrack"):
                    bearing_bin = (
                        l1_records[0].bearing_bin if l1_records else "aligned"
                    )
                    from .semantic_reward import classify_distance
                    dist_bin = classify_distance(event.start_distance_cm)
                    sit_key = f"{bearing_bin}|{dist_bin}"
                    pat = SAOPattern(
                        situation_key=sit_key,
                        action=event.event_type,
                        outcome="failure_pattern",
                        count=1,
                        total_delta_cm=event.net_progress_cm,
                        lesson=event.lesson,
                    )
                    key = pat.pattern_key
                    if key in self._l2:
                        self._l2[key].merge(pat)
                    else:
                        self._l2[key] = pat

            # Step 4: prune L2 if too large (keep highest-count patterns)
            if len(self._l2) > self._l2_max:
                sorted_pats = sorted(
                    self._l2.items(),
                    key=lambda x: x[1].count,
                    reverse=True,
                )
                self._l2 = dict(sorted_pats[:self._l2_max])
                log.info("  L2 pruned to %d patterns", len(self._l2))

            self._save_l2()
            self._save_episode_count()
            should_distill = (
                distill
                and self._episode_count % self._distill_every == 0
            )

        # Distill outside the lock — LLM call can be slow; L3 has its own
        # save path.  If two forks race on distill they both produce valid
        # L3 state, the second wins.
        if should_distill:
            self._distill_skills()

    def distill_if_due(self) -> None:
        """Trigger L3 distillation if the shared episode counter is due.

        Useful for batch-mode callers that want to force a distill after
        all forks have completed their episodes.
        """
        with self._write_lock:
            due = (
                self._l2
                and self._episode_count % self._distill_every == 0
            )
        if due:
            self._distill_skills()

    # ══════════════════════════════════════════════════════════════════
    # Ghost-mode fork: per-agent L1, shared L2/L3
    # ══════════════════════════════════════════════════════════════════

    def fork(self, agent_id: str) -> "_GhostView":
        """Return a lightweight per-agent view that shares L2/L3 but has a
        private L1 working memory.

        All ghosts in a wave should use their own fork; end_episode() on
        each view folds that ghost's L1 into the shared L2 under a lock,
        so updates from parallel ghosts are safely batched.
        """
        return _GhostView(self, agent_id)

    # ══════════════════════════════════════════════════════════════════
    # Internal: L2 → L3 distillation
    # ══════════════════════════════════════════════════════════════════

    def _distill_skills(self) -> None:
        """Extract L3 skills from L2 patterns.

        Uses LLM distillation if ``llm_call`` is set, otherwise
        falls back to rule-based extraction.
        """
        if not self._l2:
            return

        log.info("Distilling L3 skills from %d L2 patterns...", len(self._l2))

        if self._llm_call is not None:
            skills = self._distill_skills_llm()
            if skills:
                self._l3 = skills[:self._l3_max]
                self._save_l3()
                log.info("  LLM distilled %d L3 skills", len(self._l3))
                return
            log.warning("  LLM distillation failed, falling back to rules")

        self._distill_skills_rules()

    def _distill_skills_llm(self) -> Optional[List[str]]:
        """LLM-based L2→L3 distillation."""
        # Select top patterns by count for the prompt
        sorted_pats = sorted(
            self._l2.values(),
            key=lambda p: p.count,
            reverse=True,
        )[:30]  # Cap input to avoid prompt bloat

        patterns_text = "\n".join(
            f"- {p.to_text()}" for p in sorted_pats
        )

        prompt = _DISTILL_PROMPT.format(
            patterns_text=patterns_text,
            max_skills=self._l3_max,
        )

        try:
            raw = self._llm_call(prompt)
            skills = self._parse_json_array(raw)
            if skills:
                return [s.strip() for s in skills if s.strip()]
        except Exception as exc:
            log.warning("LLM distill failed: %s", exc)

        return None

    @staticmethod
    def _parse_json_array(raw: str) -> Optional[List[str]]:
        """Parse a JSON array from LLM response, tolerant of markdown."""
        import json as _json
        text = raw.strip()
        # Strip markdown fences
        if "```" in text:
            lines = text.split("\n")
            lines = [l for l in lines if not l.strip().startswith("```")]
            text = "\n".join(lines).strip()
        start = text.find("[")
        end = text.rfind("]")
        if start == -1 or end == -1:
            return None
        try:
            arr = _json.loads(text[start:end + 1])
            return [str(s) for s in arr if isinstance(s, str)]
        except _json.JSONDecodeError:
            return None

    def _distill_skills_rules(self) -> None:
        """Rule-based L2→L3 distillation (fallback, no LLM)."""
        skills: List[str] = []

        # Group patterns by situation
        situation_stats: Dict[str, Dict[str, list]] = {}
        for pat in self._l2.values():
            sit = pat.situation_key
            if sit not in situation_stats:
                situation_stats[sit] = {}
            action = pat.action
            if action not in situation_stats[sit]:
                situation_stats[sit][action] = []
            situation_stats[sit][action].append(pat)

        for sit, actions in situation_stats.items():
            bearing, distance = sit.split("|")

            best_action = None
            best_avg = -1e9
            worst_action = None
            worst_avg = 1e9

            for action, pats in actions.items():
                total_count = sum(p.count for p in pats)
                if total_count < 3:
                    continue
                total_delta = sum(p.total_delta_cm for p in pats)
                avg = total_delta / total_count
                if avg > best_avg:
                    best_avg = avg
                    best_action = action
                if avg < worst_avg:
                    worst_avg = avg
                    worst_action = action

            if best_action and best_avg > 30:
                skills.append(
                    f"When {bearing} & {distance}: prefer {best_action} "
                    f"(avg {best_avg:+.0f}cm progress, well-evidenced)."
                )
            if worst_action and worst_avg < -30 and worst_action != best_action:
                skills.append(
                    f"When {bearing} & {distance}: avoid {worst_action} "
                    f"(avg {worst_avg:+.0f}cm regression)."
                )

        # Failure patterns
        failure_pats = [
            p for p in self._l2.values()
            if p.outcome == "failure_pattern" and p.count >= 2
        ]
        for fp in sorted(failure_pats, key=lambda p: p.count, reverse=True)[:3]:
            skills.append(fp.lesson)

        # Deduplicate and cap
        seen = set()
        unique: List[str] = []
        for s in skills:
            key = s[:50]
            if key not in seen:
                seen.add(key)
                unique.append(s)

        self._l3 = unique[:self._l3_max]
        self._save_l3()
        log.info("  Rule-based distilled %d L3 skills", len(self._l3))

    # ══════════════════════════════════════════════════════════════════
    # Persistence
    # ══════════════════════════════════════════════════════════════════

    def _l2_path(self) -> Path:
        return self._dir / "l2_episodic.json"

    def _l3_path(self) -> Path:
        return self._dir / "l3_skills.json"

    def _counter_path(self) -> Path:
        return self._dir / "episode_count.txt"

    def _save_l2(self) -> None:
        data = {k: v.to_dict() for k, v in self._l2.items()}
        self._l2_path().write_text(
            json.dumps(data, indent=2), encoding="utf-8"
        )

    def _load_l2(self) -> None:
        path = self._l2_path()
        if path.exists():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                self._l2 = {
                    k: SAOPattern.from_dict(v) for k, v in data.items()
                }
            except Exception as exc:
                log.warning("Failed to load L2: %s", exc)
                self._l2 = {}

    def _save_l3(self) -> None:
        self._l3_path().write_text(
            json.dumps({"skills": self._l3}, indent=2), encoding="utf-8"
        )

    def _load_l3(self) -> None:
        path = self._l3_path()
        if path.exists():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                self._l3 = data.get("skills", [])
            except Exception as exc:
                log.warning("Failed to load L3: %s", exc)
                self._l3 = []

    def _save_episode_count(self) -> None:
        self._counter_path().write_text(str(self._episode_count))

    def _load_episode_count(self) -> int:
        path = self._counter_path()
        if path.exists():
            try:
                return int(path.read_text().strip())
            except Exception:
                pass
        return 0

    def clear(self) -> None:
        """Wipe all memory (for fresh experiments)."""
        self._l1 = []
        self._l2 = {}
        self._l3 = []
        self._episode_count = 0
        self._save_l2()
        self._save_l3()
        self._save_episode_count()
        log.info("HierarchicalMemory cleared")


# ══════════════════════════════════════════════════════════════════════
# Ghost-mode per-agent view: private L1, shared L2/L3
# ══════════════════════════════════════════════════════════════════════


class _GhostView:
    """Per-agent facade over a shared :class:`HierarchicalMemory`.

    Each ghost in a wave forks its own view so their working memories
    (L1) stay isolated — one ghost's recent steps do not leak into
    another's retrieval context.  L2 (episodic patterns) and L3
    (distilled skills) remain shared and are updated under a lock
    when each ghost's episode ends, so all ghosts contribute to the
    same persistent knowledge store.

    Implements the same `AgentMemory` protocol as the parent
    (insert / query / reset) plus the hierarchical extras used by
    the runner (get_system_prompt_section / end_episode /
    check_rethink).
    """

    name = "hierarchical_ghost"

    def __init__(self, parent: "HierarchicalMemory", agent_id: str) -> None:
        self._parent = parent
        self._agent_id = agent_id
        self._l1: List[StepRecord] = []
        self._episode_lessons: List[Dict[str, Any]] = []
        self._current_bearing: float = 0.0
        self._current_distance: float = 0.0

    # ── AgentMemory protocol ──────────────────────────────────────────

    def insert(self, text: str, metadata: Optional[Dict[str, Any]] = None) -> None:
        meta = metadata or {}

        if meta.get("event_type") == "episode_summary":
            self._episode_lessons.append({
                "text": text,
                "success": bool(meta.get("success", False)),
            })
            return

        required = {"step", "action", "bearing_deg", "distance_cm",
                    "prev_distance_cm", "reward"}
        if required.issubset(meta.keys()):
            record = self._parent._interpreter.interpret(
                step=int(meta["step"]),
                action=str(meta["action"]),
                bearing_deg=float(meta["bearing_deg"]),
                distance_cm=float(meta["distance_cm"]),
                prev_distance_cm=float(meta["prev_distance_cm"]),
                reward=float(meta["reward"]),
                yaw_deg=float(meta.get("yaw_deg", 0.0)),
            )
            self._l1.append(record)
            self._current_bearing = record.bearing_deg
            self._current_distance = record.distance_cm
        else:
            record = StepRecord(
                step=int(meta.get("step", len(self._l1) + 1)),
                action=str(meta.get("action", "UNKNOWN")),
                bearing_deg=0.0,
                bearing_bin="aligned",
                distance_cm=float(meta.get("d_goal_cm", 0)),
                distance_bin="medium",
                prev_distance_cm=float(meta.get("d_goal_cm", 0)),
                delta_cm=float(meta.get("delta_cm", 0)),
                outcome="neutral",
                reward=float(meta.get("reward", 0)),
                yaw_deg=0.0,
                semantic=text,
                situation_key="aligned|medium",
            )
            self._l1.append(record)

    def query(self, text: str, k: int = 5) -> List[str]:
        results: List[str] = []

        # L2 (shared) — snapshot under lock to avoid concurrent mutation
        with self._parent._write_lock:
            l2_snapshot = dict(self._parent._l2)
        if l2_snapshot:
            matched = self._parent._retriever.query(
                l2_snapshot,
                bearing_deg=self._current_bearing,
                distance_cm=self._current_distance,
                k=min(k, 3),
            )
            for pat in matched:
                results.append(f"[experience] {pat.to_text()}")

        # L1 (private): own recent steps only
        recent = self._l1[-self._parent._l1_window:]
        for rec in recent:
            results.append(f"[recent] {rec.semantic}")

        return results

    def reset(self) -> None:
        """Compact own L1 into shared L2, then clear for next episode."""
        if self._l1:
            self._parent._merge_l1_into_l2(self._l1)
        self._l1 = []
        self._episode_lessons = []
        self._current_bearing = 0.0
        self._current_distance = 0.0

    # ── Hierarchical extras ───────────────────────────────────────────

    def get_system_prompt_section(self) -> str:
        return self._parent.get_system_prompt_section()

    def end_episode(
        self,
        success: bool = False,
        total_steps: int = 0,
        final_distance_cm: float = 0.0,
        distill: bool = False,
    ) -> None:
        """Fold this ghost's L1 into shared L2.

        ``distill`` is OFF by default for ghost views — with N ghosts
        running concurrently we want to distill once after the wave
        rather than N times during it.  The wave runner should call
        :meth:`HierarchicalMemory.distill_if_due` after all ghosts have
        ended their episodes.
        """
        self._parent._merge_l1_into_l2(
            self._l1,
            success=success,
            total_steps=total_steps,
            final_distance_cm=final_distance_cm,
            distill=distill,
        )
        self._l1 = []
        self._episode_lessons = []

    def check_rethink(self) -> Optional[RethinkSignal]:
        """Online failure-pattern detection on this ghost's own L1."""
        recent = self._l1[-_RETHINK_WINDOW:]
        if len(recent) < 3:
            return None

        # Delegate to parent's rethink logic by temporarily pointing it at
        # our L1.  Safe because parent's check_rethink is pure wrt _l1 and
        # this view is the only caller holding a reference to our list.
        # We keep this inline rather than refactoring to avoid churning
        # the single-agent code path.
        turns = [r for r in recent if r.action in ("TURN_LEFT", "TURN_RIGHT")]
        if len(turns) >= 3:
            alternations = sum(
                1 for j in range(len(turns) - 1)
                if turns[j].action != turns[j + 1].action
            )
            net_progress = sum(r.delta_cm for r in recent)
            if alternations >= 2 and abs(net_progress) < 60:
                bearing = recent[-1].bearing_deg
                distance = recent[-1].distance_cm
                action_seq = " → ".join(r.action for r in recent)
                return RethinkSignal(
                    reason="oscillation",
                    steps_affected=len(recent),
                    prompt=(
                        f"OBSERVATION: Your last {len(recent)} actions were: "
                        f"{action_seq}. Net distance change: {net_progress:+.0f}cm "
                        f"(no progress). Current bearing to goal: {bearing:+.0f}°, "
                        f"distance: {distance:.0f}cm. "
                        f"Your current approach is not working. "
                        f"Rethink your strategy before choosing the next action."
                    ),
                )

        forward_recent = [r for r in recent if r.action == "MOVE_FORWARD"]
        if len(forward_recent) >= 2:
            stuck_steps = [r for r in forward_recent if abs(r.delta_cm) < 30]
            if len(stuck_steps) >= 2:
                bearing = recent[-1].bearing_deg
                distance = recent[-1].distance_cm
                deltas = [f"{r.delta_cm:+.0f}" for r in stuck_steps]
                return RethinkSignal(
                    reason="stuck",
                    steps_affected=len(stuck_steps),
                    prompt=(
                        f"OBSERVATION: You moved forward {len(stuck_steps)} times "
                        f"but distance barely changed (deltas: {', '.join(deltas)}cm). "
                        f"Something is blocking your path. "
                        f"Current bearing: {bearing:+.0f}°, distance: {distance:.0f}cm. "
                        f"Rethink your strategy before choosing the next action."
                    ),
                )

            regressing = [r for r in forward_recent if r.delta_cm < -30]
            if len(regressing) >= 2:
                bearing = recent[-1].bearing_deg
                distance = recent[-1].distance_cm
                total_regress = sum(r.delta_cm for r in regressing)
                return RethinkSignal(
                    reason="backtrack",
                    steps_affected=len(regressing),
                    prompt=(
                        f"OBSERVATION: You moved forward {len(regressing)} times "
                        f"but distance to goal INCREASED by {abs(total_regress):.0f}cm. "
                        f"You are moving away from the goal. "
                        f"Current bearing: {bearing:+.0f}°, distance: {distance:.0f}cm. "
                        f"Rethink your strategy before choosing the next action."
                    ),
                )

        return None
