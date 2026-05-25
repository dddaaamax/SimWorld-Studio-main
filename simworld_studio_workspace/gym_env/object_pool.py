"""Curated pool of small objects for ObjectNav search tasks.

Each entry describes one distinct small object that can be spawned
into the scene as an ObjectNav target.  The pool mixes existing
Blueprints (fully configured actors with collision and physics) and
raw StaticMesh assets (lightweight, no BP overhead).

Design goals:
  * **Visually distinctive** — every entry should be easy to tell
    apart in a first-person RGB frame (different silhouette/colour).
  * **Small scale** — fit inside ~1 m^3 so the agent has to get
    close to see them.  Rules out buildings, vehicles, trees, road
    infrastructure.
  * **Single instance friendly** — unique per episode, so the
    landmark-relative description is unambiguous.

The pool is loaded by :func:`sample_target_objects` in
``episode_builder.py``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import List, Literal


SpawnKind = Literal["blueprint", "static_mesh"]


@dataclass(frozen=True)
class ObjectSpec:
    """One candidate small object that can be spawned as a target.

    Attributes:
        asset_path: Full UE package path, e.g.
            ``/Game/CityDatabase/blueprints/BP_Hydrant.BP_Hydrant_C``
            (blueprint) or
            ``/Game/Asian_town/Assets/Lamp/SM_lamp_small.SM_lamp_small``
            (static mesh).
        kind: ``"blueprint"`` → spawn via ``vset /objects/spawn_bp_asset``;
            ``"static_mesh"`` → spawn a StaticMeshActor pointing at the mesh.
        category: Coarse category (used for metrics, color mask, etc.).
        nouns: Human-readable nouns the VLM/LLM can use when generating
            a description ("red fire hydrant", "trash can", ...).  First
            entry is the canonical name.
        approx_size_cm: Rough (w, h) bounding box in centimetres.  Used
            to space multiple targets so they don't overlap.
    """

    asset_path: str
    kind: SpawnKind
    category: str
    nouns: tuple
    approx_size_cm: tuple


# ── Curated pool ─────────────────────────────────────────────────────
#
# Blueprints (first — these already work with spawn_bp_asset):

_BLUEPRINTS: List[ObjectSpec] = [
    ObjectSpec(
        "/Game/CityDatabase/blueprints/BP_Hydrant.BP_Hydrant_C",
        "blueprint", "infrastructure",
        ("red fire hydrant", "hydrant", "fireplug"),
        (50, 100),
    ),
    ObjectSpec(
        "/Game/CityDatabase/blueprints/BP_Trash_bin_a.BP_Trash_bin_a_C",
        "blueprint", "container",
        ("metal trash bin", "trash bin", "rubbish bin"),
        (60, 120),
    ),
    ObjectSpec(
        "/Game/CityDatabase/blueprints/BP_Trash_bin_b.BP_Trash_bin_b_C",
        "blueprint", "container",
        ("green plastic wheelie bin", "wheelie bin", "trash cart"),
        (70, 130),
    ),
    ObjectSpec(
        "/Game/CityDatabase/blueprints/BP_Trash_can.BP_Trash_can_C",
        "blueprint", "container",
        ("black trash can", "trash can", "waste bin"),
        (50, 90),
    ),
    ObjectSpec(
        "/Game/CityDatabase/blueprints/BP_Can.BP_Can_C",
        "blueprint", "can",
        ("soda can on the ground", "crumpled can", "drink can"),
        (10, 15),
    ),
    ObjectSpec(
        "/Game/CityDatabase/blueprints/BP_Can2.BP_Can2_C",
        "blueprint", "can",
        ("rusty metal can", "tin can", "food can"),
        (10, 15),
    ),
    ObjectSpec(
        "/Game/CityDatabase/blueprints/BP_Soda1.BP_Soda1_C",
        "blueprint", "bottle",
        ("red soda bottle", "cola bottle", "plastic bottle"),
        (10, 25),
    ),
    ObjectSpec(
        "/Game/CityDatabase/blueprints/BP_Soda2.BP_Soda2_C",
        "blueprint", "bottle",
        ("green soda bottle", "lemon-lime bottle", "plastic drink bottle"),
        (10, 25),
    ),
    ObjectSpec(
        "/Game/CityDatabase/blueprints/BP_Soda3.BP_Soda3_C",
        "blueprint", "bottle",
        ("blue soda bottle", "plastic bottle", "drink bottle"),
        (10, 25),
    ),
    ObjectSpec(
        "/Game/CityDatabase/blueprints/BP_Soda4.BP_Soda4_C",
        "blueprint", "bottle",
        ("orange soda bottle", "plastic drink bottle", "bottle"),
        (10, 25),
    ),
    ObjectSpec(
        "/Game/CityDatabase/blueprints/BP_Rabbish.BP_Rabbish_C",
        "blueprint", "debris",
        ("pile of rubbish", "trash pile", "debris"),
        (80, 40),
    ),
    ObjectSpec(
        "/Game/CityDatabase/blueprints/BP_Box.BP_Box_C",
        "blueprint", "box",
        ("cardboard box", "crate", "shipping box"),
        (60, 60),
    ),
    ObjectSpec(
        "/Game/CityDatabase/blueprints/BP_Box2.BP_Box2_C",
        "blueprint", "box",
        ("wooden crate", "timber box", "packing crate"),
        (70, 60),
    ),
    ObjectSpec(
        "/Game/CityDatabase/blueprints/BP_Box3.BP_Box3_C",
        "blueprint", "box",
        ("dark plastic crate", "plastic box", "milk crate"),
        (50, 40),
    ),
    ObjectSpec(
        "/Game/CityDatabase/blueprints/BP_RoadCone.BP_RoadCone_C",
        "blueprint", "cone",
        ("orange traffic cone", "road cone", "construction cone"),
        (40, 70),
    ),
    ObjectSpec(
        "/Game/CityDatabase/blueprints/BP_RoadBlocker.BP_RoadBlocker_C",
        "blueprint", "barrier",
        ("yellow road barrier", "traffic barrier", "jersey barrier"),
        (120, 80),
    ),
]

# Static meshes (spawn via a StaticMeshActor):

_STATIC_MESHES: List[ObjectSpec] = [
    ObjectSpec(
        "/Game/Asian_town/Assets/Barrel/SM_barrel_01.SM_barrel_01",
        "static_mesh", "barrel",
        ("wooden barrel", "oak barrel", "old barrel"),
        (60, 90),
    ),
    ObjectSpec(
        "/Game/Asian_town/Assets/Barrel/SM_barrel_02.SM_barrel_02",
        "static_mesh", "barrel",
        ("rusty metal barrel", "steel drum", "oil drum"),
        (60, 90),
    ),
    ObjectSpec(
        "/Game/Asian_town/Assets/Umbrella/SM_umbrella_01.SM_umbrella_01",
        "static_mesh", "umbrella",
        ("red market umbrella", "parasol", "sunshade"),
        (200, 250),
    ),
    ObjectSpec(
        "/Game/Asian_town/Assets/Umbrella/SM_umbrella_02.SM_umbrella_02",
        "static_mesh", "umbrella",
        ("striped market umbrella", "patio umbrella", "parasol"),
        (200, 250),
    ),
    ObjectSpec(
        "/Game/Asian_town/Assets/Lamp/SM_Lamp_small.SM_Lamp_small",
        "static_mesh", "lamp",
        ("small paper lantern", "lantern", "hanging lamp"),
        (30, 50),
    ),
]


# Full pool — blueprints first so old scripts that only understand
# BPs still work by slicing ``get_pool()[:len(_BLUEPRINTS)]``.
_POOL: List[ObjectSpec] = _BLUEPRINTS + _STATIC_MESHES


def get_pool(kinds: tuple = ("blueprint", "static_mesh")) -> List[ObjectSpec]:
    """Return the candidate small-object pool, optionally filtered by kind."""
    return [o for o in _POOL if o.kind in kinds]


def canonical_noun(spec: ObjectSpec) -> str:
    """Return the first noun from ``spec.nouns`` (canonical name)."""
    return spec.nouns[0]
