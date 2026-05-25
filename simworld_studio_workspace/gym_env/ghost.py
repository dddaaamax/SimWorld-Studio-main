"""Ghost-mode helpers for the gym_env UCVClient path.

Provides functions that work directly with :class:`UCVClient` to
enable/disable ghost mode on spawned humanoid agents.  This is the
gym_env-native counterpart of ``utils/ghost_mode.py`` (which wraps
SimWorld's ``UnrealCV`` / ``Communicator`` classes).

Ghost agents:
  - Are hidden from all cameras (``SetActorHiddenInGame``).
  - Use collision channel 8 (``GhostAgent`` / ECC_GameTraceChannel1).
  - Ignore other ghosts (channel 8) and normal Pawns (channel 2).
  - Still block WorldStatic (buildings), WorldDynamic, Vehicles, etc.

Requires the ``collision_channel`` and ``collision_response`` commands
in the UnrealCV plugin (ObjectHandler C++ patch).
"""

from __future__ import annotations

from .ucv_client import UCVClient

GHOST_CHANNEL = 8  # ECC_GameTraceChannel1


def enable_ghost(ucv: UCVClient, actor: str) -> None:
    """Enable ghost mode on a spawned actor."""
    ucv.send(f"vset /object/{actor}/collision false")
    ucv.send(f"vset /object/{actor}/hide")
    ucv.send(f"vset /object/{actor}/collision_channel {GHOST_CHANNEL}")
    ucv.send(f"vset /object/{actor}/collision_response {GHOST_CHANNEL} ignore")
    ucv.send(f"vset /object/{actor}/collision_response 2 ignore")
    ucv.send(f"vset /object/{actor}/collision true")


def disable_ghost(ucv: UCVClient, actor: str) -> None:
    """Restore normal visibility and collision."""
    ucv.send(f"vset /object/{actor}/show")
    ucv.send(f"vset /object/{actor}/collision_channel 2")
    ucv.send(f"vset /object/{actor}/collision_response {GHOST_CHANNEL} block")
    ucv.send(f"vset /object/{actor}/collision_response 2 block")


def teleport_ghost(ucv: UCVClient, actor: str, x: float, y: float, z: float) -> None:
    """Teleport a ghost without depenetration pushback."""
    ucv.send(f"vset /object/{actor}/collision false")
    ucv.send(f"vset /object/{actor}/location {x} {y} {z}")
    ucv.send(f"vset /object/{actor}/collision true")
