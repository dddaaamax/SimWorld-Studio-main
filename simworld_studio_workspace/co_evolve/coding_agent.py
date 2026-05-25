"""Coding agent: LLM-driven scene builder + task designer. NO FALLBACK.

If JSON parse fails, retries the LLM call. If still fails, uses the
LAST successful design (never a hardcoded default).

Adversarial reward: coding_reward = 1 - nav_sr.
The coding agent is incentivized to push difficulty up while keeping
the nav agent in the learning zone.
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from .scene_manager import ASSET_CATALOG, BuildReport, SceneManager, SceneSpec, SpawnedObject
from .teacher import predict_spec_difficulty

log = logging.getLogger(__name__)


CODING_AGENT_PROMPT = """\
You are a **training environment designer** for a navigation agent.

## DIFFICULTY SUGGESTION (from the curriculum teacher — advisory)
{difficulty_directive}

## REWARD CONTEXT (informational)
Reward = Gaussian(agent_SR; peak=0.60, sigma=0.20) × (1 + 0.25·progress_bonus).
The teacher's suggested difficulty band is a HINT for where learning
progress is currently highest. Try to land near it, but use your own
judgement based on the ROLLING SR + failure patterns below if you have a
better idea.

## GUIDELINES
1. Treat the difficulty band as a soft target. Off-band designs are
   accepted but logged.
2. Base scene-content decisions (which objects to add/remove) on the
   ROLLING SR + failure patterns shown below.
{retry_feedback}

## EMBODIED AGENT STATUS
{rolling_summary}

## SCENE EDITING PHASE
{edit_phase_directive}

<<<<<<< Updated upstream
## ENVIRONMENT FEEDBACK (last build only — no historical residue)
{build_feedback}

Last epoch only (do NOT consult older history; rely on rolling stats):
=======
## ENVIRONMENT FEEDBACK (last build)
{build_feedback}

Per-epoch history (for trend only; act on the rolling stats above):
>>>>>>> Stashed changes
{performance_history}

Learned strategies:
{strategies}

Failure patterns (last 12 episodes only):
{failure_patterns}

## CURRENT SCENE (difficulty: {current_difficulty}/10)
{current_scene}

## YOUR DESIGN MEMORY
{coding_memory}

## AVAILABLE ASSETS
{asset_catalog}

## COORDINATE CONSTRAINTS (CRITICAL)
The navigation area is a square CENTERED AT (X=7661, Y=10970, Z=100)
on the HwaseongHaenggung map (NOT at the world origin).
ALL object coordinates MUST be within:
  x: [-2339, 17661]   (i.e. 7661 ± 10000)
  y: [970, 20970]     (i.e. 10970 ± 10000)
  z: 100              (ground level on this map)
Objects outside this range will NOT block the agent's path because the
agent's start and goal positions are sampled within this area.

To create effective obstacles that FORCE detours:
- Place buildings/objects BETWEEN likely start and goal positions
- Cluster objects to form walls, corridors, or chokepoints
- A single isolated object is easy to walk around — use groups of 2-3
  objects placed close together (300-800 units apart) to form barriers

## DIFFICULTY SCALE (0-10, monotone target)
1 = empty field, 1000cm path → agent ~80% SR
2 = empty field, 2000cm path → agent ~65% SR
3 = 1-2 objects, 1500cm path → agent ~55% SR
4 = 3-4 objects forming a partial wall, 2000cm path → agent ~45% SR
5 = 5+ objects forming corridors, 2500cm path → agent ~35% SR
6 = dense layout with walls, 3000cm path, forced detours → agent ~25% SR
8 = complex town layout with multiple walls → agent ~15% SR

## POLICY
Target rolling_SR in [0.45, 0.75] (ZPD band).
- If rolling_SR > 0.75 AND stable (streak ≥ 2): increase difficulty by +0.3 to +0.6
  (add 1 object OR +300cm path length, not both).
- If rolling_SR in [0.45, 0.75]: KEEP difficulty roughly flat (±0.2), let the agent
  master this level. Small scene tweaks are OK.
- If rolling_SR in [0.20, 0.45]: hold difficulty flat; do NOT add hardness, let
  the agent catch up (new strategies accumulate every epoch).
- If rolling_SR < 0.20: decrease difficulty by -0.3 to -0.5 (remove 1 object OR
  -300cm path), NOT more.
- Change ONE variable at a time (path OR objects OR heading), never multiple.

## DIFFICULTY CONTROL (deterministic — no random sampling noise)
You choose a SINGLE `path_cm` (the exact geodesic path length every episode
will target). Difficulty is then deterministic from your spec:
  difficulty ≈ min(2.5, path_cm/1000) + 2.5·blocked_ratio + 0.5 + (1.5 if objectnav else 0)
If you want SR to change, change `path_cm` and/or n_objects. The runner will
clamp `path_cm` to a per-epoch floor/ceiling around the previous epoch's
value (max ±800cm step) so you cannot regress more than one notch at a time.

Output JSON:
```json
{{
  "action": "keep_scene" or "modify_scene" or "new_scene",
  "add_objects": [{{"name": "Building_1", "asset": "hwaseong_bijangcheong", "x": 8500, "y": 11500, "z": 100}}],
  "remove_objects": ["Building_old_1", "Building_old_2"],
  "task_type": "pointnav",
  "path_cm": 1500,
  "max_steps": 40,
  "n_episodes": 10,
  "target_difficulty": 2,
  "reasoning": "why this design"
}}
```

Valid assets: {asset_keys}
Actions:
- keep_scene: no changes to objects, only adjust path/steps
- modify_scene: add AND/OR remove specific objects (incremental update)
- new_scene: CLEAR ALL existing objects, then add the new objects listed
For modify_scene: use add_objects to add, remove_objects to delete by name.
If SR is too low, REMOVE objects to reduce clutter. If SR is too high, ADD objects.
Coordinates: x in [-2339, 17661], y in [970, 20970], z=100 always
(scene is centered at world (7661, 10970, 100) on HwaseongHaenggung).
ASSET DIVERSITY: when adding multiple objects in one round, do NOT use
the same asset_key for all of them — pick at least 3 different keys
from the catalog so the scene looks varied (the catalog has 28 distinct
palace meshes, use them).
Output ONLY the JSON."""


class CodingAgent:
    """LLM-driven scene + task designer. Never falls back to hardcoded defaults."""

    def __init__(self, llm_call, coding_memory=None):
        self._llm_call = llm_call
        self.memory = coding_memory
        self._current_scene_desc = "(empty field — no objects)"
        self._current_scene_id = "scene_000"
        self._scene_counter = 0
        self._current_difficulty = 0.0
        self._best_difficulty = 0.0
        self._last_scene_streak = 0
        self._last_successful_spec: Optional[SceneSpec] = None

    def design(
        self,
        performance_history: str,
        strategies: str,
        failure_patterns: str,
        current_scene_objects: List[str],
        rolling_summary: str = "(no data yet)",
        current_scene_streak: int = 0,
        target_difficulty: Optional[float] = None,
        difficulty_band: Optional[Tuple[float, float]] = None,
        prev_blocked_ratio: float = 0.0,
        prev_build_report: Optional[BuildReport] = None,
        max_band_retries: int = 0,  # kept for backward-compat; ignored
        edit_round: int = 0,
        max_edit_rounds: int = 1,
    ) -> SceneSpec:
        """Design next scene+tasks.

        The teacher's ``difficulty_band`` is treated as ADVISORY: we inject
        it into the prompt and log the predicted-vs-band gap, but we do NOT
        reject off-band designs. Only inner JSON-parse retries (3 attempts)
        protect against malformed LLM output.
        """

        current_scene = self._current_scene_desc
        if current_scene_objects:
            current_scene += f"\nObjects: {', '.join(current_scene_objects)}"

        coding_memory_text = ""
        if self.memory:
            coding_memory_text = self.memory.get_prompt_section()

        # ---- Build the curriculum-teacher directive injected into the prompt.
        if difficulty_band is not None:
            lo, hi = float(difficulty_band[0]), float(difficulty_band[1])
            tgt = float(target_difficulty) if target_difficulty is not None else (lo + hi) / 2.0
            difficulty_directive = (
                f"Target difficulty: {tgt:.2f}/10\n"
                f"Acceptable band: [{lo:.2f}, {hi:.2f}] (rubric: path length, "
                f"detour ratio, scene clutter, heading offset, task type)\n"
                f"Hint: difficulty ≈ path_cm/1000 (cap 2.5) + 2.5·blocked_ratio "
                f"+ heading/180 + (1.5 if objectnav else 0). Use min/max_path_cm "
                f"and add/remove objects to land inside the band."
            )
        else:
            tgt = float(target_difficulty) if target_difficulty is not None else self._current_difficulty
            difficulty_directive = (
                f"Target difficulty: {tgt:.2f}/10 (no hard band — best effort)."
            )
        self._last_scene_streak = current_scene_streak

        # ---- Editing-phase directive: tell the LLM how many rounds it has
        # left within this epoch and how to signal "done".
        if max_edit_rounds > 1:
            remaining = max(0, max_edit_rounds - 1 - edit_round)
            edit_phase_directive = (
                f"You are in editing round {edit_round + 1} / {max_edit_rounds} "
                f"of this epoch (rounds left after this one: {remaining}).\n"
                "Look at ENVIRONMENT FEEDBACK above. If the previous build is "
                "satisfactory (or you have nothing more to fix), output "
                '"action": "keep_scene" with empty add_objects/remove_objects '
                "to finalise this epoch. Otherwise emit a small targeted "
                "modify_scene to fix the reported issues. The same embodied "
                "agent will then run episodes on whatever scene you finalise."
            )
        else:
            edit_phase_directive = (
                "Single-shot scene design (no in-epoch re-edits available)."
            )

        def _build_prompt(retry_feedback: str = "") -> str:
            return CODING_AGENT_PROMPT.format(
                performance_history=performance_history or "(first epoch)",
                strategies=strategies or "(none yet)",
                failure_patterns=failure_patterns or "(none yet)",
                current_scene=current_scene,
                current_difficulty=f"{self._current_difficulty:.1f}",
                coding_memory=coding_memory_text or "(no experience yet)",
                asset_catalog=SceneManager.get_asset_catalog_prompt(),
                asset_keys=", ".join(sorted(ASSET_CATALOG.keys())),
                rolling_summary=rolling_summary,
                difficulty_directive=difficulty_directive,
                retry_feedback=retry_feedback,
                build_feedback=(prev_build_report.to_prompt()
                                if prev_build_report is not None
                                else "(no previous build)"),
                edit_phase_directive=edit_phase_directive,
            )

        def _try_once(prompt: str) -> Optional[SceneSpec]:
            for attempt in range(3):
                try:
                    raw = self._llm_call(prompt)
                    return self._parse(raw)
                except Exception as exc:
                    log.warning("CodingAgent JSON attempt %d failed: %s: %s",
                                attempt + 1, type(exc).__name__, exc)
            return None

        # Single LLM attempt (with internal JSON-parse retries). Band is
        # advisory — we log predicted-vs-band but never reject the design.
        del max_band_retries  # explicitly ignored; band is advisory now
        spec = _try_once(_build_prompt(""))
        if spec is not None:
            predicted = predict_spec_difficulty(spec, prev_blocked_ratio)
            spec._predicted_difficulty = predicted
            if difficulty_band is not None:
                lo, hi = float(difficulty_band[0]), float(difficulty_band[1])
                inside = lo <= predicted <= hi
                gap = 0.0 if inside else max(lo - predicted, predicted - hi)
                log.info(
                    "CodingAgent suggestion check: predicted=%.2f band=[%.2f,%.2f] -> %s (gap=%.2f)",
                    predicted, lo, hi, "IN-BAND" if inside else "OFF-BAND", gap,
                )
            else:
                log.info("CodingAgent: predicted difficulty=%.2f (no band)", predicted)
            self._last_successful_spec = spec
            return spec

        # JSON parse failed every retry — fall back to last successful design.
        if self._last_successful_spec is not None:
            log.warning("CodingAgent: using last successful design (all retries failed)")
            return self._last_successful_spec

        # True first call with no history — minimal default
        log.warning("CodingAgent: first call, no history, using minimal default")
        spec = SceneSpec(
            scene_id=self._current_scene_id,
            description="empty field",
            task_type="pointnav",
            min_path_cm=500.0, max_path_cm=1000.0,
            max_steps=25, n_episodes=4,
            reasoning="Initial: empty field, short paths",
        )
        spec._is_new_scene = False
        self._last_successful_spec = spec
        return spec

    def _parse(self, raw: str) -> SceneSpec:
        """Parse LLM output. Raises on failure (caller retries)."""
        text = raw.strip()
        # Strip ALL thinking formats
        text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL).strip()
        text = re.sub(r"^.*?</think>", "", text, flags=re.DOTALL).strip()
        text = re.sub(r"```(?:json)?", "", text).strip()

        # Find last complete {...} block
        last_end = text.rfind("}")
        if last_end == -1:
            raise ValueError("No closing brace in output")

        depth = 0
        start = -1
        for i in range(last_end, -1, -1):
            if text[i] == '}':
                depth += 1
            elif text[i] == '{':
                depth -= 1
                if depth == 0:
                    start = i
                    break
        if start == -1:
            raise ValueError("No matched braces")

        json_str = text[start:last_end + 1]
        # Fix common JSON issues — apply trailing-comma strip first, then try
        # to parse.  Only fall back to the apostrophe->quote replacement when
        # the first attempt fails: the replacement corrupts valid JSON that
        # contains apostrophes inside string values (e.g. "it's better to…").
        json_str = re.sub(r',\s*}', '}', json_str)
        json_str = re.sub(r',\s*]', ']', json_str)
        try:
            data = json.loads(json_str)
        except json.JSONDecodeError:
            # Retry: replace single-quote delimiters (LLM Python-dict output)
            json_str2 = json_str.replace("'", '"')
            json_str2 = re.sub(r',\s*}', '}', json_str2)
            json_str2 = re.sub(r',\s*]', ']', json_str2)
            data = json.loads(json_str2)

        action = data.get("action", "keep_scene")
        is_new = action == "new_scene"
        is_modify = action == "modify_scene"

        # Note: the previous "3-epoch new_scene downgrade" guardrail was
        # removed when the curriculum teacher (teacher.py) took over
        # difficulty control — the teacher already governs when to switch
        # regimes via its propose() output.

        if is_new or is_modify:
            self._scene_counter += 1
            scene_id = f"scene_{self._scene_counter:03d}"
            self._current_scene_desc = data.get("scene_description",
                                                  f"scene with objects")
        else:
            scene_id = self._current_scene_id

        # Parse objects to add (from "add_objects" or legacy "objects")
        objects = []
        obj_list = data.get("add_objects", data.get("objects", []))
        if (is_new or is_modify) and obj_list:
            for obj in obj_list:
                asset_key = obj.get("asset", "")
                if asset_key not in ASSET_CATALOG:
                    continue
                # No clamp — trust the LLM. Default to map center if missing.
                CX, CY, CZ = 7661.0, 10970.0, 100.0
                ox = float(obj.get("x", CX))
                oy = float(obj.get("y", CY))
                oz = float(obj.get("z", CZ))
                objects.append(SpawnedObject(
                    actor_name=obj.get("name", f"obj_{len(objects)}"),
                    asset_key=asset_key,
                    x=ox,
                    y=oy,
                    z=oz,
                    yaw=float(obj.get("yaw", 0)),
                ))

        # Parse objects to remove
        remove_names = []
        if is_modify:
            remove_names = data.get("remove_objects", [])

        # The curriculum teacher owns difficulty selection now; we only
        # track the LLM's self-reported target_difficulty for logging /
        # legacy compatibility. No clipping here — the band-retry loop in
        # design() is the enforcement mechanism.
        target_diff = float(data.get("target_difficulty", self._current_difficulty))
        self._current_difficulty = target_diff
        if target_diff > self._best_difficulty:
            self._best_difficulty = target_diff

        # Single path length (deterministic difficulty). Accept `path_cm`
        # (preferred) or fall back to legacy min/max midpoint.
        if "path_cm" in data:
            path_cm = max(500.0, min(5000.0, float(data["path_cm"])))
        else:
            lo = float(data.get("min_path_cm", 800))
            hi = float(data.get("max_path_cm", lo + 400))
            path_cm = max(500.0, min(5000.0, (lo + hi) / 2.0))
        # Sampling tolerance: ±15% around the target path_cm. Difficulty is
        # still computed deterministically from the center `path_cm` (see
        # loop.py predict_spec_difficulty), so this only relaxes geometry
        # constraints for the navmesh sampler — the coding agent still owns
        # the difficulty knob.
        path_lo = max(500.0, path_cm * 0.85)
        path_hi = min(5000.0, path_cm * 1.15)
        spec = SceneSpec(
            scene_id=scene_id,
            description=self._current_scene_desc,
            objects=objects,
            task_type=str(data.get("task_type", "pointnav")),
            min_path_cm=path_lo,
            max_path_cm=path_hi,
            max_steps=min(40, max(15, int(data.get("max_steps", 25)))),
            n_episodes=min(12, max(4, int(data.get("n_episodes", 8)))),
            reasoning=str(data.get("reasoning", "")),
        )
        spec._is_new_scene = is_new
        spec._is_modify = is_modify
        spec._remove_names = remove_names
        self._current_scene_id = scene_id
        return spec
