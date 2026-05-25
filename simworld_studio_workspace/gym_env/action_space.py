"""LLM-facing action space for navigation tasks.

Source of truth for the **action set** is
``nav_task.task_spec.NAVIGATION_ACTIONS`` (4 discrete actions:
MOVE_FORWARD / TURN_LEFT / TURN_RIGHT / STOP).  We keep the names and
descriptions from there for Habitat parity.

We do NOT use that module's ``simworld_cmd`` templates for the actual
UnrealCV commands.  Two reasons:

  1. Their TurnAround template is wrong — it embeds the literal string
     ``left`` / ``right``, but the BP expects two **numbers**:
     ``vbp {agent} TurnAround 1 <signed_angle> <±1>`` where the sign
     of ``angle`` and the sign of ``clockwise`` together encode the
     direction.  Verified against the SimWorld Python wrapper at
     ``SimWorld/simworld/communicator/unrealcv.py:619-635``.
  2. ``StepForward`` is parameterised on duration; making it a literal
     in the template prevents us from tuning per-experiment.

So we ignore ``simworld_cmd`` and build the raw command in
:func:`translate_action`, matching the canonical implementation.

Two outputs:

  * :func:`nav_tool_schemas` returns OpenAI / Anthropic-compatible
    tool schemas suitable for ``chat.completions.create(tools=...)``
    or ``messages.create(tools=...)``.

  * :func:`translate_action` converts an LLM tool-call dict into the
    raw UnrealCV ``vbp`` command string.
"""

from __future__ import annotations

from typing import Any, Dict, List

from nav_task.task_spec import NAVIGATION_ACTIONS

NAV_TOOL_NAMES = tuple(a.name for a in NAVIGATION_ACTIONS)

# Canonical descriptions taken from nav_task; can be overridden if needed.
_DESCRIPTIONS = {a.name: a.description for a in NAVIGATION_ACTIONS}

# Default per-action parameters (override in env config if you want).
DEFAULT_FORWARD_DURATION_S = 2.0     # StepForward duration (each step ~4m at 200cm/s)
DEFAULT_TURN_ANGLE_DEG = 30.0        # TurnAround magnitude


def nav_tool_schemas() -> List[Dict[str, Any]]:
    """Return the unified (OpenAI-shape) tool list for the 4 nav actions.

    All four actions are parameter-free at the LLM level: the model
    picks an action by name; numerical parameters (step duration, turn
    angle) are fixed in the env, not chosen by the model.
    """
    return [
        {
            "name": name,
            "description": _DESCRIPTIONS.get(name, ""),
            "parameters": {
                "type": "object",
                "properties": {},
                "required": [],
            },
        }
        for name in NAV_TOOL_NAMES
    ]


def translate_action(
    action: Dict[str, Any],
    agent_name: str,
    *,
    forward_duration_s: float = DEFAULT_FORWARD_DURATION_S,
    turn_angle_deg: float = DEFAULT_TURN_ANGLE_DEG,
) -> str:
    """Convert ``{"tool": "MOVE_FORWARD", "params": {}}`` to a ``vbp`` command.

    Verified BP signatures (from SimWorld's
    ``communicator.unrealcv.UnrealCV``):

    * ``vbp {agent} StepForward {duration_seconds} {direction}``
      where ``direction`` is 0 (forward) or 1 (backward).
    * ``vbp {agent} TurnAround 1 {signed_angle} {clockwise}``
      where ``clockwise = +1`` for right, ``-1`` for left, and the
      angle is **negated** for left turns.  This double-encoding is
      historical but required.
    * ``vbp {agent} StopAgent`` for STOP.

    Raises
    ------
    ValueError
        If ``tool`` is not one of the four navigation actions.
    """
    name = action.get("tool") or action.get("name")
    if name is None:
        raise ValueError(f"action dict missing 'tool': {action!r}")
    if name not in NAV_TOOL_NAMES:
        raise ValueError(
            f"unknown nav action {name!r}; expected one of {NAV_TOOL_NAMES}"
        )

    if name == "MOVE_FORWARD":
        return f"vbp {agent_name} StepForward {forward_duration_s} 0"
    if name == "TURN_LEFT":
        # Verified: communicator.unrealcv.humanoid_rotate at line 619-632
        # left  → angle negated, clockwise = -1
        return f"vbp {agent_name} TurnAround 1 {-turn_angle_deg} -1"
    if name == "TURN_RIGHT":
        # right → angle as-is,  clockwise = +1
        return f"vbp {agent_name} TurnAround 1 {turn_angle_deg} 1"
    if name == "STOP":
        return f"vbp {agent_name} StopAgent"
    # unreachable
    raise AssertionError(name)


def is_stop_action(action: Dict[str, Any]) -> bool:
    name = action.get("tool") or action.get("name")
    return name == "STOP"
