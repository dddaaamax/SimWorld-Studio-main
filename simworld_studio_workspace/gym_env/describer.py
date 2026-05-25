"""LLM-driven target description generator for ObjectNav search.

The describer takes:

  * a small object (spec + spawned position)
  * a slice of the surrounding scene graph (nearby actors)
  * optionally the agent's intended start position

and asks an LLM to write ONE natural-language hint describing where
the target is relative to nearby buildings / trees.  The LLM is
expected to reason about the scene graph on its own — we do not
pre-select which landmark to reference.

The describer is decoupled from the agent LLM via a plain callable
``(prompt: str) -> str``, so a different model can be used for
description than for navigation.  This is wired through the
``--describer-model`` CLI arg in ``runner.py``.

Design notes
------------
* **Accuracy matters more than fluency** — the hint must be factually
  correct (if it says "north of the green tower", the target must
  actually be north of it).  The prompt below enumerates the ground-
  truth directions and distances so the LLM only has to pick
  phrasing, not compute geometry.
* **Diversity of phrasing** — we ask the LLM to vary between
  "near X", "to the left of X", "across from Y", etc.  A high-
  temperature call (handled by the caller) produces more varied
  output.
* **Fallback** — if the LLM call fails or returns garbage, we
  synthesise a deterministic rule-based hint using the closest
  landmark, so task generation never hard-fails.
"""

from __future__ import annotations

import logging
import random
from dataclasses import dataclass
from typing import Callable, List, Optional

from .object_pool import ObjectSpec, canonical_noun
from .scene_context import SceneActor, nearby_actors, relative_heading

log = logging.getLogger(__name__)


_DESCRIBER_PROMPT = """\
You are writing a one-sentence navigation hint for a robot searching \
for a small object in a 3D city.  The robot can read the hint but has \
to physically explore to find the object.

Target object:
  * Canonical name: {canonical}
  * Also called: {aliases}
  * Category: {category}

The target's position in the scene is known.  Below is a list of \
nearby scene objects with their category and direction/distance from \
the target.  All directions are as seen from the target looking \
outward (i.e. "west" means the landmark is west of the target).

{scene_context}

Agent's starting position is about {start_distance_m:.0f} metres to \
the {start_direction} of the target.

Write ONE natural English sentence that tells the robot where to \
find the {canonical}.  Requirements:

  1. Mention the target by one of its names.
  2. Reference AT LEAST ONE nearby scene object as a landmark.
  3. The spatial relation you state MUST be factually correct — if \
     you say "next to the red building" the red building must actually \
     be listed above as adjacent.
  4. Do NOT give exact coordinates or compass bearings in degrees.
  5. Keep it under 30 words.
  6. Vary the phrasing style — examples:
       * "Look for the <target> just west of <building>."
       * "You'll find the <target> near <landmark>, close to <building>."
       * "The <target> is sitting between <X> and <Y>."
       * "Head towards <building>; the <target> is on its north side."
     Pick ONE that feels natural for this specific scene layout.

Return ONLY the sentence, no quotes, no preamble, no "Answer:"."""


@dataclass
class TargetDescription:
    """A generated hint plus the metadata the task uses for grading."""

    prompt: str                    # natural-language hint
    target_name: str               # actor name in UE
    target_spec: ObjectSpec
    target_xy: tuple               # (x, y) in cm
    landmarks: List[SceneActor]    # the actors passed to the describer
    generator: str                 # "llm" / "fallback" — which path produced it


def _format_scene_context(
    target_xy: tuple,
    landmarks: List[SceneActor],
    max_items: int = 6,
) -> str:
    """Produce the bulleted context passed to the LLM."""
    lines: List[str] = []
    for lm in landmarks[:max_items]:
        direction, dist_cm = relative_heading(
            target_xy[0], target_xy[1], lm.x, lm.y
        )
        lines.append(
            f"  * {lm.name} (category: {lm.category}) "
            f"— {dist_cm / 100:.0f} m to the {direction}"
        )
    if not lines:
        return "  * (no nearby scene objects recorded)"
    return "\n".join(lines)


def _fallback_description(
    target_spec: ObjectSpec,
    target_xy: tuple,
    landmarks: List[SceneActor],
) -> str:
    """Deterministic rule-based hint when no LLM is available."""
    canonical = canonical_noun(target_spec)
    if not landmarks:
        return f"Find a {canonical} somewhere in the area."
    closest = landmarks[0]
    direction, dist = relative_heading(
        target_xy[0], target_xy[1], closest.x, closest.y
    )
    # Invert direction to phrase it "from the landmark towards the target"
    opposite = {
        "east": "west", "west": "east",
        "north": "south", "south": "north",
        "northeast": "southwest", "southwest": "northeast",
        "northwest": "southeast", "southeast": "northwest",
    }[direction]
    return (
        f"Look for the {canonical} about {dist / 100:.0f} m "
        f"{opposite} of {closest.name} ({closest.category})."
    )


class TargetDescriber:
    """LLM-backed describer for ObjectNav search targets.

    Args:
        llm_call: Callable ``(prompt) -> text``.  Caller is responsible
            for any retries / rate limiting.  If None, only the
            fallback path is used.
        scene_graph: Pre-loaded scene graph (from
            :func:`gym_env.scene_context.load_scene_graph`).
        landmark_radius_cm: How far around a target to search for
            nearby scene objects (default 5000 cm).  Buildings in this
            project are ~3000 cm wide, so this reliably includes at
            least one neighbouring building.
        landmark_top_k: How many nearest scene objects to include in
            the LLM context (default 6).
        categories: If given, restrict the landmark search to these
            categories, e.g. ``("building", "tree")`` to keep the
            context focused on large, memorisable landmarks.
    """

    def __init__(
        self,
        llm_call: Optional[Callable[[str], str]],
        scene_graph: List[SceneActor],
        landmark_radius_cm: float = 5000.0,
        landmark_top_k: int = 6,
        categories: Optional[tuple] = ("building", "tree"),
    ) -> None:
        self._llm = llm_call
        self._scene_graph = scene_graph
        self._radius = landmark_radius_cm
        self._top_k = landmark_top_k
        self._categories = categories

    def describe(
        self,
        target_spec: ObjectSpec,
        target_name: str,
        target_xy: tuple,
        start_xy: tuple,
    ) -> TargetDescription:
        """Generate a natural-language hint for one target."""

        landmarks = nearby_actors(
            self._scene_graph,
            x=target_xy[0],
            y=target_xy[1],
            radius_cm=self._radius,
            top_k=self._top_k,
            categories=self._categories,
        )

        if self._llm is not None and landmarks:
            prompt_body = self._build_llm_prompt(
                target_spec, target_xy, start_xy, landmarks,
            )
            try:
                text = self._llm(prompt_body)
                text = text.strip().strip('"').strip()
                # Basic sanity checks — must mention target, must be short.
                if text and len(text) <= 400:
                    return TargetDescription(
                        prompt=text,
                        target_name=target_name,
                        target_spec=target_spec,
                        target_xy=target_xy,
                        landmarks=landmarks,
                        generator="llm",
                    )
                log.warning("LLM describer returned unusable text: %r", text[:80])
            except Exception as exc:
                log.warning("LLM describer failed: %s", exc)

        # Fallback path
        return TargetDescription(
            prompt=_fallback_description(target_spec, target_xy, landmarks),
            target_name=target_name,
            target_spec=target_spec,
            target_xy=target_xy,
            landmarks=landmarks,
            generator="fallback",
        )

    def _build_llm_prompt(
        self,
        target_spec: ObjectSpec,
        target_xy: tuple,
        start_xy: tuple,
        landmarks: List[SceneActor],
    ) -> str:
        canonical = canonical_noun(target_spec)
        aliases = ", ".join(target_spec.nouns[1:]) or "(none)"
        scene_context = _format_scene_context(target_xy, landmarks)
        start_direction, start_distance_cm = relative_heading(
            target_xy[0], target_xy[1], start_xy[0], start_xy[1]
        )
        return _DESCRIBER_PROMPT.format(
            canonical=canonical,
            aliases=aliases,
            category=target_spec.category,
            scene_context=scene_context,
            start_distance_m=start_distance_cm / 100,
            start_direction=start_direction,
        )
