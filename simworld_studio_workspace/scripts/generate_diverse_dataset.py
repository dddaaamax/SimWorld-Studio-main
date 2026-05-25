"""Generate PointNav + ObjectNav task dataset from DiverseMaps50.

Split strategy:
  - Template hold-out: all temple_plaza maps → test
  - Remaining maps: 80 / 20 train / test split (deterministic, seed-based)
  - Target: ~1200+ tasks, ~7:3 train/test ratio

Per map:
  - Boot UE in editor mode with the map
  - Spawn NavMeshBoundsVolume if absent, rebuild navmesh, start PIE
  - Connect UCVClient
  - Sample 20 pointnav tasks + 20 objectnav tasks
  - Write to separate JSONL files

Output (under simworld_studio_workspace/datasets/diverse50/):
  train_pointnav.jsonl
  test_pointnav.jsonl
  train_objectnav.jsonl
  test_objectnav.jsonl
  split_manifest.json

Usage:
  cd simworld_studio_workspace
  python3 scripts/generate_diverse_dataset.py --parallel 8
  python3 scripts/generate_diverse_dataset.py --parallel 8 --resume
  python3 scripts/generate_diverse_dataset.py --only map_34_temple_plaza_native
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import pathlib
import queue
import random
import subprocess
import sys
import threading
import time
from typing import List, Optional

_THIS = pathlib.Path(__file__).resolve()
WORKSPACE = _THIS.parent.parent
sys.path.insert(0, str(WORKSPACE))
sys.path.insert(0, str(WORKSPACE / "gym_env"))
sys.path.insert(0, str(WORKSPACE / "nav_task"))

UE_EDITOR = os.environ.get("UE_EDITOR", "/data/koe/UE_5.3.2/Engine/Binaries/Linux/UnrealEditor")
MAPS_DIR = pathlib.Path("/data/koe/simworld_studio_projects/Content/DiverseMaps50")
DATASET_DIR = WORKSPACE / "datasets" / "diverse50"

TASKS_PER_MAP = 20
SPLIT_SEED = 2026
SPLIT_RATIO = 0.8          # 80% train among non-holdout maps

# Template-based hold-out: ALL maps from these templates go to test set
HOLDOUT_TEMPLATES = ["temple_plaza"]

# Navmesh / episode sampling parameters
TASK_SEED_BASE = 7777
MIN_GEODESIC_CM = 1000.0
MAX_GEODESIC_CM = 8000.0
SUCCESS_DIST_CM  = 200.0

# Per-actor objectnav description mapping
# Maps substrings in actor label → (category, description)
ACTOR_DESCRIPTIONS = [
    ("BP_Hydrant",    "fire_hydrant",   "a red fire hydrant"),
    ("BP_Trash_bin",  "trash_bin",      "a trash bin"),
    ("BP_Table",      "table",          "a table"),
    ("BP_Bench",      "bench",          "a bench"),
    ("BP_RoadCone",   "traffic_cone",   "an orange traffic cone"),
    ("BP_Couch",      "couch",          "a couch"),
    ("BP_Tree",       "tree",           "a tree"),
    ("BP_Building_",  "building",       "a large building"),
    ("BP_Scooter",    "scooter",        "a scooter"),
    ("BP_Cart",       "cart",           "a cart"),
    # Allow-AI pack actors from template maps
    ("SM_Tree",       "tree",           "a tree"),
    ("SM_Prop",       "prop",           "a prop"),
    ("SM_Bench",      "bench",          "a bench"),
    ("SM_Lamp",       "street lamp",    "a street lamp"),
    ("SM_Fence",      "fence",          "a fence"),
    ("BP_Lamp",       "street lamp",    "a street lamp"),
    ("Hydrant",       "fire_hydrant",   "a fire hydrant"),
    ("TrashBin",      "trash_bin",      "a trash bin"),
    ("Bldg_",         "building",       "a building"),
]

SLOTS = [
    {"mcp_port": 55558, "ucv_port": 9010, "gpu": 0, "uproject": "/data/koe/simworld_studio_inst_0/SimWorld.uproject"},
    {"mcp_port": 55560, "ucv_port": 9011, "gpu": 1, "uproject": "/data/koe/simworld_studio_inst_1/SimWorld.uproject"},
    {"mcp_port": 55574, "ucv_port": 9018, "gpu": 3, "uproject": "/data/koe/simworld_studio_inst_2/SimWorld.uproject"},
    {"mcp_port": 55564, "ucv_port": 9013, "gpu": 4, "uproject": "/data/koe/simworld_studio_inst_3/SimWorld.uproject"},
    {"mcp_port": 55576, "ucv_port": 9019, "gpu": 5, "uproject": "/data/koe/simworld_studio_inst_8/SimWorld.uproject"},
    {"mcp_port": 55568, "ucv_port": 9015, "gpu": 6, "uproject": "/data/koe/simworld_studio_inst_5/SimWorld.uproject"},
    {"mcp_port": 55570, "ucv_port": 9016, "gpu": 7, "uproject": "/data/koe/simworld_studio_inst_6/SimWorld.uproject"},
    {"mcp_port": 55572, "ucv_port": 9017, "gpu": 4, "uproject": "/data/koe/simworld_studio_inst_7/SimWorld.uproject"},
]

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Split logic
# ---------------------------------------------------------------------------

def build_split(all_maps: List[str]) -> dict:
    """Return {map_name: "train"|"test"} dict."""
    rng = random.Random(SPLIT_SEED)
    result = {}
    non_holdout = []
    for m in all_maps:
        if any(tmpl in m for tmpl in HOLDOUT_TEMPLATES):
            result[m] = "test"
        else:
            non_holdout.append(m)

    shuffled = sorted(non_holdout)
    rng.shuffle(shuffled)
    n_train = int(len(shuffled) * SPLIT_RATIO)
    for i, m in enumerate(shuffled):
        result[m] = "train" if i < n_train else "test"
    return result


# ---------------------------------------------------------------------------
# UE boot helpers
# ---------------------------------------------------------------------------

def wait_for_mcp(log_path: pathlib.Path, port: int, timeout: int = 180) -> bool:
    bind_ok = f"UnrealMCPBridge: Server started on 127.0.0.1:{port}"
    bind_fail = f"Failed to bind listener socket to 127.0.0.1:{port}"
    start = time.time()
    while time.time() - start < timeout:
        if log_path.exists():
            txt = log_path.read_text(errors="ignore")
            if bind_ok in txt:
                return True
            if bind_fail in txt or "Assertion failed" in txt or "Signal 11 caught" in txt:
                return False
        time.sleep(2)
    return False


# ---------------------------------------------------------------------------
# Actor → objectnav description
# ---------------------------------------------------------------------------

def actor_to_nav_goal(actor_name: str):
    """Return (category, description) or None if not a known target."""
    for substr, cat, desc in ACTOR_DESCRIPTIONS:
        if substr in actor_name:
            return cat, desc
    return None


# ---------------------------------------------------------------------------
# Per-map task generation
# ---------------------------------------------------------------------------

def _find_unrealcv_port(ue_pid: int, mcp_port: int, timeout: int = 40) -> Optional[int]:
    """Find the TCP port UnrealCV is ACTUALLY listening on for a specific UE PID.

    Strategy: find all fd inodes owned by ue_pid, then match those inodes to
    LISTEN sockets in /proc/<pid>/net/tcp to get the exact port for THIS process.
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            # 1. Get all socket inodes owned by ue_pid via /proc/{pid}/fd/
            fd_dir = pathlib.Path(f"/proc/{ue_pid}/fd")
            owned_inodes = set()
            for fd_link in fd_dir.iterdir():
                try:
                    target = fd_link.resolve()
                    name = str(target)
                    if "socket:" in name:
                        inode = name.split("socket:[")[1].rstrip("]")
                        owned_inodes.add(inode)
                except Exception:
                    pass

            if not owned_inodes:
                time.sleep(1)
                continue

            # 2. Scan /proc/<pid>/net/tcp for LISTEN sockets matching those inodes
            net_tcp = pathlib.Path(f"/proc/{ue_pid}/net/tcp").read_text()
            for line in net_tcp.splitlines()[1:]:
                parts = line.split()
                if len(parts) < 10:
                    continue
                state = parts[3]
                if state != "0A":  # 0A = LISTEN
                    continue
                inode = parts[9]
                if inode not in owned_inodes:
                    continue
                local_addr = parts[1]
                port_hex = local_addr.split(":")[1]
                port = int(port_hex, 16)
                if port > 1024 and port != mcp_port:
                    return port
        except Exception:
            pass
        time.sleep(1)
    return None


def generate_for_map(slot: dict, map_name: str, split: str, log_fn) -> dict:
    """Boot UE, start PIE, generate TASKS_PER_MAP pointnav + objectnav tasks.

    Returns dict with:
        ok: bool
        pointnav: list of task dicts
        objectnav: list of task dicts
        reason: str (on failure)
    """
    from gym_env.mcp_client import MCPClient
    from gym_env.ucv_client import UCVClient
    from nav_task.navmesh_interface import NavmeshNavigationInterface
    from gym_env.episode_builder import (
        sample_pointnav_episode_navmesh,
        snapshot_scene,
    )

    ue_map_path = f"/Game/DiverseMaps50/{map_name}"
    uproject = slot["uproject"]
    work_dir = pathlib.Path(f"/tmp/koe_dataset/{map_name}")
    work_dir.mkdir(parents=True, exist_ok=True)
    ue_log = work_dir / "ue.log"

    log_fn(f"[{map_name}] BOOT port={slot['mcp_port']}")
    ue_proc = subprocess.Popen(
        [UE_EDITOR, uproject, ue_map_path,
         f"-MCPPort={slot['mcp_port']}", f"-cvport={slot['ucv_port']}",
         "-Unattended", "-NOSPLASH", "-NOSOUND", "-Messaging",
         "-ResX=1280", "-ResY=720", "-FPSMAX=15", "-RenderOffScreen",
         f"-graphicsadapter={slot['gpu']}", "-log"],
        stdout=open(ue_log, "w"), stderr=subprocess.STDOUT,
    )

    result = {"ok": False, "pointnav": [], "objectnav": [], "reason": ""}
    try:
        if not wait_for_mcp(ue_log, slot["mcp_port"], timeout=180):
            result["reason"] = "MCP_BIND_FAIL"; return result
        log_fn(f"[{map_name}] MCP bound → settle 40s")
        time.sleep(40)
        if "Assertion failed" in ue_log.read_text(errors="ignore"):
            result["reason"] = "UE_CRASHED"; return result

        mcp = MCPClient(host="127.0.0.1", port=slot["mcp_port"])

        # Setup navmesh + start PIE (mirrors generate_tasks.py)
        log_fn(f"[{map_name}] navmesh setup + start PIE")
        nav_script = """
import unreal
try:
    world = unreal.EditorLevelLibrary.get_editor_world()
except:
    world = unreal.get_editor_subsystem(unreal.UnrealEditorSubsystem).get_editor_world()
if world:
    vols = unreal.GameplayStatics.get_all_actors_of_class(world, unreal.NavMeshBoundsVolume)
    eas = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)
    ps  = unreal.GameplayStatics.get_all_actors_of_class(world, unreal.PlayerStart)
    cx, cy = 0.0, 0.0
    if ps:
        l = ps[0].get_actor_location(); cx, cy = l.x, l.y
    if vols:
        vols[0].set_actor_scale3d(unreal.Vector(200, 200, 40))
        vols[0].set_actor_location(unreal.Vector(cx, cy, 0), False, False)
        print('NAV_VOL_RESIZED')
    else:
        vol = eas.spawn_actor_from_class(unreal.NavMeshBoundsVolume, unreal.Vector(cx, cy, 0))
        if vol:
            vol.set_actor_scale3d(unreal.Vector(200, 200, 40))
            print('NAV_VOL_SPAWNED')
    unreal.SystemLibrary.execute_console_command(world, 'RebuildNavigation')
    print('NAV_BUILD_REQUESTED')
"""
        mcp.execute_python(nav_script, timeout=60)
        time.sleep(8)

        # ---- Query BP_ actor labels + locations in EDITOR mode (before PIE) ----
        # PIE renames actors internally; editor mode preserves the labels we set.
        # Build pattern string for embedded Python (avoid f-string collision)
        _bp_patterns = [p[0] for p in ACTOR_DESCRIPTIONS]
        _patterns_repr = repr(_bp_patterns)
        obj_script = f"""
import unreal
eas = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)
patterns = {_patterns_repr}
count = 0
for a in eas.get_all_level_actors():
    try:
        label = a.get_actor_label()
        if not any(p in label for p in patterns):
            continue
        loc = a.get_actor_location()
        print("BP:" + label + ":" + str(float(loc.x)) + ":" + str(float(loc.y)) + ":" + str(float(loc.z)))
        count += 1
    except: pass
print("BP_TOTAL=" + str(count))
"""
        r = mcp.execute_python(obj_script, timeout=60)
        editor_actors = []
        try:
            # Normalize response - handle both {"result": {...}} and direct {"python_logs": [...]}
            if isinstance(r, dict):
                inner = r.get("result") or r
                if isinstance(inner, dict):
                    logs = inner.get("python_logs", [])
                else:
                    logs = []
            else:
                logs = []
            log_fn(f"[{map_name}] actor query: {len(logs)} lines")
            import re as _re
            for line in logs:
                # Strip UE prefix like "[853] " if present
                clean = _re.sub(r'^\[\s*\d+\]\s*', '', line)
                if clean.startswith("BP:"):
                    parts = clean[3:].split(":")
                    if len(parts) == 4:
                        label, x, y, z = parts[0], float(parts[1]), float(parts[2]), float(parts[3])
                        info = actor_to_nav_goal(label)
                        if info:
                            cat, desc = info
                            editor_actors.append((label, (x, y, z), cat, desc))
        except Exception as exc:
            log_fn(f"[{map_name}] editor actor query failed: {exc}")
        log_fn(f"[{map_name}] editor BP_ actors: {len(editor_actors)}")

        mcp.start_pie(wait_seconds=12.0)

        # Discover actual UnrealCV port (binds to OS-assigned port, not our configured one)
        ucv_port = _find_unrealcv_port(ue_proc.pid, slot["mcp_port"], timeout=20)
        if ucv_port is None:
            result["reason"] = "UCV_PORT_NOT_FOUND"; return result
        log_fn(f"[{map_name}] UnrealCV on port {ucv_port}")

        # Connect UCVClient
        ucv = UCVClient(host="127.0.0.1", port=ucv_port)
        for attempt in range(15):
            try:
                ucv.connect()
                break
            except Exception:
                time.sleep(2)
        else:
            result["reason"] = "UCV_CONNECT_FAIL"; return result

        nav = NavmeshNavigationInterface(ucv)

        # Build navmesh in PIE
        for attempt in range(4):
            resp = nav.build_navmesh()
            log_fn(f"[{map_name}] navmesh build: {str(resp)[:80]}")
            if resp and "error" not in str(resp).lower():
                break
            time.sleep(3)
        time.sleep(5)

        rng = random.Random(TASK_SEED_BASE + abs(hash(map_name)) % 10000)
        base_seed = TASK_SEED_BASE + abs(hash(map_name)) % 10000

        # PS Z for ObjectNav ground snapping (editor mode PS query result)
        ps_z_for_on = 100.0  # safe default; overridden below if PS found
        try:
            ps_z_script = """
import unreal
world = unreal.get_editor_subsystem(unreal.UnrealEditorSubsystem).get_editor_world()
ps = unreal.GameplayStatics.get_all_actors_of_class(world, unreal.PlayerStart)
if ps: l = ps[0].get_actor_location(); print("PSZ=%.1f" % float(l.z))
else: print("PSZ=100")
"""
            r_ps = mcp.execute_python(ps_z_script, timeout=20)
            import re as _re_ps
            for ln in (r_ps.get("result", {}).get("python_logs", []) or []):
                cl = _re_ps.sub(r'^\[\s*\d+\]\s*', '', ln)
                if cl.startswith("PSZ="):
                    ps_z_for_on = float(cl[4:])
                    break
        except Exception:
            pass

        pointnav_tasks = []
        objectnav_tasks = []

        from nav_task.episode import Position

        # ---- Use pre-queried editor actors as object candidates ----
        # editor_actors was populated before PIE from MCP editor-mode query.
        obj_candidates = list(editor_actors)

        # If no BP_ objects in scene, spawn temporary targets in EDITOR mode (before PIE).
        # PIE duplicates the editor world so spawned actors will also exist in PIE.
        # We never save after task generation so maps stay unchanged.
        if not obj_candidates:
            log_fn(f"[{map_name}] no BP_ actors — spawning temp targets in editor mode")
            # Get PS location to place targets near the playable area
            ps_script = """
import unreal, json
world = unreal.get_editor_subsystem(unreal.UnrealEditorSubsystem).get_editor_world()
ps = unreal.GameplayStatics.get_all_actors_of_class(world, unreal.PlayerStart)
if ps:
    l = ps[0].get_actor_location()
    print("PS_LOC=%.0f,%.0f,%.0f" % (l.x, l.y, l.z-100))
else:
    print("PS_LOC=0,0,0")
"""
            ps_r = mcp.execute_python(ps_script, timeout=30)
            ps_loc = (0.0, 0.0, 0.0)
            try:
                import re as _re2
                for line in ps_r.get("result", {}).get("python_logs", []):
                    clean_ps = _re2.sub(r'^\[\s*\d+\]\s*', '', line)
                    if clean_ps.startswith("PS_LOC="):
                        coords = clean_ps[7:].split(",")
                        ps_loc = (float(coords[0]), float(coords[1]), float(coords[2]))
                        break
            except Exception:
                pass

            temp_targets = [
                ("TempHydrant_0", "fire_hydrant", "a red fire hydrant",
                 "/Game/CityDatabase/blueprints/BP_Hydrant.BP_Hydrant_C",
                 (ps_loc[0] + 500, ps_loc[1], ps_loc[2])),
                ("TempBin_0", "trash_bin", "a trash bin",
                 "/Game/CityDatabase/blueprints/BP_Trash_bin_a.BP_Trash_bin_a_C",
                 (ps_loc[0] - 500, ps_loc[1] + 300, ps_loc[2])),
                ("TempTree_0", "tree", "a tree",
                 "/Game/CityDatabase/blueprints/BP_Tree3.BP_Tree3_C",
                 (ps_loc[0], ps_loc[1] + 700, ps_loc[2])),
            ]
            for label, cat, desc, bp_path, (tx, ty, tz) in temp_targets:
                spawn_script = f"""
import unreal
eas = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)
bp = unreal.load_object(None, '{bp_path}')
if bp:
    a = eas.spawn_actor_from_class(bp, unreal.Vector({tx}, {ty}, {tz}))
    if a:
        a.set_actor_label('{label}')
        print('SPAWNED {label}')
    else:
        print('SPAWN_FAILED {label}')
else:
    print('BP_NOT_FOUND {bp_path}')
"""
                r = mcp.execute_python(spawn_script, timeout=30)
                logs = r.get("result", {}).get("python_logs", []) if isinstance(r, dict) else []
                if any(f"SPAWNED {label}" in l for l in logs) or any(f"SPAWNED {label}" in _re.sub(r'^\[\s*\d+\]\s*','',l) for l in logs):
                    obj_candidates.append((label, (tx, ty, tz), cat, desc))
                    log_fn(f"[{map_name}] temp spawned {label} at ({tx:.0f},{ty:.0f})")
                else:
                    log_fn(f"[{map_name}] temp spawn failed {label}: {logs}")

        # ---- Generate tasks using existing pipeline ----
        # PointNav: use sample_pointnav_episode_navmesh (full pipeline with NavigationEpisode)
        # ObjectNav: same start positions, object goals from editor_actors
        log_fn(f"[{map_name}] generating {TASKS_PER_MAP} tasks")

        import math
        from gym_env.episode_builder import sample_pointnav_episode_navmesh

        # --- PointNav tasks via pipeline ---
        for i in range(TASKS_PER_MAP):
            try:
                res = sample_pointnav_episode_navmesh(
                    ucv,
                    seed=base_seed + i,
                    idx=i,
                    min_geodesic_cm=MIN_GEODESIC_CM,
                    max_geodesic_cm=MAX_GEODESIC_CM,
                    success_distance_cm=SUCCESS_DIST_CM,
                    max_steps=60,
                    build_navmesh=False,
                    nav_interface=nav,
                )
                ep = res["episode"]
                pointnav_tasks.append({
                    "episode_id": f"{map_name}_pn_{i:04d}",
                    "map": map_name,
                    "umap_path": f"/Game/DiverseMaps50/{map_name}",
                    "split": split,
                    "task_type": "pointnav",
                    "start_position": ep.start_position.to_dict(),
                    "start_heading_deg": res["start_heading_deg"],
                    "goal_position": ep.goal_position.to_dict(),
                    "gt_path": [wp.to_dict() for wp in ep.reference_path.waypoints],
                    "geodesic_distance_cm": ep.reference_path.shortest_path_length_cm,
                    "difficulty": res["difficulty"],
                    "success_criteria": {"success_distance_cm": SUCCESS_DIST_CM, "max_steps": 60},
                })
            except Exception as exc:
                log_fn(f"[{map_name}] pointnav[{i}] failed: {exc}")

        log_fn(f"[{map_name}] pointnav: {len(pointnav_tasks)}/{TASKS_PER_MAP}")

        # --- ObjectNav tasks: one per PointNav task ---
        # For each PointNav task, spawn a BP object at the PointNav goal position
        # (editor mode, temporary — not saved). Use as ObjectNav target.
        # This guarantees on == pn (same GT path, same task, different goal representation).
        BP_TARGET_OPTIONS = [
            ("fire_hydrant", "a red fire hydrant",
             "/Game/CityDatabase/blueprints/BP_Hydrant.BP_Hydrant_C"),
            ("trash_bin",    "a trash bin",
             "/Game/CityDatabase/blueprints/BP_Trash_bin_a.BP_Trash_bin_a_C"),
            ("tree",         "a tree",
             "/Game/CityDatabase/blueprints/BP_Tree3.BP_Tree3_C"),
        ]
        rng_bp = random.Random(base_seed + 7777)

        for i, pn_task in enumerate(pointnav_tasks):
            try:
                gp = pn_task["goal_position"]
                gx, gy = gp["x"], gp["y"]
                # Ground Z: from navmesh projection or ps_z-100 fallback
                goal_pos_nav = Position(x=gx, y=gy, node_type="navmesh")
                nav_pt = nav.project_to_navmesh(goal_pos_nav) if hasattr(nav, 'project_to_navmesh') else None
                gz = nav_pt.z if (nav_pt and nav_pt.z != 0) else (ps_z_for_on - 100)

                cat, desc, bp_path = rng_bp.choice(BP_TARGET_OPTIONS)
                lbl = f"ObjNav_target_{i}"
                # Spawn in editor via MCP
                spawn_s = f"""
import unreal
eas = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)
bp = unreal.load_object(None, '{bp_path}')
if bp:
    a = eas.spawn_actor_from_class(bp, unreal.Vector({gx}, {gy}, {gz}))
    if a: a.set_actor_label('{lbl}'); print('SPAWNED')
    else: print('SPAWN_FAILED')
else: print('BP_NOT_FOUND')
"""
                mcp.execute_python(spawn_s, timeout=20)

                objectnav_tasks.append({
                    "episode_id": f"{map_name}_on_{i:04d}",
                    "map": map_name,
                    "umap_path": f"/Game/DiverseMaps50/{map_name}",
                    "split": split,
                    "task_type": "objectnav",
                    "start_position": pn_task["start_position"],
                    "start_heading_deg": pn_task["start_heading_deg"],
                    "target_category": cat,
                    "target_description": desc,
                    "gt_path": pn_task["gt_path"],
                    "geodesic_distance_cm": pn_task["geodesic_distance_cm"],
                    "success_criteria": {"success_distance_cm": SUCCESS_DIST_CM, "max_steps": 60},
                })
            except Exception as exc:
                log_fn(f"[{map_name}] objectnav[{i}] spawn failed: {exc}")

        log_fn(f"[{map_name}] objectnav: {len(objectnav_tasks)}/{len(pointnav_tasks)}")
        result["pointnav"] = pointnav_tasks
        result["objectnav"] = objectnav_tasks
        result["ok"] = len(pointnav_tasks) > 0
        return result

        # --- OLD: ObjectNav tasks from existing objects (kept for reference) ---
        if False and obj_candidates and pointnav_tasks:
            rng_obj = random.Random(base_seed + 5000)
            navigable_positions = nav.get_navigable_positions(count=300, rng=rng_obj)

            for i in range(TASKS_PER_MAP):
                if not navigable_positions:
                    break
                attempts = 0
                while attempts < 30:
                    attempts += 1
                    try:
                        t_name, t_loc, t_cat, t_desc = rng_obj.choice(obj_candidates)
                        tx, ty, tz = t_loc
                        goal_pos = Position(x=tx, y=ty, node_type="object")
                        start_pos = rng_obj.choice(navigable_positions)

                        geo = nav.get_geodesic_distance(start_pos, goal_pos)
                        if geo is None or geo < MIN_GEODESIC_CM or geo > MAX_GEODESIC_CM:
                            continue

                        gt_wps = nav.get_reference_path(start_pos, goal_pos)
                        if gt_wps is None:
                            continue

                        eucl = math.sqrt((tx - start_pos.x)**2 + (ty - start_pos.y)**2)
                        start_heading = rng_obj.uniform(0, 360)

                        # PointNav counterpart: record goal coordinates
                        pointnav_tasks.append({
                            "episode_id": f"{map_name}_obj_pn_{i:04d}",
                            "map": map_name,
                            "umap_path": f"/Game/DiverseMaps50/{map_name}",
                            "split": split,
                            "task_type": "pointnav",
                            "start_position": start_pos.to_dict(),
                            "start_heading_deg": round(start_heading, 1),
                            "goal_position": {"x": round(tx, 2), "y": round(ty, 2), "node_type": "object"},
                            "gt_path": [wp.to_dict() for wp in gt_wps],
                            "geodesic_distance_cm": round(geo, 2),
                            "difficulty": {
                                "distance_m": geo / 100,
                                "detour_ratio": round(geo / eucl, 3) if eucl > 0 else 1.0,
                                "difficulty_score": min(geo / 10000, 1.0),
                            },
                            "success_criteria": {"success_distance_cm": SUCCESS_DIST_CM, "max_steps": 60},
                        })

                        # ObjectNav counterpart: record description only (no coordinates)
                        objectnav_tasks.append({
                            "episode_id": f"{map_name}_on_{i:04d}",
                            "map": map_name,
                            "umap_path": f"/Game/DiverseMaps50/{map_name}",
                            "split": split,
                            "task_type": "objectnav",
                            "start_position": start_pos.to_dict(),
                            "start_heading_deg": round(start_heading, 1),
                            "target_category": t_cat,
                            "target_description": t_desc,
                            "gt_path": [wp.to_dict() for wp in gt_wps],
                            "geodesic_distance_cm": round(geo, 2),
                            "success_criteria": {"success_distance_cm": SUCCESS_DIST_CM, "max_steps": 60},
                        })
                        break
                    except Exception as exc:
                        log_fn(f"[{map_name}] objectnav[{i}] attempt {attempts} failed: {exc}")

        log_fn(f"[{map_name}] objectnav: {len(objectnav_tasks)}/{TASKS_PER_MAP}")

        result["pointnav"] = pointnav_tasks
        result["objectnav"] = objectnav_tasks

        result["ok"] = len(pointnav_tasks) > 0
        return result

    except Exception as exc:
        result["reason"] = f"EXCEPTION:{exc}"; return result
    finally:
        try:
            mcp_f = MCPClient(host="127.0.0.1", port=slot["mcp_port"])
            mcp_f.stop_pie()
        except Exception:
            pass
        ue_proc.kill()
        try: ue_proc.wait(timeout=15)
        except: pass
        subprocess.run(["fuser", "-k", f"{slot['mcp_port']}/tcp"], capture_output=True)
        subprocess.run(["fuser", "-k", f"{slot['ucv_port']}/tcp"], capture_output=True)
        time.sleep(8)


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--parallel", type=int, default=4)
    ap.add_argument("--resume", action="store_true")
    ap.add_argument("--only", type=str, default=None)
    ap.add_argument("--tasks-per-map", type=int, default=TASKS_PER_MAP)
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-5s | %(message)s",
    )

    DATASET_DIR.mkdir(parents=True, exist_ok=True)
    tmp_dir = pathlib.Path("/tmp/koe_dataset")
    tmp_dir.mkdir(parents=True, exist_ok=True)

    # Discover maps
    all_maps = sorted(p.stem for p in MAPS_DIR.glob("*.umap"))
    if args.only:
        all_maps = [m for m in all_maps if m in args.only.split(",")]

    split_map = build_split(all_maps)

    # Resume: skip maps already done (have a done marker)
    if args.resume:
        before = len(all_maps)
        all_maps = [m for m in all_maps if not (tmp_dir / m / "done").exists()]
        print(f"RESUME: skipping {before - len(all_maps)} already-done")

    # Print split summary
    n_train = sum(1 for m in split_map if split_map[m] == "train")
    n_test  = sum(1 for m in split_map if split_map[m] == "test")
    print(f"Split: {n_train} train, {n_test} test maps")
    print(f"Queue: {len(all_maps)} maps, parallel={args.parallel}")
    for m in all_maps:
        print(f"  [{split_map[m]:5s}] {m}")

    # JSONL output files (append-safe, one record per line)
    out_files = {
        "train_pointnav":  open(DATASET_DIR / "train_pointnav.jsonl", "a"),
        "test_pointnav":   open(DATASET_DIR / "test_pointnav.jsonl", "a"),
        "train_objectnav": open(DATASET_DIR / "train_objectnav.jsonl", "a"),
        "test_objectnav":  open(DATASET_DIR / "test_objectnav.jsonl", "a"),
    }
    write_lock = threading.Lock()

    def write_tasks(tasks: list, split: str, task_type: str):
        key = f"{split}_{task_type}"
        with write_lock:
            f = out_files[key]
            for t in tasks:
                f.write(json.dumps(t, ensure_ascii=False) + "\n")
            f.flush()

    log_lock = threading.Lock()
    def log_fn(msg):
        with log_lock:
            print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)

    # Stats
    stats = {"total_pn": 0, "total_on": 0, "ok": 0, "fail": 0}
    stats_lock = threading.Lock()

    jobq: queue.Queue = queue.Queue()
    for m in all_maps:
        jobq.put(m)

    def worker(slot_idx):
        slot = SLOTS[slot_idx]
        while True:
            try:
                map_name = jobq.get_nowait()
            except queue.Empty:
                return
            split = split_map[map_name]
            try:
                res = generate_for_map(slot, map_name, split, log_fn)
            except Exception as exc:
                res = {"ok": False, "pointnav": [], "objectnav": [],
                       "reason": f"EXCEPTION:{exc}"}

            if res["ok"]:
                write_tasks(res["pointnav"], split, "pointnav")
                write_tasks(res["objectnav"], split, "objectnav")
                # Mark done
                done_f = pathlib.Path(f"/tmp/koe_dataset/{map_name}/done")
                done_f.parent.mkdir(parents=True, exist_ok=True)
                done_f.write_text(json.dumps({
                    "pn": len(res["pointnav"]),
                    "on": len(res["objectnav"]),
                }))

            with stats_lock:
                stats["ok" if res["ok"] else "fail"] += 1
                stats["total_pn"] += len(res["pointnav"])
                stats["total_on"] += len(res["objectnav"])
                status = "OK  " if res["ok"] else "FAIL"
                log_fn(f"[{map_name}] {status} pn={len(res['pointnav'])} "
                       f"on={len(res['objectnav'])}  {res.get('reason','')}")
            jobq.task_done()

    threads = [threading.Thread(target=worker, args=(i,), daemon=True)
               for i in range(min(args.parallel, len(SLOTS)))]
    for t in threads: t.start()
    for t in threads: t.join()

    for f in out_files.values():
        f.close()

    # Write split manifest
    manifest = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "tasks_per_map": args.tasks_per_map,
        "split_seed": SPLIT_SEED,
        "holdout_templates": HOLDOUT_TEMPLATES,
        "total_pointnav": stats["total_pn"],
        "total_objectnav": stats["total_on"],
        "maps": {m: split_map[m] for m in sorted(split_map)},
    }
    (DATASET_DIR / "split_manifest.json").write_text(json.dumps(manifest, indent=2))

    print(f"\n=== DONE ===")
    print(f"  OK: {stats['ok']}  FAIL: {stats['fail']}")
    print(f"  PointNav tasks: {stats['total_pn']}")
    print(f"  ObjectNav tasks: {stats['total_on']}")
    print(f"  Output: {DATASET_DIR}")


if __name__ == "__main__":
    main()
