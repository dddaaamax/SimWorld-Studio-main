"""Ghost-mode wrappers for SimWorld UnrealCV and Communicator.

Extends the upstream SimWorld classes **by inheritance** so the original
submodule stays untouched.

Ghost agents:
  - Are hidden (``SetActorHiddenInGame``) — invisible to all cameras.
  - Use a custom collision channel so they pass through each other.
  - Still collide normally with buildings, terrain, vehicles, and objects.

Requires the ``SetCollisionObjectType`` and ``SetCollisionResponseToChannel``
commands to be registered in the UnrealCV plugin's ObjectHandler.

UE collision channel layout (indices):
    0  WorldStatic      4  Camera
    1  WorldDynamic     5  PhysicsBody
    2  Pawn             6  Vehicle
    3  Visibility       7  Destructible
    8  GameTraceChannel1  ← we use this as "GhostAgent"
"""

from __future__ import annotations

from simworld.communicator.unrealcv import UnrealCV
from simworld.communicator.communicator import Communicator

# ECC_GameTraceChannel1 — must match DefaultEngine.ini custom channel registration
GHOST_COLLISION_CHANNEL = 8


class GhostUnrealCV(UnrealCV):
    """UnrealCV with ghost-mode primitives (hide, collision channel control)."""

    def set_hidden(self, actor_name: str, hidden: bool = True) -> None:
        """Hide or show an actor via the existing ``vset /object/.../hide|show``."""
        cmd = f'vset /object/{actor_name}/{"hide" if hidden else "show"}'
        with self.lock:
            self.client.request(cmd)

    def set_collision_object_type(self, actor_name: str, channel_index: int) -> None:
        """Set the collision object-type channel for an actor (0-31)."""
        cmd = f'vset /object/{actor_name}/collision_channel {channel_index}'
        with self.lock:
            self.client.request(cmd)

    def set_collision_response(self, actor_name: str, channel_index: int, response: str) -> None:
        """Set collision response to a channel: ``ignore``, ``overlap``, or ``block``."""
        cmd = f'vset /object/{actor_name}/collision_response {channel_index} {response}'
        with self.lock:
            self.client.request(cmd)


class GhostCommunicator(Communicator):
    """Communicator that can spawn ghost agents.

    Drop-in replacement: all normal ``Communicator`` behaviour is preserved.
    Pass a :class:`GhostUnrealCV` instance at construction time.

    Normal ``spawn_agent()`` is completely unaffected — ghost logic only
    activates when you explicitly call ``spawn_ghost_agent()`` or
    ``enable_ghost_mode()``.
    """

    def __init__(self, unrealcv: GhostUnrealCV = None):
        super().__init__(unrealcv)
        self._ghost_agent_names: set[str] = set()

    # ------------------------------------------------------------------
    # Ghost-mode helpers
    # ------------------------------------------------------------------

    def enable_ghost_mode(self, actor_name: str) -> None:
        """Turn an already-spawned actor into a ghost.

        * Hidden from all camera views.
        * Collision disabled during channel reconfiguration to prevent
          CharacterMovementComponent depenetration pushback.
        * Object type → GhostAgent channel (8).
        * Ignores other ghosts (channel 8) and normal Pawns (channel 2).
        * Still blocks WorldStatic (buildings), WorldDynamic (objects),
          Vehicle, PhysicsBody, etc.
        """
        ucv: GhostUnrealCV = self.unrealcv
        # Disable collision first to prevent any interaction during setup
        ucv.set_collision(actor_name, False)
        ucv.set_hidden(actor_name, True)
        ucv.set_collision_object_type(actor_name, GHOST_COLLISION_CHANNEL)
        ucv.set_collision_response(actor_name, GHOST_COLLISION_CHANNEL, 'ignore')
        ucv.set_collision_response(actor_name, 2, 'ignore')  # ECC_Pawn
        ucv.set_collision(actor_name, True)
        self._ghost_agent_names.add(actor_name)

    def disable_ghost_mode(self, actor_name: str) -> None:
        """Restore normal visibility and collision."""
        ucv: GhostUnrealCV = self.unrealcv
        ucv.set_hidden(actor_name, False)
        ucv.set_collision_object_type(actor_name, 2)  # ECC_Pawn
        ucv.set_collision_response(actor_name, GHOST_COLLISION_CHANNEL, 'block')
        ucv.set_collision_response(actor_name, 2, 'block')
        self._ghost_agent_names.discard(actor_name)

    def teleport_ghost(self, actor_name: str, location: tuple) -> None:
        """Teleport a ghost agent to a position without depenetration pushback.

        Temporarily disables collision so UE's CharacterMovementComponent
        does not push overlapping ghosts apart on the next tick.
        """
        self.unrealcv.set_collision(actor_name, False)
        self.unrealcv.set_location(location, actor_name)
        self.unrealcv.set_collision(actor_name, True)

    def spawn_ghost_agent(
        self,
        agent,
        name=None,
        position=None,
        model_path='/Game/TrafficSystem/Pedestrian/Base_User_Agent.Base_User_Agent_C',
        type='humanoid',
    ):
        """Spawn an agent in ghost mode (hidden + selective collision).

        The actor is spawned normally, immediately switched to ghost mode,
        then teleported to the target position with collision temporarily
        off to avoid depenetration pushback.
        """
        self.spawn_agent(agent, name, position, model_path, type)

        if type == 'humanoid':
            actor_name = self.get_humanoid_name(agent.id)
        else:
            actor_name = name

        self.enable_ghost_mode(actor_name)

        # Re-teleport with collision off to fix any CMC depenetration
        if position is not None:
            self.teleport_ghost(actor_name, position)
        elif agent is not None:
            self.teleport_ghost(actor_name, (agent.position.x, agent.position.y, 600))

    @property
    def ghost_agent_names(self) -> set[str]:
        return set(self._ghost_agent_names)
