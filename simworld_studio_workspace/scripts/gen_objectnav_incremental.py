"""Incremental ObjectNav generation: spawn BP objects at PN goal positions.

For each map that has PointNav tasks but missing ObjectNav tasks:
1. Boot UE with that map (editor mode)
2. For each PN task: spawn a random BP object at the goal position
3. Save the updated map (objects are now in the scene)
4. Write ObjectNav JSONL records (same start/path as PN, goal = object description)

Usage:
  cd simworld_studio_workspace
  python3 -u scripts/gen_objectnav_incremental.py --parallel 4
  python3 -u scripts/gen_objectnav_incremental.py --parallel 4 --resume
"""
from __future__ import annotations
import argparse, json, pathlib, queue, random, subprocess, sys, threading, time
import collections

_THIS = pathlib.Path(__file__).resolve()
WORKSPACE = _THIS.parent.parent
sys.path.insert(0, str(WORKSPACE))

UE_EDITOR = "/data/koe/UE_5.3.2/Engine/Binaries/Linux/UnrealEditor"
DATASET   = WORKSPACE / "datasets" / "diverse50"
MAPS_DIR  = pathlib.Path("/data/koe/simworld_studio_projects/Content/DiverseMaps50")
SUCCESS_DIST_CM = 200.0

SLOTS = [
    {"mcp_port": 55558, "gpu": 0, "uproject": "/data/koe/simworld_studio_inst_0/SimWorld.uproject"},
    {"mcp_port": 55560, "gpu": 1, "uproject": "/data/koe/simworld_studio_inst_1/SimWorld.uproject"},
    {"mcp_port": 55574, "gpu": 3, "uproject": "/data/koe/simworld_studio_inst_2/SimWorld.uproject"},
    {"mcp_port": 55564, "gpu": 4, "uproject": "/data/koe/simworld_studio_inst_3/SimWorld.uproject"},
    {"mcp_port": 55576, "gpu": 5, "uproject": "/data/koe/simworld_studio_inst_8/SimWorld.uproject"},
    {"mcp_port": 55568, "gpu": 6, "uproject": "/data/koe/simworld_studio_inst_5/SimWorld.uproject"},
    {"mcp_port": 55570, "gpu": 7, "uproject": "/data/koe/simworld_studio_inst_6/SimWorld.uproject"},
    {"mcp_port": 55572, "gpu": 4, "uproject": "/data/koe/simworld_studio_inst_7/SimWorld.uproject"},
]

BP_OPTIONS = [
    ("fire_hydrant", "a red fire hydrant",
     "/Game/CityDatabase/blueprints/BP_Hydrant.BP_Hydrant_C"),
    ("trash_bin",    "a trash bin",
     "/Game/CityDatabase/blueprints/BP_Trash_bin_a.BP_Trash_bin_a_C"),
    ("tree",         "a large tree",
     "/Game/CityDatabase/blueprints/BP_Tree3.BP_Tree3_C"),
    ("bench",        "a wooden bench",
     "/Game/CityDatabase/blueprints/BP_Table.BP_Table_C"),
    ("traffic_cone", "an orange traffic cone",
     "/Game/CityDatabase/blueprints/BP_RoadCone.BP_RoadCone_C"),
]


def load_jsonl(p):
    return [json.loads(l) for l in open(p) if l.strip()] if p.exists() else []


def get_maps_needing_on():
    """Return {map_name: (split, [pn_tasks_missing_on])} for maps that need ON tasks."""
    result = {}
    for split in ("train", "test"):
        pn = load_jsonl(DATASET / f"{split}_pointnav.jsonl")
        on = load_jsonl(DATASET / f"{split}_objectnav.jsonl")
        on_ids = {r["episode_id"] for r in on}

        by_map = collections.defaultdict(list)
        for rec in pn:
            on_id = rec["episode_id"].replace("_pn_", "_on_").replace("_obj_pn_", "_on_")
            if on_id not in on_ids:
                by_map[rec["map"]].append(rec)

        for map_name, tasks in by_map.items():
            if map_name not in result:
                result[map_name] = (split, [])
            result[map_name][1].extend(tasks)  # type: ignore

    return result


def wait_for_mcp(log_path: pathlib.Path, port: int, timeout=180) -> bool:
    bind_ok   = f"UnrealMCPBridge: Server started on 127.0.0.1:{port}"
    bind_fail = f"Failed to bind listener socket to 127.0.0.1:{port}"
    t0 = time.time()
    while time.time() - t0 < timeout:
        if log_path.exists():
            txt = log_path.read_text(errors="ignore")
            if bind_ok in txt:   return True
            if bind_fail in txt or "Assertion failed" in txt: return False
        time.sleep(2)
    return False


def gen_on_for_map(slot: dict, map_name: str, split: str,
                   pn_tasks: list, log_fn) -> list:
    """Boot UE, spawn BP objects at PN goal positions, save map, return ON records."""
    from gym_env.mcp_client import MCPClient

    uproject = slot["uproject"]
    ue_map   = f"/Game/DiverseMaps50/{map_name}"
    work_dir = pathlib.Path(f"/tmp/koe_on/{map_name}")
    work_dir.mkdir(parents=True, exist_ok=True)
    ue_log   = work_dir / "ue.log"

    log_fn(f"[{map_name}] BOOT mcp={slot['mcp_port']}")
    ue_proc = subprocess.Popen(
        [UE_EDITOR, uproject, ue_map,
         f"-MCPPort={slot['mcp_port']}",
         "-Unattended", "-NOSPLASH", "-NOSOUND", "-Messaging",
         "-ResX=1280", "-ResY=720", "-FPSMAX=15", "-RenderOffScreen",
         f"-graphicsadapter={slot['gpu']}", "-log"],
        stdout=open(ue_log, "w"), stderr=subprocess.STDOUT,
    )

    on_records = []
    try:
        if not wait_for_mcp(ue_log, slot["mcp_port"], timeout=180):
            log_fn(f"[{map_name}] MCP_BIND_FAIL"); return []
        log_fn(f"[{map_name}] MCP bound → settle 40s")
        time.sleep(40)
        if "Assertion failed" in ue_log.read_text(errors="ignore"):
            log_fn(f"[{map_name}] UE_CRASHED"); return []

        mcp = MCPClient(host="127.0.0.1", port=slot["mcp_port"])

        # Get PS Z for ground snapping
        ps_z = 100.0
        import re as _re
        r = mcp.execute_python(
            "import unreal\nworld=unreal.get_editor_subsystem(unreal.UnrealEditorSubsystem).get_editor_world()\n"
            "ps=unreal.GameplayStatics.get_all_actors_of_class(world,unreal.PlayerStart)\n"
            "if ps: l=ps[0].get_actor_location(); print('PSZ=%.1f'%float(l.z))\n"
            "else: print('PSZ=100')", timeout=30)
        for ln in (r.get("result", {}).get("python_logs", []) or []):
            cl = _re.sub(r'^\[\s*\d+\]\s*', '', ln)
            if cl.startswith("PSZ="):
                ps_z = float(cl[4:]); break

        ground_z = ps_z - 100  # bottom-pivot actors spawn at ground surface

        rng = random.Random(hash(map_name) + 8888)
        spawned = []

        # Spawn one BP at each PN goal position
        for i, pn in enumerate(pn_tasks):
            gp  = pn["goal_position"]
            gx, gy = gp["x"], gp["y"]
            gz  = ground_z
            cat, desc, bp_path = rng.choice(BP_OPTIONS)
            lbl = f"ObjNavTarget_{map_name[:8]}_{i:03d}"

            spawn_s = (
                f"import unreal\n"
                f"eas=unreal.get_editor_subsystem(unreal.EditorActorSubsystem)\n"
                f"bp=unreal.load_object(None,'{bp_path}')\n"
                f"if bp:\n"
                f"    a=eas.spawn_actor_from_class(bp,unreal.Vector({gx},{gy},{gz}))\n"
                f"    if a: a.set_actor_label('{lbl}'); print('SPAWNED {lbl}')\n"
                f"    else: print('FAILED {lbl}')\n"
                f"else: print('NOCLASS {bp_path}')\n"
            )
            r2 = mcp.execute_python(spawn_s, timeout=20)
            logs2 = r2.get("result", {}).get("python_logs", []) if isinstance(r2, dict) else []
            ok = any("SPAWNED" in _re.sub(r'^\[\s*\d+\]\s*', '', l) for l in logs2)

            on_id = pn["episode_id"].replace("_pn_", "_on_").replace("_obj_pn_", "_on_")
            on_records.append({
                "episode_id":         on_id,
                "map":                map_name,
                "umap_path":          f"/Game/DiverseMaps50/{map_name}",
                "split":              split,
                "task_type":          "objectnav",
                "start_position":     pn["start_position"],
                "start_heading_deg":  pn["start_heading_deg"],
                "target_category":    cat,
                "target_description": desc,
                "target_actor_label": lbl if ok else None,
                "gt_path":            pn["gt_path"],
                "geodesic_distance_cm": pn["geodesic_distance_cm"],
                "success_criteria":   pn.get("success_criteria",
                                             {"success_distance_cm": SUCCESS_DIST_CM, "max_steps": 60}),
            })
            spawned.append(ok)

        n_ok = sum(spawned)
        log_fn(f"[{map_name}] spawned {n_ok}/{len(pn_tasks)} objects")

        # Save map with spawned objects
        r3 = mcp.execute_python(
            "import unreal\n"
            "world=unreal.get_editor_subsystem(unreal.UnrealEditorSubsystem).get_editor_world()\n"
            "pkg=world.get_outer()\n"
            "try: pkg.mark_package_dirty()\nexcept: pass\n"
            "ok=unreal.EditorLoadingAndSavingUtils.save_map(world,'')\n"
            "print('SAVE_OK='+str(ok))", timeout=60)
        logs3 = r3.get("result", {}).get("python_logs", []) if isinstance(r3, dict) else []
        saved = any("SAVE_OK=True" in _re.sub(r'^\[\s*\d+\]\s*', '', l) for l in logs3)
        log_fn(f"[{map_name}] save={'OK' if saved else 'FAIL'}, {len(on_records)} ON records")
        return on_records

    except Exception as exc:
        log_fn(f"[{map_name}] EXCEPTION: {exc}")
        return []
    finally:
        ue_proc.kill()
        try: ue_proc.wait(timeout=15)
        except: pass
        subprocess.run(["fuser", "-k", f"{slot['mcp_port']}/tcp"], capture_output=True)
        time.sleep(8)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--parallel", type=int, default=4)
    ap.add_argument("--resume",   action="store_true")
    args = ap.parse_args()

    DATASET.mkdir(parents=True, exist_ok=True)
    done_dir = pathlib.Path("/tmp/koe_on")
    done_dir.mkdir(parents=True, exist_ok=True)

    todo = get_maps_needing_on()
    if args.resume:
        before = len(todo)
        todo = {m: v for m, v in todo.items()
                if not (done_dir / m / "done").exists()}
        print(f"RESUME: skipping {before - len(todo)} already-done")

    print(f"Maps needing ON tasks: {len(todo)}")
    for m, (split, tasks) in sorted(todo.items()):
        print(f"  [{split}] {m}: {len(tasks)} ON tasks to generate")

    write_lock = threading.Lock()
    def append_on(split, records):
        with write_lock:
            p = DATASET / f"{split}_objectnav.jsonl"
            with open(p, "a") as f:
                for r in records:
                    f.write(json.dumps(r, ensure_ascii=False) + "\n")

    log_lock = threading.Lock()
    def log_fn(msg):
        with log_lock:
            print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)

    jobq: queue.Queue = queue.Queue()
    for m, (split, tasks) in todo.items():
        jobq.put((m, split, tasks))

    results = []
    rl = threading.Lock()

    def worker(slot_idx):
        slot = SLOTS[slot_idx]
        while True:
            try:
                map_name, split, pn_tasks = jobq.get_nowait()
            except queue.Empty:
                return
            on_recs = gen_on_for_map(slot, map_name, split, pn_tasks, log_fn)
            if on_recs:
                append_on(split, on_recs)
                (done_dir / map_name).mkdir(parents=True, exist_ok=True)
                (done_dir / map_name / "done").write_text(str(len(on_recs)))
            with rl:
                results.append((map_name, len(on_recs)))
                status = "OK" if on_recs else "FAIL"
                log_fn(f"[{map_name}] {status} on={len(on_recs)}")
            jobq.task_done()

    threads = [threading.Thread(target=worker, args=(i,), daemon=True)
               for i in range(min(args.parallel, len(SLOTS)))]
    for t in threads: t.start()
    for t in threads: t.join()

    n_ok = sum(1 for _, n in results if n > 0)
    total_on = sum(n for _, n in results)
    print(f"\n=== DONE {n_ok}/{len(results)} maps, {total_on} total ON records added ===")

    print("\nFinal JSONL counts:")
    for split in ("train", "test"):
        for task in ("pointnav", "objectnav"):
            p = DATASET / f"{split}_{task}.jsonl"
            n = sum(1 for _ in open(p)) if p.exists() else 0
            print(f"  {split}_{task}: {n}")


if __name__ == "__main__":
    main()
