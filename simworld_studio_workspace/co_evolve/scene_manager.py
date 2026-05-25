"""Scene manager: build/clear UE scenes via UnrealCV.

Handles spawning and destroying objects to create training environments.
The coding agent outputs a SceneSpec, and this module materializes it in UE.
"""
from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Asset catalog — Hwaseong palace map only.
#
# We deliberately RESTRICT the catalog to assets that ship with the
# active HwaseongHaenggung map. The previous catalog also exposed
# ~150 BPs from /Game/CityDatabase, but the coding LLM kept defaulting
# to those generic city blueprints which are visually inconsistent with
# the palace map and clutter the prompt. Only ``hwaseong_*`` keys are
# valid now; any older spec referring to ``building_01`` etc. will be
# rejected by build_scene() with an "unknown asset_key" spawn_failed.
# ---------------------------------------------------------------------------

# Static-mesh buildings shipped under /Game/HwaseongHaenggung/Meshes/Buildings.
# Paths do NOT end in "_C": build_scene() dispatches them through
# ucv.spawn_static_mesh() (the BP spawn RPC rejects raw SM assets).
_HWASEONG_MESHES = [
    "SM_Bijangcheong", "SM_Bokgunyeong", "SM_Boknaedang", "SM_Bongsudang",
    "SM_Gyeongryugwan", "SM_Jeonsacheong", "SM_Jibsacheong_1",
    "SM_Jibsacheong_2", "SM_Jungyangmun", "SM_Jwaikmun",
    "SM_Mirohanjeong", "SM_Naeposa", "SM_Naesanmun", "SM_Naknamhyeon",
    "SM_Namgunyeong", "SM_Oijeong", "SM_Oijeongriso", "SM_Oisamun",
    "SM_Punghuadang", "SM_Seoricheong", "SM_Sinpungru",
    "SM_Smallgate_1", "SM_Smallgate_2", "SM_Smallgate_3",
    "SM_Smallgate_4", "SM_Smallgate_5",
    "SM_Unhwagak_Iancheong", "SM_Yuyeotaek",
]


def _build_asset_catalog():
    catalog = {}
    for _name in _HWASEONG_MESHES:
        # key:  hwaseong_<lowercase basename without SM_ prefix>
        _key = "hwaseong_" + _name[3:].lower()
        catalog[_key] = f"/Game/HwaseongHaenggung/Meshes/Buildings/{_name}.{_name}"
    return catalog


ASSET_CATALOG = _build_asset_catalog()

_hwaseong_keys = sorted(ASSET_CATALOG.keys())

ASSET_CATEGORIES = {
    "hwaseong_buildings": _hwaseong_keys,
}


@dataclass
class SpawnedObject:
    """One object in the scene."""
    actor_name: str
    asset_key: str          # key in ASSET_CATALOG
    x: float
    y: float
    z: float = 0.0
    yaw: float = 0.0


@dataclass
class BuildReport:
    """Environment feedback returned to the coding agent after build_scene().

    All fields are derived from REAL UE state queried via UnrealCV after
    spawning — no client-side geometric heuristics. Specifically:

    * ``overlapping_pairs`` comes from world-space AABB intersection using
      ``vget /object/<n>/bounds`` (the engine's actual rendered bounds).
    * ``unreachable_pairs`` comes from ``vget /nav/reachable`` between
      probe points sampled on the play area, so the navmesh (which sees
      the spawned obstacles) is the source of truth.
    * ``floating_in_air`` flags coding-agent objects that have no
      vertical support: the AABB bottom is neither close to the ground
      (``Zmin`` near 0) nor sitting on top of another object whose XY
      footprint fully contains the upper object's footprint
      ("上小下大"). "上大下小" stacks count as floating because
      the upper edges overhang into thin air.
    * ``bounds_unknown`` lists objects whose UE bounds query failed; for
      those we skip overlap/floating checks rather than guessing.

    All checks are scoped to objects spawned by the coding agent
    (``self._spawned_names`` in :class:`SceneManager`). Initial-scene
    actors (whatever was already present at PIE start) are never flagged,
    but they ARE used as the "neighbour" set against which new objects
    are compared.
    """
    spawned: List[str] = field(default_factory=list)
    spawn_failed: List[Tuple[str, str]] = field(default_factory=list)
    overlapping_pairs: List[Tuple[str, str, float]] = field(default_factory=list)
    off_bounds: List[str] = field(default_factory=list)
    unreachable_pairs: List[Tuple[Tuple[float, float], Tuple[float, float]]] = field(default_factory=list)
    n_probes: int = 0
    # (name, height_above_floor_cm, reason). reason is one of
    # "no_support" or "overhang" (上大下小).
    floating_in_air: List[Tuple[str, float, str]] = field(default_factory=list)
    bounds_unknown: List[str] = field(default_factory=list)
    requested: int = 0

    def is_clean(self) -> bool:
        return not (
            self.spawn_failed or self.overlapping_pairs
            or self.off_bounds or self.unreachable_pairs
            or self.floating_in_air
        )

    def to_prompt(self) -> str:
        """Format as a feedback section for the coding agent prompt."""
        if self.requested == 0:
            return "(no scene built last epoch)"
        header = (
            f"Last build: {len(self.spawned)}/{self.requested} placed; "
            f"{self.n_probes - len(self.unreachable_pairs)}/{self.n_probes} "
            f"navmesh probe pairs reachable."
        )
        if self.is_clean():
            return header + " Scene is clean (UE bounds + navmesh agree)."
        lines = [header]
        if self.spawn_failed:
            lines.append("  Spawn failures (UE rejected):")
            for name, reason in self.spawn_failed[:6]:
                lines.append(f"    - {name}: {reason[:60]}")
        if self.off_bounds:
            lines.append("  Off-bounds (x or y outside play area centered at (7661,10970) ±10000):")
            for name in self.off_bounds[:6]:
                lines.append(f"    - {name}")
        if self.overlapping_pairs:
            lines.append(
                "  Overlapping objects (true UE-AABB intersection on XY, "
                "new objects vs. anything already in the scene):"
            )
            for a, b, area in self.overlapping_pairs[:6]:
                lines.append(
                    f"    - {a} ↔ {b} (intersection ≈ {area:.0f} cm^2)"
                )
        if self.floating_in_air:
            lines.append(
                "  Floating in air (no vertical support — must rest on "
                "ground OR sit on another object whose XY footprint fully "
                "contains it; 上大下小 overhangs are NOT supported):"
            )
            for name, h, reason in self.floating_in_air[:6]:
                lines.append(
                    f"    - {name} (Zmin≈{h:.0f} cm above floor, {reason})"
                )
        if self.unreachable_pairs:
            lines.append(
                "  Navmesh BLOCKED — these start→goal probes can no longer "
                "be reached after this layout (scene over-walled):"
            )
            for (s, g) in self.unreachable_pairs[:6]:
                lines.append(
                    f"    - ({s[0]:.0f},{s[1]:.0f}) → ({g[0]:.0f},{g[1]:.0f})"
                )
        # bounds_unknown intentionally omitted from the prompt: it's a
        # diagnostic for humans (asset has no mesh, UCV stalled, ...) and
        # the LLM cannot act on it. We keep it on the report for logging.
        return "\n".join(lines)


@dataclass
class SceneSpec:
    """Complete scene specification output by the coding agent."""
    scene_id: str
    description: str                            # verbal description of the scene
    objects: List[SpawnedObject] = field(default_factory=list)
    task_type: str = "pointnav"                 # "pointnav" or "objectnav"
    min_path_cm: float = 500.0
    max_path_cm: float = 2000.0
    max_steps: int = 30
    n_episodes: int = 4
    reasoning: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "scene_id": self.scene_id,
            "description": self.description,
            "objects": [
                {"name": o.actor_name, "asset": o.asset_key,
                 "x": o.x, "y": o.y, "z": o.z, "yaw": o.yaw}
                for o in self.objects
            ],
            "task_type": self.task_type,
            "min_path_cm": self.min_path_cm,
            "max_path_cm": self.max_path_cm,
            "max_steps": self.max_steps,
            "n_episodes": self.n_episodes,
            "reasoning": self.reasoning,
        }


class SceneManager:
    """Build and clear scenes in UE via UnrealCV."""

    # Ground plane asset — standard UE Cube flattened.
    # Required for NavMesh to work on maps without walkable geometry.
    GROUND_MESH = "/Engine/BasicShapes/Cube.Cube"
    GROUND_NAME = "CoEvolve_Ground"
    GROUND_SCALE = (200, 200, 1)  # 20000x20000x100 cm

    def __init__(self, ucv):
        self.ucv = ucv
        self._spawned_names: List[str] = []
        self._ground_placed = False
        # Monotonic counter used to make every spawned actor name globally
        # unique across the whole run. UE's destroy is asynchronous: the
        # old actor lingers in PersistentLevel for a frame or two, and
        # spawning a new one with the SAME name fatal-asserts CoreUObject
        # ("Renaming an object on top of an existing object is not
        # allowed"). Suffixing every name with #<n> sidesteps the race.
        self._spawn_seq: int = 0
        # Snapshot of actors that already existed in the CURRENT PIE world
        # at the first build_scene() call after PIE start. NEW coding-agent
        # objects are checked against these but they are never flagged.
        # Reset by on_new_pie() because PIE restart yields a fresh world.
        self._baseline_actors: Optional[List[str]] = None

    def on_new_pie(self) -> None:
        """Hook called after a fresh PIE world is entered.

        Earlier this was a no-op based on the (incorrect) assumption that
        UCV-spawned actors persisted across PIE restarts. The 9-hour
        prod30 run on 2026-04-24 disproved this: every
        ``vget_location(Building_*_sNN)`` after a PIE restart returned
        the literal string ``'error'`` because the actors had been GC'd
        with the dying PIE world. The Python-side ``_spawned_names``
        list kept growing forever (>180 stale names by epoch 19), and
        each new epoch's snapshot blocked for 1-2 minutes querying dead
        actors before timing out the whole epoch budget.

        Correct behaviour: PIE restart yields a fresh world, so we drop
        all per-PIE state — the spawned-name list, the baseline-actor
        snapshot and the ground flag. The next ``build_scene()`` will
        rebuild from scratch.
        """
        if self._spawned_names or self._baseline_actors or self._ground_placed:
            log.info(
                "on_new_pie: clearing %d stale spawned names, "
                "%d baseline actors, ground=%s",
                len(self._spawned_names),
                len(self._baseline_actors) if self._baseline_actors else 0,
                self._ground_placed,
            )
        self._spawned_names = []
        self._baseline_actors = None
        self._ground_placed = False

    def build_scene(self, spec: SceneSpec) -> "BuildReport":
        """Spawn all objects in a SceneSpec.

        Returns a :class:`BuildReport` describing what was actually placed,
        what failed, and which planned objects overlap or fall outside the
        navigable bounds. The report is fed back to the coding agent on the
        next epoch as environment feedback.

        Objects are spawned incrementally — we do NOT clear previous objects
        first. Use clear_scene() explicitly only when needed.
        """
        log.info("Building scene '%s': adding %d objects...",
                 spec.scene_id, len(spec.objects))

        report = BuildReport(requested=len(spec.objects))

        # Snapshot baseline actors on first build so later checks can
        # treat them as existing-but-not-owned-by-coding-agent.
        if self._baseline_actors is None:
            try:
                all_actors = self.ucv.vget_objects()
            except Exception as exc:
                log.warning("baseline snapshot failed: %s", exc)
                all_actors = []
            owned = set(self._spawned_names) | {self.GROUND_NAME}
            self._baseline_actors = [a for a in all_actors if a not in owned]
            log.info("Baseline actors snapshot: %d (excluded from feedback checks)",
                     len(self._baseline_actors))

        # Names of coding-agent objects already in the scene BEFORE this
        # build (i.e. survivors from earlier epochs). NEW objects are
        # checked against these, against the baseline, and against each
        # other.
        existing_owned_before = list(self._spawned_names)

        # ---- Config check (NOT a geometry guess): the play area for the
        # agent's start/goal sampling is a 20000x20000 box centered on
        # the HwaseongHaenggung map at (7661, 10970, 100). Anything the
        # LLM places outside that box cannot influence the navigation task.
        BOUNDS_CX = 7661.0
        BOUNDS_CY = 10970.0
        BOUNDS_HALF = 10000.0
        new_objs = [o for o in spec.objects if o.actor_name not in self._spawned_names]
        for o in new_objs:
            if (abs(o.x - BOUNDS_CX) > BOUNDS_HALF or
                    abs(o.y - BOUNDS_CY) > BOUNDS_HALF):
                report.off_bounds.append(o.actor_name)

        success_count = 0
        for obj in spec.objects:
            # Skip if already spawned
            if obj.actor_name in self._spawned_names:
                log.info("  %s already exists, skipping", obj.actor_name)
                continue

            bp_path = ASSET_CATALOG.get(obj.asset_key)
            if bp_path is None:
                log.warning("Unknown asset key '%s', skipping", obj.asset_key)
                report.spawn_failed.append((obj.actor_name, f"unknown asset_key '{obj.asset_key}'"))
                continue

            # Make actor name globally unique to dodge UE's async-destroy
            # name-collision fatal assert (see _spawn_seq doc).
            self._spawn_seq += 1
            unique_name = f"{obj.actor_name}_s{self._spawn_seq}"
            obj.actor_name = unique_name

            try:
                # Dispatch by asset type. Blueprint class paths end in "_C"
                # (e.g. BP_Building_01_C); StaticMesh asset paths do not.
                # SM assets must use spawn_static_mesh (fire-and-forget +
                # hard reconnect) since the BP spawn RPC rejects them.
                if bp_path.endswith("_C"):
                    self.ucv.spawn_bp_asset(
                        bp_path, obj.actor_name,
                        location=(obj.x, obj.y, obj.z),
                        rotation=(0.0, obj.yaw, 0.0),
                        collision_mode=2,  # XY-only: separate objects, no ground trace
                    )
                else:
                    self.ucv.spawn_static_mesh(
                        bp_path, obj.actor_name,
                        location=(obj.x, obj.y, obj.z),
                        rotation=(0.0, obj.yaw, 0.0),
                    )
                # Snap z to NavMesh-projected ground level so objects don't
                # float — BUT only when the LLM is implicitly placing on the
                # floor. If the spec asks for a clearly elevated position
                # (z significantly above navmesh), we treat that as an
                # intentional stack/overhang and trust the LLM. The floating
                # check below will then either confirm it's supported or flag
                # it.
                try:
                    floor_z = self._floor_z_at(obj.x, obj.y, z=obj.z)
                    if floor_z is not None:
                        if abs(obj.z - floor_z) <= self._STACK_INTENT_CM:
                            self.ucv.send(
                                f"vset /object/{obj.actor_name}/location "
                                f"{obj.x} {obj.y} {floor_z}"
                            )
                            log.info("  Snapped %s z -> navmesh floor %.0f",
                                     obj.actor_name, floor_z)
                        else:
                            log.info("  Kept %s z=%.0f (%.0f above floor %.0f, intentional stack)",
                                     obj.actor_name, obj.z,
                                     obj.z - floor_z, floor_z)
                except Exception as snap_exc:
                    log.warning("  z-snap failed for %s: %s",
                                obj.actor_name, snap_exc)
                self._spawned_names.append(obj.actor_name)
                report.spawned.append(obj.actor_name)
                success_count += 1
                log.info("  Spawned %s (%s) at (%.0f, %.0f)",
                         obj.actor_name, obj.asset_key, obj.x, obj.y)
            except Exception as exc:
                log.warning("  Failed to spawn %s: %s", obj.actor_name, exc)
                report.spawn_failed.append((obj.actor_name, str(exc)))

        log.info("Scene: %d new + %d existing = %d total objects",
                 success_count, len(self._spawned_names) - success_count,
                 len(self._spawned_names))

        # ---- Real environment feedback from UE (NOT heuristics).
        # Only check NEW objects against (a) other new objects in this
        # build and (b) previously-spawned coding-agent objects. We
        # deliberately EXCLUDE baseline_actors here: a real city map
        # contains tens of thousands of foliage/instance actors
        # (32k+ in agent_test.umap), and probing vget_bounds for each
        # would take many minutes per build round. The navmesh
        # reachability probe below already implicitly accounts for
        # baseline geometry by walking the actual collision world.
        new_names = [n for n in report.spawned
                     if n not in existing_owned_before]
        existing_neighbours = list(existing_owned_before)
        self._fill_real_overlaps(report, new_names, existing_neighbours)
        self._fill_vertical_support(report, new_names, existing_neighbours)
        self._fill_navmesh_reachability(report)

        if not report.is_clean():
            log.warning(
                "BuildReport: failures=%d off_bounds=%d overlaps=%d "
                "floating=%d unreachable=%d/%d bounds_unknown=%d",
                len(report.spawn_failed), len(report.off_bounds),
                len(report.overlapping_pairs),
                len(report.floating_in_air),
                len(report.unreachable_pairs), report.n_probes,
                len(report.bounds_unknown),
            )
        return report

    # ------------------------------------------------------------------
    # Real-feedback helpers (UE is the source of truth)
    # ------------------------------------------------------------------

    # Tolerance for "resting on the floor" (Zmin within this many cm of
    # the navmesh-projected ground). Generous to absorb sub-mesh sockets.
    _GROUND_TOL_CM = 30.0
    # Tolerance when checking whether one object's Zmin sits on another's
    # Zmax (上小下大 stack). Same scale as ground tolerance.
    _STACK_TOL_CM = 30.0
    # Margin used when deciding whether the upper object's XY footprint
    # is contained inside the supporter's footprint. AABB is conservative
    # for non-axis-aligned meshes, so we keep this loose.
    _CONTAIN_MARGIN_CM = 30.0
    # If the LLM-requested z is within this many cm of the navmesh floor,
    # we assume "meant to be on the floor" and snap. Beyond that we treat
    # it as an intentional elevated placement (e.g. a stack) and leave it.
    _STACK_INTENT_CM = 60.0

    def _query_aabbs(
        self,
        names: List[str],
        report: "BuildReport",
        record_unknown: bool,
    ) -> Dict[str, Tuple[float, float, float, float, float, float]]:
        """Pull (xmin,ymin,zmin,xmax,ymax,zmax) for each name from UE.

        Names whose query fails are added to ``report.bounds_unknown``
        only when ``record_unknown=True`` (we record for coding-agent
        objects but not for baseline actors, where unknown bounds are
        normal — e.g. cameras with no mesh).
        """
        out: Dict[str, Tuple[float, float, float, float, float, float]] = {}
        for name in names:
            if name == self.GROUND_NAME:
                continue
            box = None
            try:
                box = self.ucv.vget_bounds(name)
            except Exception as exc:
                log.warning("  bounds query failed for %s: %s", name, exc)
            if box is None:
                if record_unknown:
                    report.bounds_unknown.append(name)
                continue
            out[name] = box
        return out

    def _fill_real_overlaps(
        self,
        report: "BuildReport",
        new_names: List[str],
        existing_neighbours: List[str],
    ) -> None:
        """XY-plane AABB intersection between every NEW object and every
        already-present object (baseline + previously-spawned + earlier
        new objects in the same build). UE's ``GetActorBounds`` is the
        source of truth.

        Baseline-vs-baseline pairs are NOT checked (the initial map is a
        given). The ground plane is excluded — its AABB covers the whole
        map.
        """
        new_aabbs = self._query_aabbs(new_names, report, record_unknown=True)
        existing_aabbs = self._query_aabbs(
            existing_neighbours, report, record_unknown=False,
        )

        # New × existing.
        for n_name, (nx0, ny0, _nz0, nx1, ny1, _nz1) in new_aabbs.items():
            for e_name, (ex0, ey0, _ez0, ex1, ey1, _ez1) in existing_aabbs.items():
                dx = min(nx1, ex1) - max(nx0, ex0)
                dy = min(ny1, ey1) - max(ny0, ey0)
                if dx > 0 and dy > 0:
                    report.overlapping_pairs.append((n_name, e_name, dx * dy))

        # New × new (avoid double-counting by ordering).
        new_items = list(new_aabbs.items())
        for i, (a_name, (ax0, ay0, _az0, ax1, ay1, _az1)) in enumerate(new_items):
            for b_name, (bx0, by0, _bz0, bx1, by1, _bz1) in new_items[i + 1:]:
                dx = min(ax1, bx1) - max(ax0, bx0)
                dy = min(ay1, by1) - max(ay0, by0)
                if dx > 0 and dy > 0:
                    report.overlapping_pairs.append((a_name, b_name, dx * dy))

    def _fill_vertical_support(
        self,
        report: "BuildReport",
        new_names: List[str],
        existing_neighbours: List[str],
    ) -> None:
        """Each NEW object must have vertical support: either its AABB
        bottom is near the navmesh-projected ground at its (x,y), OR it
        sits on top of another object that fully contains its XY
        footprint ("上小下大"). Otherwise it floats.

        We use real UE bounds and the navmesh's reported floor height
        rather than assuming z=0, so this works on uneven terrain too.
        """
        new_aabbs = self._query_aabbs(new_names, report, record_unknown=False)
        existing_aabbs = self._query_aabbs(
            existing_neighbours, report, record_unknown=False,
        )

        for name, (x0, y0, z0, x1, y1, z1) in new_aabbs.items():
            cx = (x0 + x1) * 0.5
            cy = (y0 + y1) * 0.5
            cz = (z0 + z1) * 0.5

            # 1) Resting on the navmesh floor at its centre? Use the
            #    object's own height as projection origin so terrain at
            #    non-zero elevation still resolves.
            floor_z = self._floor_z_at(cx, cy, z=cz)
            if floor_z is not None and (z0 - floor_z) <= self._GROUND_TOL_CM:
                continue

            # 2) Sitting on top of a supporter that fully contains its
            #    XY footprint (上小下大)?
            supported = False
            m = self._CONTAIN_MARGIN_CM
            # Supporters can be other new objects or existing ones.
            for s_name, (sx0, sy0, _sz0, sx1, sy1, sz1) in (
                list(existing_aabbs.items()) + list(new_aabbs.items())
            ):
                if s_name == name:
                    continue
                if abs(z0 - sz1) > self._STACK_TOL_CM:
                    continue
                # Upper footprint must be (margin-)contained in lower.
                if (x0 >= sx0 - m and x1 <= sx1 + m and
                        y0 >= sy0 - m and y1 <= sy1 + m):
                    supported = True
                    break

            if supported:
                continue

            height = z0 if floor_z is None else (z0 - floor_z)
            # Distinguish "there IS a supporter near Zmin but I overhang
            # it" (上大下小) from "nothing under me at all".
            reason = "no_support"
            for s_name, (sx0, sy0, _sz0, sx1, sy1, sz1) in (
                list(existing_aabbs.items()) + list(new_aabbs.items())
            ):
                if s_name == name:
                    continue
                if abs(z0 - sz1) > self._STACK_TOL_CM:
                    continue
                # Some XY overlap with a candidate supporter → overhang.
                ix = min(x1, sx1) - max(x0, sx0)
                iy = min(y1, sy1) - max(y0, sy0)
                if ix > 0 and iy > 0:
                    reason = "overhang"
                    break
            report.floating_in_air.append((name, height, reason))

    def _floor_z_at(
        self, x: float, y: float, z: float = 0.0,
    ) -> Optional[float]:
        """Project (x, y, z) onto the navmesh and return the floor z in cm,
        or ``None`` if the projection fails. Pass the caller's own z
        (e.g. object centre or spec.z) so terrain at non-zero elevation
        still resolves — navmesh projection has a finite vertical search
        radius.
        """
        try:
            resp = self.ucv.send(
                f"vget /nav/project {x} {y} {z}"
            ).strip()
        except Exception:
            return None
        if not resp or resp.lower().startswith("error"):
            return None
        parts = resp.replace(",", " ").split()
        if len(parts) < 3:
            return None
        try:
            return float(parts[2])
        except ValueError:
            return None

    def _fill_navmesh_reachability(self, report: "BuildReport") -> None:
        """Ask UE's navigation system whether typical start↔goal pairs are
        still reachable after the scene was built. Uses
        ``vget /nav/reachable`` so the navmesh (which sees spawned
        obstacles) is the source of truth.

        We probe four diagonal/cardinal pairs spanning the play area.
        Each endpoint is first projected onto the navmesh — if either end
        doesn't lie on the mesh (e.g. play area extends past the navmesh),
        the probe is dropped rather than counted as blocked. Only when UE
        says "both endpoints valid AND no path" do we flag it.
        """
        # Probes are centered on the HwaseongHaenggung play area
        # (cx=7661, cy=10970) with a ±8000 half-extent so endpoints stay
        # well inside the ±10000 LLM placement box.
        cx, cy, h = 7661.0, 10970.0, 8000.0
        probes = [
            ((cx - h, cy - h), (cx + h, cy + h)),
            ((cx - h, cy + h), (cx + h, cy - h)),
            ((cx - h, cy    ), (cx + h, cy    )),
            ((cx,     cy - h), (cx,     cy + h)),
        ]
        report.n_probes = 0
        for (sx, sy), (gx, gy) in probes:
            sz = self._floor_z_at(sx, sy)
            gz = self._floor_z_at(gx, gy)
            if sz is None or gz is None:
                # Endpoint outside navmesh — probe is meaningless, skip.
                continue
            try:
                ok = self.ucv.nav_reachable(sx, sy, sz, gx, gy, gz)
            except Exception as exc:
                log.warning("  nav_reachable probe failed: %s", exc)
                continue
            report.n_probes += 1
            if not ok:
                report.unreachable_pairs.append(((sx, sy), (gx, gy)))

    def clear_scene(self):
        """Destroy all objects we spawned (keep the agent and engine objects)."""
        if not self._spawned_names:
            return
        log.info("Clearing %d spawned objects...", len(self._spawned_names))
        for name in self._spawned_names:
            try:
                self.ucv.send(f"vset /object/{name}/destroy")
            except Exception:
                pass
        self._spawned_names.clear()

    def remove_objects(self, names: List[str]) -> int:
        """Destroy specific objects by name. Returns count of removed objects."""
        removed = 0
        for name in names:
            if name in self._spawned_names:
                try:
                    self.ucv.send(f"vset /object/{name}/destroy")
                    self._spawned_names.remove(name)
                    removed += 1
                    log.info("  Removed %s", name)
                except Exception as exc:
                    log.warning("  Failed to remove %s: %s", name, exc)
            else:
                log.info("  %s not found in spawned objects, skipping", name)
        return removed

    def ensure_ground(self):
        """Spawn a walkable ground plane if not already present.
        Required for NavMesh to work on maps without built-in walkable geometry."""
        if self._ground_placed:
            return
        existing = self.ucv.vget_objects()
        if self.GROUND_NAME in existing:
            self._ground_placed = True
            return
        log.info("Spawning ground plane for NavMesh...")
        try:
            self.ucv.spawn_bp_asset(
                self.GROUND_MESH, self.GROUND_NAME,
                location=(0, 0, -50),  # slightly below origin
            )
            sx, sy, sz = self.GROUND_SCALE
            self.ucv.send(f"vset /object/{self.GROUND_NAME}/scale {sx} {sy} {sz}")
            self._ground_placed = True
            log.info("Ground plane placed: %s scale=(%d,%d,%d)", self.GROUND_NAME, sx, sy, sz)
        except Exception as exc:
            log.warning("Ground plane spawn failed: %s", exc)

    def get_scene_objects(self) -> List[str]:
        """Return names of currently spawned scene objects (excluding ground)."""
        return [n for n in self._spawned_names if n != self.GROUND_NAME]

    def snapshot_owned_objects(
        self, asset_keys: Dict[str, str],
    ) -> List[Dict[str, Any]]:
        """Return the current pose of every owned (coding-agent-spawned)
        object as a list of dicts suitable for ``respawn_persisted``.

        ``asset_keys`` maps actor_name -> asset_key. We need this from the
        caller because UCV's bounds query doesn't tell us the asset key
        and we don't track it server-side here.
        """
        out: List[Dict[str, Any]] = []
        dead_names: List[str] = []
        for name in self._spawned_names:
            if name == self.GROUND_NAME:
                continue
            asset_key = asset_keys.get(name, "")
            if not asset_key:
                # We can't respawn an object whose asset_key we lost.
                log.warning("snapshot: no asset_key for %s, skipping", name)
                continue
            try:
                loc = self.ucv.vget_location(name)
            except Exception as exc:
                # UE returns the literal string 'error' for actors that no
                # longer exist (PIE was restarted, actor was GC'd, etc.),
                # which raises ValueError when the wrapper tries to parse
                # floats. Treat any exception here as "actor is dead":
                # forget it on our side so we don't keep probing it for the
                # rest of the run (each probe blocks for ~600 ms).
                log.warning("snapshot: vget_location(%s) failed: %s -- pruning", name, exc)
                dead_names.append(name)
                continue
            if not loc or len(loc) != 3:
                dead_names.append(name)
                continue
            x, y, z = (float(c) for c in loc)
            yaw = 0.0
            try:
                rot = self.ucv.send(f"vget /object/{name}/rotation").strip()
                parts = rot.replace(",", " ").split()
                if len(parts) >= 2:
                    yaw = float(parts[1])  # pitch yaw roll → yaw is index 1
            except Exception:
                pass
            out.append({
                "actor_name": name,
                "asset_key": asset_key,
                "x": x, "y": y, "z": z, "yaw": yaw,
            })
        if dead_names:
            self._spawned_names = [n for n in self._spawned_names if n not in dead_names]
            log.info("snapshot: pruned %d dead actor names (now tracking %d)",
                     len(dead_names), len(self._spawned_names))
        return out

    @staticmethod
    def get_asset_catalog_prompt() -> str:
        """Format asset catalog for injection into coding agent prompt.

        Hwaseong-only catalog: every asset is a Korean palace static
        mesh shipped with the active HwaseongHaenggung map. No generic
        CityDatabase BPs are exposed because they don't visually match
        the map and were causing the LLM to repeatedly default to a
        small handful of generic city blueprints.
        """
        hw_keys = ASSET_CATEGORIES["hwaseong_buildings"]
        lines = [
            "Available assets for scene building "
            f"(Hwaseong palace map — {len(hw_keys)} items, ALL are static-mesh buildings):",
            "  " + ", ".join(hw_keys),
            "",
            "Coordinates: UE units (1m = 100 units). "
            "Play area is centered at (X=7661, Y=10970) on this map; "
            "x must be in [-2339, 17661] and y in [970, 20970]. "
            "All assets are large palace buildings (footprint ~500-2000 units). "
            "Keep objects spaced at least 800 units apart. "
            "ALL objects MUST be placed at z=100 (ground level on this map). "
            "DO NOT use any name outside the list above; CityDatabase "
            "blueprints (building_01, BP_Tree*, BP_Hydrant, etc.) are NOT "
            "available on this map and will be rejected as spawn failures. "
            "ASSET DIVERSITY: there are 28 distinct palace meshes — when "
            "placing multiple objects in one scene, use AT LEAST 3 different "
            "asset_keys (do not spam the same building everywhere).",
        ]
        return "\n".join(lines)
