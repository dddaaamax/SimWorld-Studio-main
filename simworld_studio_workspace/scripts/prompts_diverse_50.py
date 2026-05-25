"""Diverse-biome map generation prompts.

First 10 are the preview batch covering: snow village, slavic village, chinese water town,
courtyard remix, middle-east, korean palace, castle, cave-remix, landscape-remix, urban grid.

Each entry:
  name          — output umap filename (without extension)
  template      — UE /Game/... path to boot on. Use EmptyMap for from-scratch scenes.
  nav_center    — [x, y, z] UU. Center for NavMeshBoundsVolume.
  nav_scale     — [sx, sy, sz]. 100 ≈ 200m side. For T3 keep small (≤75).
  task          — per-map agent instructions (creative remix / native edit / scratch build).
"""
import textwrap


COMMON_TEMPLATE = """You are generating a training-scene .umap for a navigation agent. UE Editor is booted with a specific template ALREADY LOADED via CLI; do NOT call unreal.EditorLoadingAndSavingUtils.load_map (it crashes UE 5.3).

## Available tools (MCP)
spawn_blueprint_actor, spawn_actor, delete_actor, get_actors_in_level, find_actors_by_name, set_actor_transform, take_screenshot, execute_python_script, list_assets, setup_environment. Full catalog via `list_assets` — 22 map templates + 125 buildings + 17 allow-AI pack roots.

## Hard rules
1. Do NOT call load_map — template is already loaded on boot.
2. Do NOT spawn BP_Road1 or SM_Road* (roads look poor).
3. Do NOT touch anything under /Game/80_no_ai_maps/.
4. Save output ONLY to: __SAVE_PATH__
5. **NEVER use BP_Building IDs 1, 2, 3, 4, 5, or 6** (BP_Building_01 through BP_Building_06) — these are overused and boring. Only pick IDs 7 through 127 (skip 57 and 120 which don't exist). Vary IDs across spawns.
6. **NEVER delete actors whose name matches `Floor*`, `Ground*`, `Plane*`, `Landscape*`, `Terrain*`, `SM_Floor*`, `SM_Ground*`, `SM_Pavement*`, `SM_Sidewalk*`** — these are the walkable surface. Deleting them kills the navmesh (REACHABLE drops to 0). To "open up" the scene, delete obstacles (benches, crates, fences, planters) — NOT the floor.
7. **NEVER spawn objects at Z = ps_loc.z** — PlayerStart pivot is at character mid-height (~100 UU above ground). Objects with bottom-pivot (buildings, trees, props = almost everything) must be spawned at `ground_z = ps_loc.z - 100`. Using ps_loc.z directly makes every object float 100 UU above the surface. Always compute: `ground_z = ps_loc.z - 100` and use that as the base Z for all spawns. Then apply navmesh projection on top if available.

## Cross-pack retrieval (optional, for remix variants)
To pull assets from an allow-AI pack that isn't in spawn_blueprint_actor's shortlist:
```python
import unreal
paths = unreal.EditorAssetLibrary.list_assets("/Game/Village/", recursive=True, include_folder=False)
# Then spawn via spawn_actor(name=..., static_mesh=full_path, location=...) for SMs,
# or spawn_blueprint_actor(blueprint_id=full_path, ...) for BPs.
```

## Ground-Z snapping (CRITICAL — avoid floating objects)
Do NOT hardcode z=0 or z=200 for spawns. Use these strategies in order:

**IMPORTANT Z offset rule**: PlayerStart pivot is at character mid-height (~100 UU above the ground surface).
Objects whose pivot is at their **bottom** (most props, buildings, trees) must be spawned at `ps_loc.z - 100` (ground Z), NOT `ps_loc.z`.
```python
ground_z_base = ps_loc.z - 100  # actual ground surface Z for bottom-pivot objects
```

1. **Easiest (recommended)**: use `ground_z_base` (= `ps_loc.z - 100`) for ALL spawns.
2. **Projected to navmesh** (use AFTER nav is built): `unreal.NavigationSystemV1.project_point_to_navigation(world, unreal.Vector(x,y,ground_z_base))` returns the navmesh point at (x,y). Use its `.z`.

⚠️ **FORBIDDEN APIs — these crash the UE 5.3 Python interpreter and kill ALL subsequent execute_python_script calls (unrecoverable):**
- `unreal.SystemLibrary.line_trace_single` — DO NOT USE
- `unreal.NavigationSystemV1.get_current(world).build()` — DO NOT USE
- **Correct navmesh rebuild:** `unreal.SystemLibrary.execute_console_command(world, "RebuildNavigation")` — this is the ONLY safe way.

## Object placement by size (CRITICAL for navmesh walkability)
Spawning large buildings inside the navmesh footprint blocks navigation completely. Follow these rules:

| Object type | Approx footprint | Where to place |
|---|---|---|
| BP_Building (large, ID 7-127) | 800-2000 UU | **Outside navmesh** — at least 2000-4000 UU from PlayerStart, as background scenery |
| Trees (BP_Tree3/4), fences | 200-600 UU | **Edge of navmesh** — 800-1500 UU from PS, scattered but not blocking main paths |
| Small props (benches, crates, hydrants, bins, cones) | 50-200 UU | **Inside navmesh** — anywhere within 800 UU of PS, add visual density |

```python
# Large buildings → background ring, well outside walkable area
BIG_DIST_MIN, BIG_DIST_MAX = 2500, 5000
# Trees / medium objects → edge ring
MED_DIST_MIN, MED_DIST_MAX = 900, 1800
# Small props → freely within navmesh
SMALL_DIST_MAX = 700
```
When sampling positions for large buildings use `min_dist=1200` (collision avoidance between buildings) and do NOT require navmesh projection — they are background, not walkable.

## Recommended step ordering
Because navmesh gives you accurate Z, do them FIRST, edit AFTER, REBUILD again, save:
  Step 0: Find/create PlayerStart
  Step 1: Spawn NavMeshBoundsVolume at PS
  Step 2: RebuildNavigation (initial — so you can project spawn points to ground)
  Step 3: Wait ~20 s
  Step 4: Validate REACHABLE ≥ 10/20 (else move volume & retry once)
  **Now run your task edits** — spawn using ground_z/project_point_to_navigation, delete as needed
  Step 5: RebuildNavigation AGAIN (CRITICAL — the first navmesh doesn't know about your new obstacles; re-building here ensures the saved navmesh reflects actual walkability)
  Step 6: Wait ~20 s
  Step 7: Validate REACHABLE still ≥ 5/20 (if 0, your edits over-blocked the area)
  Step 8: Move camera above PS for overview shot
  Step 9: Overview screenshot
  Step 10: Save

## Overview screenshot camera
Before taking the final overview screenshot, position the editor camera **above PlayerStart, looking down at ~45° angle** so the user can see ground-level edits:
```python
import unreal
ues = unreal.get_editor_subsystem(unreal.UnrealEditorSubsystem)
world = ues.get_editor_world()
ps_actors = unreal.GameplayStatics.get_all_actors_of_class(world, unreal.PlayerStart)
ps = ps_actors[0].get_actor_location()
cam_loc = unreal.Vector(x=ps.x - 2500, y=ps.y, z=ps.z + 2500)  # offset 25 m back, 25 m up
# CRITICAL: Use NAMED args for Rotator — positional order in UE 5.3 Python is (roll, pitch, yaw)
# NOT (pitch, yaw, roll), so positional produces rolled cameras.
cam_rot = unreal.Rotator(pitch=-40, yaw=0, roll=0)  # pitch down ~40° looking at PS, zero roll
ues.set_level_viewport_camera_info(cam_loc, cam_rot)
print("CAMERA_SET")
```

## Edit scale & iteration (HARDCODED PIPELINE — no agent discretion)
The iteration loop is **not optional, not negotiable, and not agent-decided**. You MUST perform **EXACTLY 5 iteration rounds** (or more). Fewer rounds = PROTOCOL VIOLATION and the map will be rejected.

Each of the 5 iteration rounds MUST consist of:
  (a) A batched edit sub-round: 5–10 ops (spawn/delete/move) via ONE execute_python_script call.
  (b) A verifier call: `verify_scene(original_request="<one-sentence brief>", focus_areas="edit count, collisions, placement visibility")`.
  (c) Act on verifier output:
       - PASS: your round is complete. Move to next round with more ops.
       - NEEDS_IMPROVEMENT / FAIL: in the NEXT round's (a), specifically address each entry in `Suggestions` (e.g. "too many duplicates" → delete 3 duplicates; "objects overlap" → move 2 via set_actor_transform; "sparse area X" → spawn 2 props at X).

After all 5 rounds finish, proceed to post-edit navmesh rebuild + save. Do NOT save before round 5 even if verifier passes early — keep iterating to polish.

Other hard rules still apply:
- Minimum **25 total operations** across all rounds (ideally 30–60).
- **NO collisions**: use `is_clear(x, y, z, min_dist)` before spawn. Trees 400, buildings 800, props 200–250.
- Don't rationalize verifier feedback as "false positive". Execute the requested changes.
- Time budget ~22 min. Each round ~3-4 min (ops 1 min + verify 60s). 5 rounds ~18 min. Fits.

## Collision avoidance helper (copy into any execute_python_script that spawns)
```python
import unreal
world = unreal.get_editor_subsystem(unreal.UnrealEditorSubsystem).get_editor_world()
eas = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)

def is_clear(x, y, z, min_dist=250):
    # True if no existing level actor is within min_dist XY of (x, y)
    for a in eas.get_all_level_actors():
        try:
            l = a.get_actor_location()
            if abs(l.x) > 1e7: continue
            dx, dy = l.x - x, l.y - y
            if dx*dx + dy*dy < min_dist*min_dist:
                return False
        except Exception: pass
    return True

def ground_z(x, y, default_z):
    try:
        pt = unreal.NavigationSystemV1.project_point_to_navigation(
            world, unreal.Vector(x, y, default_z))
        if pt and hasattr(pt,'x') and (pt.x or pt.y or pt.z):
            return pt.z
    except Exception: pass
    return default_z

# Example: spawn 10 trees with collision avoidance + ground-Z snap
ps = unreal.GameplayStatics.get_all_actors_of_class(world, unreal.PlayerStart)
# PlayerStart pivot is at character mid-height (~100 UU above ground).
# Objects with bottom-pivot must spawn at ps.z - 100 (= actual ground Z).
ps_z = (ps[0].get_actor_location().z - 100) if ps else 0
placed = 0
import random; random.seed(42)
for attempt in range(100):
    if placed >= 10: break
    x = random.uniform(-3000, 3000)
    y = random.uniform(-3000, 3000)
    if not is_clear(x, y, 0, min_dist=400): continue
    z = ground_z(x, y, ps_z)
    bp = unreal.load_object(None, "/Game/CityDatabase/blueprints/BP_Tree3.BP_Tree3_C")
    a = eas.spawn_actor_from_class(bp, unreal.Vector(x, y, z))
    if a:
        a.set_actor_label(f"Tree_{placed}")
        placed += 1
print(f"placed_trees={placed}")
```
Tune `min_dist` based on object size (trees: 400, buildings: 800, props: 200).

## Your task for THIS map
__TASK__

## Mandatory finishing steps — RUN IN ORDER AT THE END

**Step 0 — Find or create PlayerStart near scene actor centroid**
Some templates place actors far from world origin (e.g., courtyard at x=2000, y=6800). If PS is at (0,0,0) while actors are elsewhere, the NavMeshBoundsVolume won't cover the playable area and REACHABLE will be 0/20.
```
import unreal
eas = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)
ues = unreal.get_editor_subsystem(unreal.UnrealEditorSubsystem)
world = ues.get_editor_world()

# Compute actor centroid first (ignore world-origin artifacts)
all_actors = eas.get_all_level_actors()
xs, ys, zs = [], [], []
for a in all_actors:
    try:
        l = a.get_actor_location()
        if abs(l.x)>1e7 or (l.x==0 and l.y==0 and l.z==0): continue
        xs.append(l.x); ys.append(l.y); zs.append(l.z)
    except Exception: pass
cx = sum(xs)/len(xs) if xs else 0.0
cy = sum(ys)/len(ys) if ys else 0.0
# Z: use median of near-ground Z values (filter out flying actors)
zs_sorted = sorted(zs)
cz = zs_sorted[len(zs_sorted)//4] if zs_sorted else 0.0   # lower-quartile Z ≈ ground
print(f"ACTOR_CENTROID=({cx:.0f},{cy:.0f},{cz:.0f})  n_actors={len(xs)}")

# Locate or place PlayerStart
ps_actors = unreal.GameplayStatics.get_all_actors_of_class(world, unreal.PlayerStart)
if ps_actors:
    ps_loc = ps_actors[0].get_actor_location()
    # Guard: if PS is at origin but centroid is far, PS is misplaced — move it.
    if abs(ps_loc.x)<10 and abs(ps_loc.y)<10 and (abs(cx)>500 or abs(cy)>500):
        ps_actors[0].set_actor_location(unreal.Vector(cx, cy, cz + 100), False, False)
        ps_loc = unreal.Vector(cx, cy, cz + 100)
        print(f"PS_RELOCATED to centroid ({ps_loc.x:.0f},{ps_loc.y:.0f},{ps_loc.z:.0f})")
    else:
        print(f"PS_EXISTING at ({ps_loc.x:.0f},{ps_loc.y:.0f},{ps_loc.z:.0f})")
else:
    ps = eas.spawn_actor_from_class(unreal.PlayerStart, unreal.Vector(cx, cy, cz + 100))
    ps_loc = ps.get_actor_location()
    print(f"PS_CREATED at ({ps_loc.x:.0f},{ps_loc.y:.0f},{ps_loc.z:.0f})")
```

**Step 1 — Add NavMeshBoundsVolume centered on PlayerStart**
Execute via execute_python_script (adjust center to ps_loc from Step 0):
```
import unreal
eas = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)
ues = unreal.get_editor_subsystem(unreal.UnrealEditorSubsystem)
world = ues.get_editor_world()
ps_actors = unreal.GameplayStatics.get_all_actors_of_class(world, unreal.PlayerStart)
center = ps_actors[0].get_actor_location() if ps_actors else unreal.Vector(__NAV_CX__, __NAV_CY__, __NAV_CZ__)
vol = eas.spawn_actor_from_class(unreal.NavMeshBoundsVolume, center)
vol.set_actor_scale3d(unreal.Vector(__NAV_SX__, __NAV_SY__, __NAV_SZ__))
print(f"NAV_VOLUME_OK at ({center.x:.0f},{center.y:.0f},{center.z:.0f})")
```

**Step 2 — Trigger nav build**
```
import unreal
world = unreal.get_editor_subsystem(unreal.UnrealEditorSubsystem).get_editor_world()
unreal.SystemLibrary.execute_console_command(world, "RebuildNavigation")
print("REBUILD_TRIGGERED")
```

**Step 3 — Wait ~20 seconds** for initial nav build.

**Step 4 — Validate navmesh (initial)**
```
import unreal
world = unreal.get_editor_subsystem(unreal.UnrealEditorSubsystem).get_editor_world()
ps = unreal.GameplayStatics.get_all_actors_of_class(world, unreal.PlayerStart)
center = ps[0].get_actor_location() if ps else unreal.Vector(__NAV_CX__, __NAV_CY__, __NAV_CZ__)
hits = 0
for i in range(20):
    try:
        pt = unreal.NavigationSystemV1.get_random_reachable_point_in_radius(world, center, 15000)
        if pt and hasattr(pt,'x') and not (pt.x==0 and pt.y==0 and pt.z==0):
            hits += 1
    except Exception:
        pass
print(f"REACHABLE_INITIAL={hits}/20")
```
If REACHABLE_INITIAL = 0/20, move the volume to a spot closer to ground-level actors and retry Steps 2-4 once.

**NOW DO 5 MANDATORY ITERATION ROUNDS — hardcoded, do not skip, do not save early**:

- **Round 1**: Bulk delete 10–15 unwanted actors (NEVER Floor/Ground/Landscape); then call `verify_scene(...)`; note suggestions.
- **Round 2**: Address Round-1 verifier suggestions + spawn 10–15 primary additions (buildings, trees, main props); call `verify_scene(...)`.
- **Round 3**: Address Round-2 verifier suggestions + spawn 5–10 more props for density; call `verify_scene(...)`.
- **Round 4**: Address Round-3 verifier suggestions; do 3–5 ops (move overlaps, delete duplicates, fill sparse areas); call `verify_scene(...)`.
- **Round 5**: Address Round-4 verifier suggestions; final polish 3–5 ops; call `verify_scene(...)`.

Report per-round op count + verifier status. Only AFTER round 5 completes do you proceed to post-edit navmesh rebuild (Step 5) and save (Step 10).

**Step 5 — Rebuild navmesh AFTER edits** (critical — otherwise saved navmesh ignores new obstacles)
```
import unreal
world = unreal.get_editor_subsystem(unreal.UnrealEditorSubsystem).get_editor_world()
unreal.SystemLibrary.execute_console_command(world, "RebuildNavigation")
print("REBUILD_POST_EDIT_TRIGGERED")
```

**Step 6 — Wait ~20 seconds** for post-edit navmesh build.

**Step 7 — Validate navmesh (post-edit)**
```
import unreal
world = unreal.get_editor_subsystem(unreal.UnrealEditorSubsystem).get_editor_world()
ps = unreal.GameplayStatics.get_all_actors_of_class(world, unreal.PlayerStart)
center = ps[0].get_actor_location() if ps else unreal.Vector(__NAV_CX__, __NAV_CY__, __NAV_CZ__)
hits = 0
for i in range(20):
    try:
        pt = unreal.NavigationSystemV1.get_random_reachable_point_in_radius(world, center, 15000)
        if pt and hasattr(pt,'x') and not (pt.x==0 and pt.y==0 and pt.z==0):
            hits += 1
    except Exception: pass
print(f"REACHABLE_FINAL={hits}/20")
```
If REACHABLE_FINAL is much lower than INITIAL, your edits over-blocked — consider deleting some of your added obstacles.

**Step 8 — Move editor camera above PlayerStart**
```
import unreal
ues = unreal.get_editor_subsystem(unreal.UnrealEditorSubsystem)
world = ues.get_editor_world()
ps_actors = unreal.GameplayStatics.get_all_actors_of_class(world, unreal.PlayerStart)
ps = ps_actors[0].get_actor_location()
ues.set_level_viewport_camera_info(
    unreal.Vector(ps.x - 2500, ps.y, ps.z + 2500),
    unreal.Rotator(pitch=-40, yaw=0, roll=0)  # MUST use named args (positional order differs in UE Python)
)
print("CAMERA_SET")
```

**Step 9 — Overview screenshot**
Call MCP tool `take_screenshot` with `filename="__NAME___overview.png"`.

**Step 10 — Save the level**
```
import unreal
world = unreal.EditorLevelLibrary.get_editor_world()
ok = unreal.EditorLoadingAndSavingUtils.save_map(world, "__SAVE_PATH__")
print(f"SAVE_OK={ok}")
```

## Final report (≤120 words)
State concisely: what edits you made, REACHABLE result, SAVE_OK result, any errors verbatim.
"""


PROMPTS = [
    # ------- Block A: T1 (easy) -------
    {
        "name": "map_01_wintertown_native",
        "template": "/Game/WinterTown/Maps/RussianWinterTownDemo01",
        "nav_center": [0, 0, 0],
        "nav_scale": [100, 100, 15],
        "task": (
            "You're in a Russian winter village (snow, wooden houses, street lamps). "
            "GOAL: open up walking lanes between buildings. "
            "Use `find_actors_by_name` to locate 5-10 actors matching SM_Bench*, SM_Fence*, or SM_Crate* and "
            "delete them. Then spawn 4 BP_Tree3 or BP_Tree5 at random positions within ±3000 UU of origin "
            "(e.g. [1200,800,0], [-1500,600,0], [800,-1400,0], [-900,-900,0]) to reinforce the snow atmosphere. "
            "Keep the scene feeling like a winter village."
        ),
    },
    {
        "name": "map_02_village_day",
        "template": "/Game/Village/Maps/Village",
        "nav_center": [0, 0, 0],
        "nav_scale": [100, 100, 15],
        "task": (
            "Slavic village by day. GOAL: make the main street navigable. "
            "Find 5-8 actors with SM_Fence*, SM_HayBale*, or SM_Crate* patterns and delete them. "
            "Spawn 3 BP_Scooter_0[1-4] and 2 BP_Cart (mix) in the cleared area. "
            "Add 2 BP_Tree4 near house corners for variety."
        ),
    },
    {
        "name": "map_03_watertown_remix",
        "template": "/Game/ChineseWaterTown/Ver1/Map/DemoMap",
        "nav_center": [0, 0, 0],
        "nav_scale": [100, 100, 15],
        "task": (
            "Chinese water town with canals. REMIX: add urban density. "
            "Spawn 4 CityDatabase buildings (e.g. BP_Building_58, BP_Building_18, BP_Building_42, BP_Building_73) "
            "at distinct positions within ±2500 UU of origin, spread them out so they don't overlap the canal. "
            "Add 3 BP_Hydrant and 2 BP_Trash_bin_a for street-furniture detail near the new buildings."
        ),
    },
    {
        "name": "map_04_courtyard_remix",
        "template": "/Game/ModularCourtyard/Maps/SampleScene_sanny",
        "nav_center": [0, 0, 0],
        "nav_scale": [75, 75, 10],
        "task": (
            "Sunny modular courtyard (plaza). REMIX: turn it into a busy market scene. "
            "Spawn 4 BP_Table and 3 BP_Trash_bin_a spread across the plaza. "
            "Add 2 BP_Scooter_02 and 1 BP_Cart2. "
            "Finally spawn 3 BP_Tree2 at the plaza edges for greenery."
        ),
    },
    {
        "name": "map_05_middleeast_native",
        "template": "/Game/MiddleEast/Maps/MiddleEast",
        "nav_center": [0, 0, 0],
        "nav_scale": [100, 100, 15],
        "task": (
            "Middle-eastern village scene. GOAL: clear some pathways + add props. "
            "Delete 4-6 SM_Crate* or SM_Barrel* actors if present (use find_actors_by_name). "
            "Spawn 3 BP_Trash_can, 2 BP_Hydrant, and 2 BP_Tree1 scattered within ±2500 UU of origin."
        ),
    },
    {
        "name": "map_06_hwaseong_native",
        "template": "/Game/HwaseongHaenggung/Maps/Demo",
        "nav_center": [0, 0, 0],
        "nav_scale": [100, 100, 15],
        "task": (
            "Korean traditional palace. GOAL: subtle native edit — do NOT spawn modern props here "
            "(it's a historical site). Optionally delete 2-4 SM_Fence* or SM_Barrier* actors to open pathways. "
            "You may spawn 3 BP_Tree1 or BP_Tree4 (trees are period-appropriate) in courtyard edges."
        ),
    },
    # ------- Block A: T2 (medium — find a cluster center) -------
    {
        "name": "map_07_castleriver_remix",
        "template": "/Game/CastleRiver/Maps/Demonstration",
        "nav_center": [0, 0, 0],
        "nav_scale": [100, 100, 20],
        "task": (
            "Medieval castle by a river. REMIX: drop winter-town assets for a 'besieged castle' feel. "
            "First, survey actors via `get_actors_in_level` to find the castle's centroid — "
            "if the castle is far from origin, compute a better nav_center (report it, the orchestrator "
            "uses the one in the prompt). "
            "Spawn 3-5 trees (BP_Tree2 or BP_Tree6) around the castle base. "
            "Try retrieving from `/Game/WinterTown/` via `unreal.EditorAssetLibrary.list_assets(..., recursive=True)`; "
            "if you find any SM_Barrel_* or SM_Crate_* static mesh paths, spawn 3 of them as 'supply crates'."
        ),
    },
    # ------- Block A: T3 (hard — needs careful nav) -------
    {
        "name": "map_08_cave_remix",
        "template": "/Game/Cave/Maps/Demonstration",
        "nav_center": [0, 0, 0],
        "nav_scale": [60, 60, 10],
        "task": (
            "Fantasy cave interior. REMIX: make it a 'hidden camp' by placing village/urban props inside. "
            "IMPORTANT: the cave is 3D — first call `get_actors_in_level` and identify an approximate "
            "walkable floor location. Spawn at that Z level (not at Z=0 if floor is higher). "
            "Spawn 2 BP_Table, 3 BP_Trash_can (as 'barrels'), and 2 BP_Tree1 "
            "to suggest inhabitants. Spread across ~800 UU radius. "
            "The NavMeshBoundsVolume is pre-configured at a small 120m × 120m area — "
            "trust it unless REACHABLE=0/20 after build, in which case re-position "
            "near the spawned objects."
        ),
    },
    {
        "name": "map_09_landscape_remix",
        "template": "/Game/Chinese_Landscape/Levels/Chinese_Landscape_Demo",
        "nav_center": [0, 0, 500],
        "nav_scale": [50, 50, 10],
        "task": (
            "Chinese landscape (mountains + grassland — huge world). REMIX: drop a small village into "
            "a grassy area near origin. Critically: use a SMALL nav volume so Recast doesn't try to tile "
            "200 km of mountains. "
            "Spawn 4 CityDatabase buildings (BP_Building_58, _12, _25, _38) in a tight cluster within "
            "±1500 UU of origin (e.g. at [1000,0,500], [-1000,0,500], [0,1200,500], [0,-1200,500]). "
            "Add 3 BP_Tree3 between the buildings. "
            "The nav volume is ONLY 100m × 100m — do not spawn anything beyond ±2000 UU of origin or "
            "it'll be outside the navmesh."
        ),
    },
    # ------- Backfill for failed T3 ↓ -------
    {
        "name": "map_09b_wintertown_demo02",
        "template": "/Game/WinterTown/Maps/RussianWinterTownDemo02",
        "nav_center": [0, 0, 0],
        "nav_scale": [100, 100, 15],
        "task": (
            "Russian winter village, second demo variant (Demo02 — different layout from Demo01). "
            "GOAL: open up walkable area and give it a little cross-pack flavor. "
            "Use `find_actors_by_name` to locate 4-8 actors with SM_Fence*, SM_Snowdrift*, or SM_Pile* "
            "patterns and delete them. "
            "Spawn 3 BP_Building_58 or BP_Building_12 (from CityDatabase) near the edges at "
            "~[2500, 2000, 0], [-2500, 1500, 0], [0, -2500, 0] to suggest a 'growing town' narrative. "
            "Add 2 BP_Tree5 for snow-pine flavor."
        ),
    },
    # ------- Block B: from-scratch on EmptyMap -------
    {
        "name": "map_10_B1_urban_grid",
        "template": "/Game/Maps/EmptyMap",
        "nav_center": [0, 0, 0],
        "nav_scale": [100, 100, 10],
        "task": (
            "EMPTY MAP — build from scratch. Goal: small urban block with navigable streets. "
            "First call `setup_environment(time_of_day='afternoon', ground_size=200)` to get sky + "
            "ground + lighting. "
            "Then spawn 6 buildings in a 2-row grid: "
            "  BP_Building_33 at [2000, 1500, 0], BP_Building_18 at [2000, 0, 0], BP_Building_42 at [2000, -1500, 0], "
            "  BP_Building_55 at [-2000, 1500, 0], BP_Building_27 at [-2000, 0, 0], BP_Building_71 at [-2000, -1500, 0]. "
            "Add 4 BP_Tree3 or BP_Tree5 between the rows: [0, 1000, 0], [0, -1000, 0], [500, 2500, 0], [-500, -2500, 0]. "
            "Add 3 BP_Hydrant and 2 BP_Trash_bin_a for detail. "
            "A pedestrian-navigable grid with trees and props. No roads."
        ),
    },

    # =========================================================================
    # Batch 2 (40 more maps: 11-50). Covers:
    #   - second variants for already-touched templates (native vs remix flip)
    #   - untouched T1 templates × 2
    #   - T2 templates × 2
    #   - 4 T3 attempts (Dungeon, Lighthouse) with tiny nav volumes
    #   - 6 more Block B scratch scenes
    # Chinese_Landscape skipped (timed out). ModularSciFi skipped (risky).
    # =========================================================================

    # --- Second variants for already-touched T1 templates ---
    {
        "name": "map_11_wintertown_demo01_remix",
        "template": "/Game/WinterTown/Maps/RussianWinterTownDemo01",
        "nav_center": [0, 0, 0],
        "nav_scale": [100, 100, 15],
        "task": (
            "Russian winter town, second variant (cross-pack remix). Spawn 3 CityDatabase buildings "
            "(BP_Building_23, BP_Building_09, BP_Building_25) near the scene edges at "
            "[2200, 1500, 0], [-2200, 1500, 0], [0, -2500, 0] — they'll look like 'modern buildings "
            "invading the old town'. Add 2 BP_Hydrant and 2 BP_Trash_bin_a near the new buildings."
        ),
    },
    {
        "name": "map_12_village_day_remix",
        "template": "/Game/Village/Maps/Village",
        "nav_center": [0, 0, 0],
        "nav_scale": [100, 100, 15],
        "task": (
            "Slavic village (day) — REMIX with urban density. Spawn 4 CityDatabase buildings "
            "(BP_Building_08, _15, _32, _61) at the village outskirts: [2500, 0, 0], [-2500, 0, 0], "
            "[0, 2500, 0], [0, -2500, 0]. Add 3 BP_Scooter_01/02/03 scattered in the village center "
            "for 'modern life' feel."
        ),
    },
    {
        "name": "map_13_watertown_native",
        "template": "/Game/ChineseWaterTown/Ver1/Map/DemoMap",
        "nav_center": [0, 0, 0],
        "nav_scale": [100, 100, 15],
        "task": (
            "Chinese water town, NATIVE edit. Use `find_actors_by_name` to find 5-8 actors matching "
            "SM_Boat*, SM_Lantern*, SM_Crate*, or SM_Basket* patterns and delete 4-6 of them to open walkways. "
            "Do NOT spawn foreign assets — keep the authentic water-town feel. Spawn 3 BP_Tree2 "
            "at canal edges for greenery."
        ),
    },
    {
        "name": "map_14_courtyard_sunny_native",
        "template": "/Game/ModularCourtyard/Maps/SampleScene_sanny",
        "nav_center": [0, 0, 0],
        "nav_scale": [75, 75, 10],
        "task": (
            "Sunny courtyard, NATIVE edit. Delete 3-5 actors matching SM_Planter*, SM_Bench*, or SM_Crate*. "
            "Then spawn 2 BP_Tree4 and 2 BP_Hydrant for subtle decoration. Keep the clean plaza aesthetic."
        ),
    },
    {
        "name": "map_15_middleeast_remix",
        "template": "/Game/MiddleEast/Maps/MiddleEast",
        "nav_center": [0, 0, 0],
        "nav_scale": [100, 100, 15],
        "task": (
            "Middle-eastern village — cross-pack REMIX. Spawn 3 CityDatabase buildings "
            "(BP_Building_47, _37, _88) at periphery: [2500, 1500, 0], [-2500, 1000, 0], [0, -2500, 0]. "
            "Add 4 BP_Tree1 for desert-edge greenery. Add 2 BP_Cart for trade-caravan flavor."
        ),
    },
    {
        "name": "map_16_hwaseong_remix",
        "template": "/Game/HwaseongHaenggung/Maps/Demo",
        "nav_center": [0, 0, 0],
        "nav_scale": [100, 100, 15],
        "task": (
            "Korean palace — subtle REMIX. Spawn 5 BP_Tree4 or BP_Tree6 (period-appropriate) at palace-courtyard "
            "edges: [1800, 1500, 0], [-1800, 1500, 0], [1800, -1500, 0], [-1800, -1500, 0], [0, 2500, 0]. "
            "Optionally add 2 BP_Table from CityDatabase as 'outdoor ceremonial tables'. Keep historical feel."
        ),
    },
    {
        "name": "map_17_castleriver_native",
        "template": "/Game/CastleRiver/Maps/Demonstration",
        "nav_center": [0, 0, 0],
        "nav_scale": [100, 100, 20],
        "task": (
            "Medieval castle by river — NATIVE edit. Survey actors; find 4-6 matching SM_Barrel*, "
            "SM_Crate*, SM_Fence*, or SM_Haybale* patterns and delete them to open castle courtyards. "
            "Spawn 3 BP_Tree2 near the river edge. Do NOT add foreign assets — keep period integrity."
        ),
    },
    {
        "name": "map_18_cave_native",
        "template": "/Game/Cave/Maps/Demonstration",
        "nav_center": [0, 0, 0],
        "nav_scale": [60, 60, 10],
        "task": (
            "Fantasy cave — NATIVE subtle edit. First call get_actors_in_level; find any SM_Rock_Small*, "
            "SM_Stalagmite*, or SM_Debris* and delete 3-5 to clear a small path. Do NOT add foreign "
            "assets — keep the cave moody and empty. Spawn 1 BP_Trash_can at the path terminus "
            "(looks like an old barrel left behind)."
        ),
    },
    {
        "name": "map_19_wintertown_demo02_native",
        "template": "/Game/WinterTown/Maps/RussianWinterTownDemo02",
        "nav_center": [0, 0, 0],
        "nav_scale": [100, 100, 15],
        "task": (
            "Russian winter town Demo02 — NATIVE edit. Find 5-10 SM_Fence*, SM_Bench*, or SM_Snowdrift* actors "
            "and delete them to carve wide walking paths. Spawn 4 BP_Tree3 or BP_Tree5 at cleared spots "
            "for winter-pine flavor."
        ),
    },

    # --- Untouched T1 templates × 2 variants each ---
    {
        "name": "map_20_trainstation_native",
        "template": "/Game/TrainStation/Maps/Demonstration",
        "nav_center": [0, 0, 0],
        "nav_scale": [100, 100, 20],
        "task": (
            "Victorian train station — NATIVE edit. Find 4-6 actors matching SM_Crate*, SM_Barrel*, "
            "SM_Luggage*, or SM_Bench* and delete them to open the platform. Spawn 3 BP_Trash_bin_a "
            "as period-fitting metal bins. Keep steampunk feel."
        ),
    },
    {
        "name": "map_21_hwaseong_remix2",
        "template": "/Game/HwaseongFortress/Maps/Demonstration",
        "nav_center": [0, 0, 0],
        "nav_scale": [100, 100, 20],
        "task": (
            "Hwaseong Fortress second remix — add a bustling market district. Spawn 4 BP_Building_33 "
            "and BP_Building_47 clustered near center, 6 BP_Tree4 along fortress walls, "
            "3 BP_Hydrant and 4 trash bins at courtyard corners."
        ),
    },
    {
        "name": "map_22_containeryard_demo_native",
        "template": "/Game/ContainerYard/Maps/Demonstration",
        "nav_center": [0, 0, 0],
        "nav_scale": [100, 100, 20],
        "task": (
            "Shipping container yard — NATIVE edit. Find 5-8 actors matching SM_Container_Small*, "
            "SM_Crate_B*, or SM_Pallet* and delete them to create forklift lanes. Do NOT add non-industrial "
            "assets. Add 2 BP_RoadBlocker (which is a concrete barrier) at yard edges."
        ),
    },
    {
        "name": "map_23_containeryard_demo_remix",
        "template": "/Game/ContainerYard/Maps/Demonstration",
        "nav_center": [0, 0, 0],
        "nav_scale": [100, 100, 20],
        "task": (
            "Container yard — REMIX into a makeshift settlement. Spawn 3 BP_Building_91 or BP_Building_29 "
            "(squatter housing) at [1800, 1500, 0], [-1800, 1500, 0], [0, -2000, 0]. "
            "Add 5 BP_Tree1 (scrub-like) and 3 BP_Trash_can scattered to suggest habitation."
        ),
    },
    {
        "name": "map_24_containeryard_day_native",
        "template": "/Game/ContainerYard/Maps/Demonstration_Day",
        "nav_center": [0, 0, 0],
        "nav_scale": [100, 100, 20],
        "task": (
            "Container yard (daytime variant) — NATIVE edit. Find 4-6 SM_Container_* or SM_Pallet* and delete "
            "to open lanes. Spawn 2 BP_Cart and 2 BP_Scooter_03 as 'dock workers' transport."
        ),
    },
    {
        "name": "map_25_containeryard_day_remix",
        "template": "/Game/ContainerYard/Maps/Demonstration_Day",
        "nav_center": [0, 0, 0],
        "nav_scale": [100, 100, 20],
        "task": (
            "Container yard (day) — REMIX with a small food-court strip. Spawn 4 BP_Table, "
            "3 BP_Hydrant, 2 BP_Trash_can, and 3 BP_Tree2 clustered at [1500, 0, 0] forming a break area. "
            "Add 1 BP_Building_11 (cafeteria) at [2500, 0, 0]."
        ),
    },
    {
        "name": "map_26_courtyard_overcast_native",
        "template": "/Game/ModularCourtyard/Maps/SampleScene_overcast",
        "nav_center": [0, 0, 0],
        "nav_scale": [75, 75, 10],
        "task": (
            "Overcast courtyard — NATIVE edit. Delete 3-5 SM_Planter*, SM_Bench*, or SM_Sculpture* "
            "actors. Spawn 3 BP_Tree4 and 2 BP_Hydrant. Keep muted, somber mood."
        ),
    },
    {
        "name": "map_27_courtyard_overcast_remix",
        "template": "/Game/ModularCourtyard/Maps/SampleScene_overcast",
        "nav_center": [0, 0, 0],
        "nav_scale": [75, 75, 10],
        "task": (
            "Overcast courtyard — REMIX into an abandoned lot. Spawn 4 BP_Trash_can, 3 BP_RoadCone, "
            "2 BP_RoadBlocker, and 2 BP_Rabbish scattered across the plaza. Add 1 BP_Scooter_04 (broken-looking) "
            "at [500, 0, 0]. Gritty, derelict feel."
        ),
    },
    {
        "name": "map_28_village_night_native",
        "template": "/Game/Village/Maps/Village_SummerNightExample",
        "nav_center": [0, 0, 0],
        "nav_scale": [100, 100, 15],
        "task": (
            "Slavic village at summer night — NATIVE edit. Find 5-8 SM_Fence*, SM_HayBale*, SM_Barrel* "
            "actors and delete them. Spawn 3 BP_Tree2 and 2 BP_Tree6 near house corners. Keep nighttime mood."
        ),
    },
    {
        "name": "map_29_village_night_remix",
        "template": "/Game/Village/Maps/Village_SummerNightExample",
        "nav_center": [0, 0, 0],
        "nav_scale": [100, 100, 15],
        "task": (
            "Slavic village night — REMIX with a late-night market. Spawn 3 BP_Table, "
            "4 BP_Trash_bin_a, 2 BP_Cart, and 3 BP_Soda1/2/3 (bottles as 'merchant goods') "
            "clustered at [1000, 0, 0]. Add 2 CityDatabase buildings (BP_Building_12, _77) at village edge."
        ),
    },

    # --- T2 templates (need careful cluster-center) ---
    {
        "name": "map_30_gothic_day_native",
        "template": "/Game/ModularGothicFantasyEnvironment/Maps/DemoMapDay",
        "nav_center": [0, 0, 0],
        "nav_scale": [100, 100, 20],
        "task": (
            "Gothic fantasy environment (day) — NATIVE edit. Survey for SM_Gravestone*, SM_Pillar_Broken*, "
            "SM_Debris*, SM_Chain* actors; delete 4-6 of them to open cathedral-plaza space. "
            "Spawn 3 BP_Tree2 or BP_Tree6 at plaza edges. Maintain gothic atmosphere."
        ),
    },
    {
        "name": "map_31_gothic_day_remix",
        "template": "/Game/ModularGothicFantasyEnvironment/Maps/DemoMapDay",
        "nav_center": [0, 0, 0],
        "nav_scale": [100, 100, 20],
        "task": (
            "Gothic fantasy (day) — REMIX with modern intrusion. Spawn 2 BP_Scooter_01, 1 BP_Cart, "
            "3 BP_Trash_can (rusty metal 'barrels'), and 2 BP_Hydrant near the plaza center. "
            "Suggests 'abandoned gothic quarter being gentrified'."
        ),
    },
    {
        "name": "map_32_gothic_night_native",
        "template": "/Game/ModularGothicFantasyEnvironment/Maps/DemoMapNight",
        "nav_center": [0, 0, 0],
        "nav_scale": [100, 100, 20],
        "task": (
            "Gothic fantasy (night) — NATIVE edit. Find 4-6 SM_Crate*, SM_Chain*, or SM_Debris* and delete. "
            "Spawn 3 BP_Tree6 at cathedral periphery. Keep dark atmospheric tone. "
            "Do NOT add bright modern props — only period-appropriate additions."
        ),
    },
    {
        "name": "map_33_gothic_night_remix",
        "template": "/Game/ModularGothicFantasyEnvironment/Maps/DemoMapNight",
        "nav_center": [0, 0, 0],
        "nav_scale": [100, 100, 20],
        "task": (
            "Gothic fantasy night — REMIX into a 'cultist hideout'. Spawn 4 BP_Table, "
            "3 BP_Couch (as gothic couches), 2 BP_Trash_bin_b, and 2 BP_Tree6 clustered at [1000, 500, 0]. "
            "Adds an occupied feel to the ruins."
        ),
    },
    {
        "name": "map_34_temple_plaza_native",
        "template": "/Game/ModularTemplePlaza/Maps/ConceptMap",
        "nav_center": [0, 0, 0],
        "nav_scale": [100, 100, 20],
        "task": (
            "Ancient temple plaza — NATIVE edit. Find 4-6 SM_Debris*, SM_Brazier*, SM_Offering*, "
            "or SM_StatueSmall* actors and delete to open ceremonial space. "
            "Spawn 3 BP_Tree2 at plaza edges. Period-appropriate only."
        ),
    },
    {
        "name": "map_35_temple_plaza_remix",
        "template": "/Game/ModularTemplePlaza/Maps/ConceptMap",
        "nav_center": [0, 0, 0],
        "nav_scale": [100, 100, 20],
        "task": (
            "Temple plaza — REMIX into 'archaeological dig site'. Spawn 3 BP_Table "
            "(field workstations), 4 BP_Trash_can (excavation barrels), 2 BP_RoadCone (marker stakes), "
            "and 3 BP_Tree4 near plaza edges. Adds modern research presence in ancient ruins."
        ),
    },

    # --- T3 attempts (tiny nav volumes; may fail — that's ok) ---
    {
        "name": "map_36_dungeon_native",
        "template": "/Game/Dungeon/Levels/Dungeon_Demo_00",
        "nav_center": [0, 0, 0],
        "nav_scale": [50, 50, 10],
        "task": (
            "Fantasy dungeon interior — NATIVE edit (3D — survey first to find floor Z). "
            "Delete 3-5 SM_Rubble*, SM_Barrel*, SM_Chain*, or SM_Torch_Broken* to clear a path. "
            "Spawn 2 BP_Trash_can (as 'barrels') at floor level. Use small (100m) nav volume — adjust "
            "center if floor is not at Z=0."
        ),
    },
    {
        "name": "map_37_dungeon_remix",
        "template": "/Game/Dungeon/Levels/Dungeon_Demo_00",
        "nav_center": [0, 0, 0],
        "nav_scale": [50, 50, 10],
        "task": (
            "Dungeon — REMIX as 'forgotten library'. Spawn 4 BP_Table "
            "at floor level (~Z=0 or wherever floor is, which you should determine via "
            "get_actors_in_level first). Add 3 BP_Couch and 2 BP_Tree1 "
            "(dried-out potted plants) for inhabited look. Adjust nav center to cluster."
        ),
    },
    {
        "name": "map_38_lighthouse_native",
        "template": "/Game/Lighthouse_Island/Levels/Lighthouse_Demo_00",
        "nav_center": [0, 0, 0],
        "nav_scale": [60, 60, 15],
        "task": (
            "Lighthouse island — NATIVE edit. Survey to find the lighthouse base location. "
            "Delete 3-5 SM_Rock*, SM_Driftwood*, or SM_Net* actors near the lighthouse to open a path. "
            "Spawn 2 BP_Tree4 near the lighthouse base. Use tight nav volume (120m) at lighthouse base. "
            "If REACHABLE=0/20, move volume center to where lighthouse actor is."
        ),
    },
    {
        "name": "map_39_lighthouse_remix",
        "template": "/Game/Lighthouse_Island/Levels/Lighthouse_Demo_00",
        "nav_center": [0, 0, 0],
        "nav_scale": [60, 60, 15],
        "task": (
            "Lighthouse island — REMIX as 'fishing village setup'. Spawn 3 BP_Building_14 or BP_Building_22 "
            "clustered near the lighthouse (survey first for lighthouse coords — move volume center if "
            "needed). Add 4 BP_Trash_can (crab pots), 2 BP_Cart, and 3 BP_Tree1 nearby."
        ),
    },

    # --- More Block B: from-scratch scenes on EmptyMap ---
    {
        "name": "map_40_B2_open_plaza",
        "template": "/Game/Maps/EmptyMap",
        "nav_center": [0, 0, 0],
        "nav_scale": [100, 100, 10],
        "task": (
            "EMPTY MAP scratch build: open public plaza. "
            "First `setup_environment(time_of_day='noon', ground_size=200)`. "
            "Spawn 4 BP_Tree3 at [1500, 0, 0], [-1500, 0, 0], [0, 1500, 0], [0, -1500, 0]. "
            "Add 4 BP_Table at [800, 800, 0], [-800, 800, 0], [800, -800, 0], [-800, -800, 0]. "
            "Add 4 BP_Trash_bin_a spread, 2 BP_Couch at [0, 500, 0] and [0, -500, 0], "
            "and 1 BP_Hydrant at each cardinal. Sparse, navigable plaza."
        ),
    },
    {
        "name": "map_41_B3_village_lane",
        "template": "/Game/Maps/EmptyMap",
        "nav_center": [0, 0, 0],
        "nav_scale": [100, 100, 10],
        "task": (
            "EMPTY MAP scratch build: rural village lane. "
            "Call `setup_environment(time_of_day='sunset', ground_size=200)` first. "
            "Spawn 4 CityDatabase buildings in a loose row: BP_Building_23 at [-2500, 800, 0], "
            "BP_Building_08 at [-500, 800, 0], BP_Building_15 at [1500, 800, 0], BP_Building_19 at [3500, 800, 0]. "
            "Add 5 BP_Tree2 and BP_Tree4 along the opposite side at Y=-1000. "
            "Add 2 BP_Cart and 1 BP_Scooter_03 as village vehicles. Add 3 BP_Hydrant."
        ),
    },
    {
        "name": "map_42_B4_industrial_yard",
        "template": "/Game/Maps/EmptyMap",
        "nav_center": [0, 0, 0],
        "nav_scale": [100, 100, 10],
        "task": (
            "EMPTY MAP scratch build: industrial yard / junkyard. "
            "Call `setup_environment(time_of_day='afternoon', ground_size=200)`. "
            "Spawn lots of boxes and cones as clutter: 5 BP_Box, 4 BP_Box2, 3 BP_Box3, 4 BP_Can, "
            "5 BP_RoadCone, 3 BP_RoadBlocker, 2 BP_Trash_bin_a, 2 BP_Trash_bin_b. "
            "Scatter them across ±3500 UU forming obstacle clusters. Add 2 BP_Cart and 1 BP_Cart2. "
            "No buildings — a flat industrial yard."
        ),
    },
    {
        "name": "map_43_B5_residential_courtyard",
        "template": "/Game/Maps/EmptyMap",
        "nav_center": [0, 0, 0],
        "nav_scale": [100, 100, 10],
        "task": (
            "EMPTY MAP scratch build: residential courtyard. "
            "`setup_environment(time_of_day='morning', ground_size=200)`. "
            "Spawn 4 small houses around a central green: BP_Building_17 at [2000, 2000, 0], "
            "BP_Building_33 at [-2000, 2000, 0], BP_Building_58 at [2000, -2000, 0], "
            "BP_Building_91 at [-2000, -2000, 0]. "
            "In the center: 4 BP_Tree5 at [±800, ±800, 0]; a central BP_Couch at [0,0,0]; "
            "2 BP_Trash_bin_b; 2 BP_Hydrant. Quiet neighborhood."
        ),
    },
    {
        "name": "map_44_B6_mixed_downtown",
        "template": "/Game/Maps/EmptyMap",
        "nav_center": [0, 0, 0],
        "nav_scale": [100, 100, 10],
        "task": (
            "EMPTY MAP scratch: dense mixed downtown block. "
            "`setup_environment(time_of_day='afternoon', ground_size=200)`. "
            "Spawn 8 buildings with varied IDs (BP_Building_09, _24, _36, _48, _62, _79, _95, _110) "
            "in a ~300 m × 300 m cluster — vary positions ±4000 UU avoiding origin (keep center as "
            "'main square'). In the main square: 3 BP_Tree3, 4 BP_Hydrant, 2 BP_Scooter_01, "
            "3 BP_Trash_bin_a, 2 BP_Cart. Busy urban feel."
        ),
    },
    {
        "name": "map_45_B7_park_with_paths",
        "template": "/Game/Maps/EmptyMap",
        "nav_center": [0, 0, 0],
        "nav_scale": [100, 100, 10],
        "task": (
            "EMPTY MAP scratch: urban park. "
            "`setup_environment(time_of_day='morning', ground_size=200)`. "
            "Tree-heavy: spawn 10 BP_Tree (mix IDs 1-6) scattered across ±4000 UU. "
            "Add 6 BP_Couch as 'park benches' evenly distributed. "
            "Add 4 BP_Trash_bin_a, 2 BP_Hydrant, 2 BP_Table (picnic). "
            "Border with 2 BP_Building_47 at [-4500, 0, 0] and BP_Building_71 at [4500, 0, 0]."
        ),
    },
    {
        "name": "map_46_B8_dense_urban",
        "template": "/Game/Maps/EmptyMap",
        "nav_center": [0, 0, 0],
        "nav_scale": [100, 100, 10],
        "task": (
            "EMPTY MAP scratch: very dense urban intersection. "
            "`setup_environment(time_of_day='noon', ground_size=200)`. "
            "Spawn 10 buildings tightly packed: IDs 05, 11, 18, 27, 33, 44, 58, 71, 82, 99 — "
            "at positions ±2500/±4500 UU forming two intersecting 'streets'. "
            "Add 4 BP_Scooter and 3 BP_Cart in the main intersection. "
            "Scatter 5 BP_Trash_bin_a, 4 BP_Hydrant, 3 BP_RoadCone. No trees (dense city)."
        ),
    },

    # --- 4 extras: re-try safe T1 templates with a distinct third variant ---
    {
        "name": "map_47_wintertown_demo01_minimal",
        "template": "/Game/WinterTown/Maps/RussianWinterTownDemo01",
        "nav_center": [0, 0, 0],
        "nav_scale": [100, 100, 15],
        "task": (
            "Winter village — MINIMAL native edit. Delete only 2-3 SM_Bench or SM_Fence "
            "actors. Spawn just 1 BP_Tree3 at [500, 500, 0]. Goal: preserve scene integrity, "
            "minimal intervention."
        ),
    },
    {
        "name": "map_48_hwaseong_cleared",
        "template": "/Game/HwaseongHaenggung/Maps/Demo",
        "nav_center": [0, 0, 0],
        "nav_scale": [100, 100, 15],
        "task": (
            "Korean palace — CLEARED variant. Focus on deleting crowds/props: find 6-10 SM_Person*, "
            "SM_Barrier*, SM_Banner*, or SM_Fence* actors and delete them to fully open palace grounds. "
            "Spawn 0 new props. Clean empty historical-site feel."
        ),
    },
    {
        "name": "map_49_courtyard_sunny_packed",
        "template": "/Game/ModularCourtyard/Maps/SampleScene_sanny",
        "nav_center": [0, 0, 0],
        "nav_scale": [75, 75, 10],
        "task": (
            "Sunny courtyard — PACKED variant. Do NOT delete anything. Spawn lots of props: "
            "6 BP_Table in a 3x2 grid at [±700, ±600, 0] and [0, ±900, 0]. "
            "Add 6 BP_Couch evenly spread. 4 BP_Tree2, 3 BP_Hydrant, 4 BP_Trash_bin_a. "
            "Crowded outdoor event feel."
        ),
    },
    # =========================================================================
    # Batch 3 (10 NEW maps: 51-60). Improvements over batch 2:
    #   - auto PlayerStart find/create (from Step 0) + use PS.z for all spawns
    #   - navmesh built FIRST so spawns can use project_point_to_navigation
    #   - camera moved to 45° aerial over PS before overview screenshot
    #   - bigger visible edits (spawn 10-20+ items, not just 3-5)
    #   - previously-missed variants: CyberVillage, Sanny, Overcast
    # =========================================================================
    {
        "name": "map_51_village_night_big_native",
        "template": "/Game/Village/Maps/Village_SummerNightExample",
        "nav_center": [0, 0, 100],
        "nav_scale": [120, 120, 20],
        "task": (
            "Slavic village at summer night — LARGE-SCALE native edit. "
            "Use find_actors_by_name to locate 15-20 actors matching SM_Fence*, SM_HayBale*, SM_Barrel*, "
            "SM_Crate*, SM_Pallet*, or SM_Cart* and delete them — dramatically open the village square. "
            "Then spawn a visible cluster: 8 BP_Tree4 + 4 BP_Tree6 scattered (min_dist 600), 6 BP_Trash_bin_b, "
            "3 BP_Scooter_02 at village center. All Z = ps_loc.z. Total 30+ operations."
        ),
    },
    {
        "name": "map_52_village_night_remix",
        "template": "/Game/Village/Maps/Village_SummerNightExample",
        "nav_center": [0, 0, 100],
        "nav_scale": [120, 120, 20],
        "task": (
            "Slavic village at night — cross-pack REMIX. Spawn 5 CityDatabase buildings with varied IDs "
            "(BP_Building_24, BP_Building_55, BP_Building_82, BP_Building_103, BP_Building_119) at ±3000 UU "
            "of PS using min_dist 800 collision spacing. Z = ps_loc.z. "
            "Add 6 BP_Scooter mixed _01/_02/_03/_04 clustered at village center. "
            "Add 8 BP_Trash_can + 4 BP_Hydrant scattered. Heavy remix — urban-meets-rural feel."
        ),
    },
    {
        "name": "map_53_courtyard_sanny_big_remix",
        "template": "/Game/ModularCourtyard/Maps/SampleScene_sanny",
        "nav_center": [0, 0, 100],
        "nav_scale": [90, 90, 15],
        "task": (
            "ModularCourtyard SampleScene_sanny — LARGE farmers-market REMIX. "
            "Spawn 8 BP_Table in 2 rows (min_dist 400), 6 BP_Trash_bin_a, 6 BP_Box + 4 BP_Box2 stacked "
            "as produce crates (min_dist 250), 3 BP_Cart2 at aisle ends, 4 BP_Tree2 at plaza corners. "
            "All Z = ps_loc.z. Total 27+ spawn operations, visibly dense market scene."
        ),
    },
    {
        "name": "map_54_courtyard_overcast_abandoned",
        "template": "/Game/ModularCourtyard/Maps/SampleScene_overcast",
        "nav_center": [0, 0, 100],
        "nav_scale": [90, 90, 15],
        "task": (
            "ModularCourtyard SampleScene_overcast — REMIX as abandoned lot. Spawn *heavy clutter*: "
            "8 BP_Trash_can, 6 BP_Rabbish, 4 BP_Can, 3 BP_RoadBlocker, 3 BP_RoadCone, 2 BP_Couch, "
            "2 BP_Scooter_04 (abandoned bikes). Scatter using min_dist 200. Z = ps_loc.z. "
            "Visually run-down, cluttered. 28+ new actors."
        ),
    },
    {
        "name": "map_55_trainstation_big_native",
        "template": "/Game/TrainStation/Maps/Demonstration",
        "nav_center": [0, 0, 100],
        "nav_scale": [120, 120, 25],
        "task": (
            "Victorian train station — NATIVE bold edit. Find 15 actors matching SM_Crate*, SM_Barrel*, "
            "SM_Luggage*, SM_Trunk*, SM_Bench*, or SM_Sign* and delete them to dramatically open the "
            "platform. Then spawn 6 BP_Trash_bin_a (period-fitting metal bins) and 4 BP_Table (as "
            "'waiting tables'), all at ps_loc.z. Keep steampunk period feel but cleaner."
        ),
    },
    {
        "name": "map_56_containeryard_settlement",
        "template": "/Game/ContainerYard/Maps/Demonstration",
        "nav_center": [0, 0, 100],
        "nav_scale": [120, 120, 25],
        "task": (
            "Container yard — cross-pack REMIX as squatter settlement. Spawn 6 CityDatabase buildings "
            "(BP_Building_91, _21, _44, _67, _91, _115) in a loose cluster at ±2500 UU of PS (use "
            "ps_loc.z). Add 4 BP_Couch + 4 BP_Table as 'living arrangements'. Add 5 BP_Tree1 + 3 BP_Tree4 "
            "(dried scrub). 3 BP_Hydrant, 4 BP_Trash_bin_b. Heavy remix — visibly an inhabited zone."
        ),
    },
    {
        "name": "map_57_gothic_day_invasion",
        "template": "/Game/ModularGothicFantasyEnvironment/Maps/DemoMapDay",
        "nav_center": [0, 0, 100],
        "nav_scale": [110, 110, 25],
        "task": (
            "Gothic cathedral plaza (day) — REMIX: 'modern gentrification invasion'. Find 6-10 "
            "SM_Gravestone*, SM_Debris*, SM_Pillar_Broken* and delete to open the plaza. Then spawn "
            "4 CityDatabase buildings (BP_Building_12, _38, _77, _99) at plaza edges (ps_loc.z). Add "
            "3 BP_Scooter_02, 4 BP_Table (cafes), 5 BP_Trash_bin_a. 4 BP_Tree6 for atmosphere. "
            "Total ~20 visible additions + 10 deletions."
        ),
    },
    {
        "name": "map_58_dungeon_library",
        "template": "/Game/Dungeon/Levels/Dungeon_Demo_00",
        "nav_center": [0, 0, 100],
        "nav_scale": [60, 60, 15],
        "task": (
            "Dungeon interior — REMIX as 'ancient library'. CRITICAL: find PS first (Step 0) — the floor "
            "Z may NOT be 0 for this 3D template. Use ps_loc.z as authoritative floor Z for ALL spawns. "
            "Spawn 8 BP_Table arranged in 2x4 grid within ±600 UU of PS (reading tables). "
            "Add 6 BP_Couch (reading chairs), 4 BP_Tree1 (dried plants), 3 BP_Trash_can (ash barrels). "
            "All Z = ps_loc.z. Should feel like a forgotten scholars' den."
        ),
    },
    {
        "name": "map_59_B10_market_plaza",
        "template": "/Game/Maps/EmptyMap",
        "nav_center": [0, 0, 0],
        "nav_scale": [100, 100, 10],
        "task": (
            "EMPTY MAP scratch: busy market plaza. "
            "First `setup_environment(time_of_day='morning', ground_size=200)`. "
            "Build a market: 10 BP_Table in 2 rows (rows at Y=+800, Y=-800, each with 5 tables at "
            "X=-2000,-1000,0,1000,2000). Add 8 BP_Box + 6 BP_Box2 stacked on/near tables as goods. "
            "Add 4 BP_Trash_bin_a, 3 BP_Hydrant, 2 BP_Cart. Edge trees: 6 BP_Tree3 around plaza perimeter. "
            "Buildings: 2 BP_Building_11 at [4000, 0, 0] and [-4000, 0, 0] as market gates. "
            "All Z = ps_loc.z. Should be a vibrant, visually dense market."
        ),
    },
    {
        "name": "map_60_B11_forest_camp",
        "template": "/Game/Maps/EmptyMap",
        "nav_center": [0, 0, 0],
        "nav_scale": [100, 100, 10],
        "task": (
            "EMPTY MAP scratch: forest clearing with camp. "
            "`setup_environment(time_of_day='sunset', ground_size=200)`. "
            "Trees: 12 BP_Tree (mix 1-6) forming a dense ring at radius ~3500 from origin. "
            "Inner clearing with a camp: 2 BP_Building_17 (cabins) at [1500, 1500, 0] and [-1500, -1500, 0]. "
            "Camp center: 4 BP_Table, 2 BP_Couch, 3 BP_Trash_can (fire barrels), 2 BP_Cart (supplies). "
            "Scatter 8 BP_Box / BP_Box2 / BP_Box3 mix as camp crates. "
            "All Z = ps_loc.z. Dense, atmospheric camp-in-forest."
        ),
    },

    {
        "name": "map_50_B9_sparse_wilderness",
        "template": "/Game/Maps/EmptyMap",
        "nav_center": [0, 0, 0],
        "nav_scale": [100, 100, 10],
        "task": (
            "EMPTY MAP scratch: sparse wilderness clearing. "
            "`setup_environment(time_of_day='morning', ground_size=200)`. "
            "Minimalist: spawn 6 BP_Tree (vary IDs) in a rough circle around origin at radius ~3000. "
            "Add 1 BP_Building_17 (forest shack) at [-3500, 0, 0]. "
            "Add 2 BP_Table (camp setup) and 1 BP_Couch at origin. "
            "No pavement — quiet forest clearing."
        ),
    },

    # =========================================================================
    # Batch 4 (map_61..70 — buffer for autonomous 50-count target).
    # Mix of untouched template variants + more Block B scratch builds.
    # =========================================================================
    {
        "name": "map_61_containeryard_day_big_remix",
        "template": "/Game/ContainerYard/Maps/Demonstration_Day",
        "nav_center": [0, 0, 100],
        "nav_scale": [120, 120, 25],
        "task": (
            "Container yard (day) — LARGE REMIX as a trucking depot. Spawn 5 CityDatabase buildings "
            "(BP_Building_19, _38, _62, _88, _111) along the yard perimeter (min_dist 900). "
            "Add 6 BP_Cart + 4 BP_Cart2 arranged as cargo carts. Add 8 BP_Box + 6 BP_Box2 + 4 BP_Box3 "
            "scattered as freight piles (min_dist 250). 4 BP_RoadCone as traffic markers. Z = ps_loc.z."
        ),
    },
    {
        "name": "map_62_trainstation_cross_remix",
        "template": "/Game/TrainStation/Maps/Demonstration",
        "nav_center": [0, 0, 100],
        "nav_scale": [120, 120, 25],
        "task": (
            "Victorian train station — BOLD cross-pack REMIX. Delete 12-18 platform actors matching "
            "SM_Bench*, SM_Crate*, SM_Luggage*, SM_Sign*. Then spawn 4 CityDatabase buildings "
            "(BP_Building_29, _55, _82, _119) at platform ends as 'station extensions' (min_dist 800). "
            "Add 5 BP_Tree3 + 3 BP_Tree5 near tracks (min_dist 500). Add 3 BP_Scooter as modern bikes. "
            "Should visibly merge Victorian + modern aesthetics."
        ),
    },
    {
        "name": "map_63_village_summer_market",
        "template": "/Game/Village/Maps/Village_SummerNightExample",
        "nav_center": [0, 0, 100],
        "nav_scale": [100, 100, 15],
        "task": (
            "Slavic village at night — late-night MARKET remix (different angle from map_29). "
            "Delete 10-15 SM_Fence* / SM_Barrel* actors to open central square. "
            "Build a market in the square: 6 BP_Table in loose grid (min_dist 400), "
            "4 BP_Couch as seating, 5 BP_Trash_bin_a, 3 BP_Hydrant, "
            "2 BP_Cart + 2 BP_Cart2 at corners. Add 5 BP_Tree6 around market perimeter (min_dist 600)."
        ),
    },
    {
        "name": "map_64_cave_lost_expedition",
        "template": "/Game/Cave/Maps/Demonstration",
        "nav_center": [0, 0, 100],
        "nav_scale": [60, 60, 15],
        "task": (
            "Fantasy cave — 'lost expedition' remix (different from map_08). Survey with "
            "get_actors_in_level to find floor Z. Spawn at ps_loc.z. "
            "Scattered: 4 BP_Trash_can (barrels), 3 BP_Box (supply crates), 2 BP_Box2, "
            "2 BP_Table (field stations), 3 BP_Couch (bedrolls), 2 BP_Hydrant (water tanks). "
            "Use min_dist 200. Total ~16 spawns within ~800 UU of PS."
        ),
    },
    {
        "name": "map_65_hwaseong_ceremonial",
        "template": "/Game/HwaseongHaenggung/Maps/Demo",
        "nav_center": [0, 0, 100],
        "nav_scale": [100, 100, 15],
        "task": (
            "Korean palace — 'ceremonial grounds' variant (different from maps 06, 16, 48). "
            "Delete 8-12 SM_Fence*, SM_Barrier*, SM_Chair* or SM_Props* actors to fully open the courtyard. "
            "Spawn 8 BP_Tree4 (period trees) at courtyard edges (min_dist 700). "
            "Add 4 BP_Table as ceremonial stations, 2 BP_Couch at pavilion. Z = ps_loc.z."
        ),
    },
    {
        "name": "map_66_middleeast_bazaar",
        "template": "/Game/MiddleEast/Maps/MiddleEast",
        "nav_center": [0, 0, 100],
        "nav_scale": [100, 100, 15],
        "task": (
            "Middle-east village — BAZAAR remix (different from maps 05, 15). Delete 10-15 clutter "
            "actors matching SM_Crate*, SM_Barrel*, or SM_Cardboard*. "
            "Then build a bazaar: 8 BP_Table in 2 rows (min_dist 350), 6 BP_Trash_bin_a, 5 BP_Box + "
            "3 BP_Box2 as merchant crates, 4 BP_Tree1 at edges (min_dist 600). Add 2 BP_Cart. Z=ps_loc.z."
        ),
    },
    {
        "name": "map_67_B12_construction_site",
        "template": "/Game/Maps/EmptyMap",
        "nav_center": [0, 0, 0],
        "nav_scale": [100, 100, 10],
        "task": (
            "EMPTY MAP scratch: construction site. "
            "`setup_environment(time_of_day='afternoon', ground_size=200)`. "
            "Spawn 2 big buildings under construction: BP_Building_27, BP_Building_58 at [2500, 0, 0] and [-2500, 0, 0]. "
            "Around them: 8 BP_RoadBlocker + 6 BP_RoadCone as barriers (min_dist 300). "
            "6 BP_Box + 4 BP_Box2 + 4 BP_Box3 stacked as materials. "
            "3 BP_Cart + 2 BP_Cart2 as wheelbarrows. 4 BP_Trash_can. 3 BP_Hydrant. "
            "Busy construction feel. Z = ps_loc.z."
        ),
    },
    {
        "name": "map_68_B13_suburban_park",
        "template": "/Game/Maps/EmptyMap",
        "nav_center": [0, 0, 0],
        "nav_scale": [100, 100, 10],
        "task": (
            "EMPTY MAP scratch: suburban park with lawn + paths. "
            "`setup_environment(time_of_day='morning', ground_size=200)`. "
            "Spawn 12 mixed BP_Tree (IDs 1-6) scattered across full ±4000 UU (min_dist 700). "
            "Add 5 BP_Table (picnic tables), 8 BP_Couch (park benches) spread out. "
            "4 BP_Trash_bin_a, 3 BP_Hydrant scattered. "
            "Border: 2 small houses BP_Building_77, BP_Building_93 at [±4500, 0, 0]. Z = ps_loc.z."
        ),
    },
    {
        "name": "map_69_B14_warehouse_district",
        "template": "/Game/Maps/EmptyMap",
        "nav_center": [0, 0, 0],
        "nav_scale": [100, 100, 10],
        "task": (
            "EMPTY MAP scratch: warehouse/industrial district. "
            "`setup_environment(time_of_day='afternoon', ground_size=200)`. "
            "Spawn 6 large buildings (BP_Building_46, _71, _88, _103, _115, _122) in 2-row layout "
            "at Y=+2000 and Y=-2000, X=-3500/0/+3500 (min_dist 1000). "
            "Between the rows: 8 BP_Cart + 4 BP_Cart2 as loading carts, "
            "8 BP_Box + 6 BP_Box2 as palletized goods. 4 BP_RoadBlocker. 3 BP_Trash_bin_b. Z=ps_loc.z."
        ),
    },
    {
        "name": "map_70_B15_open_air_restaurant",
        "template": "/Game/Maps/EmptyMap",
        "nav_center": [0, 0, 0],
        "nav_scale": [100, 100, 10],
        "task": (
            "EMPTY MAP scratch: open-air restaurant plaza. "
            "`setup_environment(time_of_day='sunset', ground_size=200)`. "
            "Spawn 2 restaurant buildings: BP_Building_33 at [-3000, 0, 0], BP_Building_91 at [3000, 0, 0]. "
            "Between them, a DINING AREA: 12 BP_Table arranged in 3 rows of 4 (min_dist 400), "
            "16 BP_Couch (chairs) spread near tables. "
            "Perimeter: 6 BP_Tree2 + 4 BP_Tree4 (min_dist 500). "
            "3 BP_Hydrant + 4 BP_Trash_bin_a. Z = ps_loc.z. Dense, inviting feel."
        ),
    },
]


def build_prompt(entry: dict, save_ue_path: str) -> str:
    """Materialize the full agent prompt for an entry."""
    task = textwrap.dedent(entry["task"]).strip()
    nav_c = entry["nav_center"]
    nav_s = entry["nav_scale"]
    return (
        COMMON_TEMPLATE
        .replace("__TASK__", task)
        .replace("__SAVE_PATH__", save_ue_path)
        .replace("__NAME__", entry["name"])
        .replace("__NAV_CX__", str(nav_c[0]))
        .replace("__NAV_CY__", str(nav_c[1]))
        .replace("__NAV_CZ__", str(nav_c[2]))
        .replace("__NAV_SX__", str(nav_s[0]))
        .replace("__NAV_SY__", str(nav_s[1]))
        .replace("__NAV_SZ__", str(nav_s[2]))
    )


if __name__ == "__main__":
    # sanity: print first prompt
    p0 = build_prompt(PROMPTS[0], "/Game/DiverseMaps50/map_01_wintertown_native")
    print(p0[:800])
    print("...")
    print(p0[-400:])
    print(f"\nTotal prompts: {len(PROMPTS)}")
