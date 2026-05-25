"""Generate ObjectNav records with LLM descriptions using Claude SDK.

For each PointNav task:
1. Boot UE in editor mode with the map
2. Get scene graph (actor labels + positions from editor)
3. Spawn a BP object at the PN goal position
4. Call Claude with scene graph context to generate a natural-language hint
5. Save map (with spawned objects)
6. Write ObjectNav JSONL: same start/GT path as PN, goal = LLM description

Usage:
  cd simworld_studio_workspace
  python3 -u scripts/gen_objectnav_with_llm.py --parallel 4
  python3 -u scripts/gen_objectnav_with_llm.py --parallel 4 --resume
"""
from __future__ import annotations

import argparse, json, math, pathlib, queue, random, re, subprocess, sys
import threading, time
from typing import Optional

_THIS = pathlib.Path(__file__).resolve()
WORKSPACE = _THIS.parent.parent
sys.path.insert(0, str(WORKSPACE))

UE_EDITOR   = "/data/koe/UE_5.3.2/Engine/Binaries/Linux/UnrealEditor"
DATASET_DIR = WORKSPACE / "datasets" / "diverse50"
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
    ("fire_hydrant", "/Game/CityDatabase/blueprints/BP_Hydrant.BP_Hydrant_C"),
    ("trash_bin",    "/Game/CityDatabase/blueprints/BP_Trash_bin_a.BP_Trash_bin_a_C"),
    ("tree",         "/Game/CityDatabase/blueprints/BP_Tree3.BP_Tree3_C"),
    ("bench",        "/Game/CityDatabase/blueprints/BP_Table.BP_Table_C"),
    ("traffic_cone", "/Game/CityDatabase/blueprints/BP_RoadCone.BP_RoadCone_C"),
]

# Canonical names for LLM context
BP_CANONICAL = {
    "fire_hydrant": ("fire hydrant", ["hydrant", "red hydrant"]),
    "trash_bin":    ("trash bin",    ["bin", "rubbish bin", "waste bin"]),
    "tree":         ("tree",         ["large tree", "green tree"]),
    "bench":        ("bench",        ["park bench", "wooden bench"]),
    "traffic_cone": ("traffic cone", ["orange cone", "safety cone"]),
}

SKIP_ACTOR_PREFIXES = (
    "PlayerStart", "NavMeshBoundsVolume", "DirectionalLight", "SkyLight",
    "SkyAtmosphere", "ExponentialHeightFog", "PostProcessVolume", "WorldSettings",
    "GameplayDebugger", "WorldDataLayers", "LevelBounds", "AbstractNavData",
    "BuoyancyManager", "DefaultPhysics", "GameNetwork", "GameSession",
    "GameState", "MassVisualizer", "ParticleEvent", "ChaosDebug",
    "PlayerCamera", "HUD_", "GameplayDebugge",
)


def load_jsonl(p: pathlib.Path):
    return [json.loads(l) for l in open(p) if l.strip()] if p.exists() else []


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


def strip_ue_prefix(line: str) -> str:
    return re.sub(r'^\[\s*\d+\]\s*', '', line)


def mcp_logs(r) -> list:
    if isinstance(r, dict):
        inner = r.get("result") or r
        if isinstance(inner, dict):
            return inner.get("python_logs", []) or []
    return []


def _build_prompt(map_name, scene_actors, tasks) -> str:
    """Build the description generation prompt."""
    # Filter to meaningful named objects only (skip Submesh_*, Arena_Env_*, etc.)
    SKIP_ALSO = ("Submesh_", "Arena_Env_", "Atmospheric", "Sky ", "InstancedFoliage",
                 "LightmassImp", "SphereReflection", "WorldDataLayers", "LevelBounds")
    notable = []
    for (label, ax, ay) in scene_actors:
        if any(label.startswith(p) for p in SKIP_ACTOR_PREFIXES):
            continue
        if any(s in label for s in SKIP_ALSO):
            continue
        if len(label) > 30 and label.count('_') > 3:  # skip cryptic internal names
            continue
        notable.append(f"  {label} at ({ax/100:.0f}m, {ay/100:.0f}m)")
    # Cap at 30 most relevant objects
    if len(notable) > 30:
        notable = notable[:30] + [f"  ... ({len(notable)-30} more nearby)"]
    scene_text = "\n".join(notable) if notable else "  (open area, no major landmarks)"

    task_lines = []
    for i, (_, sx, sy, gx, gy, cat) in enumerate(tasks):
        canonical, aliases = BP_CANONICAL.get(cat, (cat, [cat]))
        dist_m = math.sqrt((gx-sx)**2 + (gy-sy)**2) / 100
        angle  = math.degrees(math.atan2(gy-sy, gx-sx))
        dirs   = ["east","northeast","north","northwest","west","southwest","south","southeast"]
        dir_str = dirs[round(angle/45) % 8]
        task_lines.append(
            f"{i+1}. Target: {canonical} (aka: {', '.join(aliases)})"
            f" at ({gx/100:.0f}m,{gy/100:.0f}m)"
            f" — roughly {dist_m:.0f}m to the {dir_str} of start"
        )
    tasks_text = "\n".join(task_lines)

    return f"""You are writing robot navigation hints for an ObjectNav benchmark in the scene "{map_name}".

Scene objects (label, position relative to scene origin):
{scene_text}

For each target below, write EXACTLY ONE navigation hint sentence that:
1. Names the target object (use the canonical name or an alias)
2. References 1-2 nearby SCENE OBJECTS as spatial landmarks (choose objects close to the target)
3. Does NOT mention exact coordinates or compass bearings
4. Is under 30 words
5. Varies phrasing style across tasks

Targets:
{tasks_text}

Return ONLY a valid JSON array of {len(tasks)} strings, one per task, in order.
Example format: ["Find the hydrant near the tree on the east side.", "The trash bin sits beside the building entrance.", ...]"""


def _call_claude_batch(prompt: str, n: int, env: dict, timeout=90) -> list[str]:
    """Call claude CLI via stdin, return list of n descriptions."""
    r = subprocess.run(
        ["claude", "--output-format", "text", "--dangerously-skip-permissions"],
        input=prompt, capture_output=True, text=True, timeout=timeout, env=env,
    )
    raw = r.stdout.strip()
    match = re.search(r'\[.*\]', raw, re.DOTALL)
    if match:
        descs = json.loads(match.group(0))
        if isinstance(descs, list) and len(descs) == n:
            return [str(d) for d in descs]
    lines = [l.strip().strip('"').strip(',') for l in raw.split('\n') if l.strip()]
    return [l for l in lines if len(l) > 10][:n]


def generate_descriptions_claude(
    map_name: str,
    scene_actors: list,
    tasks: list,
) -> list[str]:
    """Generate descriptions via `claude` CLI in batches of 8 tasks."""
    env = dict(__import__("os").environ)
    for k in ("CLAUDECODE", "CLAUDE_CODE_SSE_PORT", "CLAUDE_CODE_ENTRYPOINT"):
        env.pop(k, None)

    BATCH = 8
    all_descs = []

    for start in range(0, len(tasks), BATCH):
        batch = tasks[start:start + BATCH]
        prompt = _build_prompt(map_name, scene_actors, batch)
        try:
            descs = _call_claude_batch(prompt, len(batch), env, timeout=90)
        except Exception:
            descs = []

        # Pad if short
        while len(descs) < len(batch):
            cat = batch[len(descs)][5]
            canonical, _ = BP_CANONICAL.get(cat, (cat, []))
            descs.append(f"Find the {canonical}.")
        all_descs.extend(descs[:len(batch)])

    return all_descs


def process_map(slot: dict, map_name: str, split: str,
                pn_tasks: list, log_fn) -> list:
    """Boot UE, spawn objects, get scene graph, generate LLM descriptions, save map."""
    from gym_env.mcp_client import MCPClient

    uproject = slot["uproject"]
    ue_map   = f"/Game/DiverseMaps50/{map_name}"
    work_dir = pathlib.Path(f"/tmp/koe_on2/{map_name}")
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

        # 1. Get PS Z and scene actors from editor
        scene_script = """
import unreal, json
eas   = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)
world = unreal.get_editor_subsystem(unreal.UnrealEditorSubsystem).get_editor_world()
ps    = unreal.GameplayStatics.get_all_actors_of_class(world, unreal.PlayerStart)
ps_z  = float(ps[0].get_actor_location().z) if ps else 100.0
actors = []
for a in eas.get_all_level_actors():
    try:
        l = a.get_actor_location()
        actors.append([a.get_actor_label(), float(l.x), float(l.y), float(l.z)])
    except: pass
print("PSZ=%.1f" % ps_z)
print("ACTORS=" + json.dumps(actors))
"""
        r = mcp.execute_python(scene_script, timeout=60)
        ps_z       = 100.0
        scene_actors = []
        for ln in mcp_logs(r):
            cl = strip_ue_prefix(ln)
            if cl.startswith("PSZ="):
                ps_z = float(cl[4:])
            elif cl.startswith("ACTORS="):
                try:
                    raw_actors = json.loads(cl[7:])
                    scene_actors = [(a[0], a[1], a[2]) for a in raw_actors]
                except Exception:
                    pass

        ground_z = ps_z - 100
        log_fn(f"[{map_name}] {len(scene_actors)} scene actors, ps_z={ps_z:.0f}")

        # 2. Spawn BP objects at each PN goal position
        rng       = random.Random(hash(map_name) + 9999)
        task_info = []   # (idx, sx, sy, gx, gy, category, label)

        for i, pn in enumerate(pn_tasks):
            gp  = pn["goal_position"]
            gx, gy = gp["x"], gp["y"]
            cat, bp_path = rng.choice(BP_OPTIONS)
            lbl = f"ObjT_{map_name[:6]}_{i:03d}"

            spawn_s = (
                f"import unreal\n"
                f"eas=unreal.get_editor_subsystem(unreal.EditorActorSubsystem)\n"
                f"bp=unreal.load_object(None,'{bp_path}')\n"
                f"if bp:\n"
                f"    a=eas.spawn_actor_from_class(bp,unreal.Vector({gx},{gy},{ground_z}))\n"
                f"    if a: a.set_actor_label('{lbl}'); print('SPAWNED')\n"
                f"    else: print('FAIL')\n"
                f"else: print('NOCLASS')\n"
            )
            r2 = mcp.execute_python(spawn_s, timeout=20)
            logs2 = mcp_logs(r2)
            ok = any("SPAWNED" in strip_ue_prefix(l) for l in logs2)

            sp = pn["start_position"]
            task_info.append((i, sp["x"], sp["y"], gx, gy, cat, lbl, ok))

        n_spawned = sum(1 for t in task_info if t[7])
        log_fn(f"[{map_name}] spawned {n_spawned}/{len(task_info)} objects")

        # 3. Save map with spawned objects
        r3 = mcp.execute_python(
            "import unreal\n"
            "world=unreal.get_editor_subsystem(unreal.UnrealEditorSubsystem).get_editor_world()\n"
            "pkg=world.get_outer()\n"
            "try: pkg.mark_package_dirty()\nexcept: pass\n"
            "ok=unreal.EditorLoadingAndSavingUtils.save_map(world,'')\n"
            "print('SAVE_OK='+str(ok))\n", timeout=60)
        saved = any("SAVE_OK=True" in strip_ue_prefix(l) for l in mcp_logs(r3))
        log_fn(f"[{map_name}] map save={'OK' if saved else 'FAIL'}")

        # 4. Generate LLM descriptions via Claude SDK
        log_fn(f"[{map_name}] calling Claude for {len(task_info)} descriptions")
        tasks_for_llm = [(t[0], t[1], t[2], t[3], t[4], t[5]) for t in task_info]
        try:
            descriptions = generate_descriptions_claude(map_name, scene_actors, tasks_for_llm)
        except Exception as exc:
            log_fn(f"[{map_name}] LLM failed ({exc}), using fallback descriptions")
            canonical_map = {cat: BP_CANONICAL.get(cat, (cat,[]))[0] for cat,_ in BP_OPTIONS}
            descriptions = [f"Find the {canonical_map.get(t[5], t[5])}."
                            for t in task_info]

        # 5. Build ON records
        for i, pn in enumerate(pn_tasks):
            t = task_info[i]
            on_id = pn["episode_id"].replace("_pn_", "_on_").replace("_obj_pn_", "_on_")
            desc = descriptions[i] if i < len(descriptions) else f"Find the {t[5]}."
            on_records.append({
                "episode_id":          on_id,
                "map":                 map_name,
                "umap_path":           f"/Game/DiverseMaps50/{map_name}",
                "split":               split,
                "task_type":           "objectnav",
                "start_position":      pn["start_position"],
                "start_heading_deg":   pn["start_heading_deg"],
                "target_category":     t[5],
                "target_description":  desc,
                "target_actor_label":  t[6],
                "target_spawned":      t[7],
                "gt_path":             pn["gt_path"],
                "geodesic_distance_cm": pn["geodesic_distance_cm"],
                "success_criteria":    pn.get("success_criteria",
                                              {"success_distance_cm": SUCCESS_DIST_CM, "max_steps": 60}),
            })

        log_fn(f"[{map_name}] generated {len(on_records)} ON records")
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
    ap.add_argument("--resume",   action="store_true",
                    help="Skip maps already processed (have done marker)")
    ap.add_argument("--dry-run",  action="store_true",
                    help="Print plan without executing")
    args = ap.parse_args()

    import logging
    logging.basicConfig(level=logging.WARNING)

    DATASET_DIR.mkdir(parents=True, exist_ok=True)
    done_dir = pathlib.Path("/tmp/koe_on2")
    done_dir.mkdir(parents=True, exist_ok=True)

    # Delete existing ObjectNav JSONLs and rebuild from scratch
    if not args.resume:
        for split in ("train", "test"):
            p = DATASET_DIR / f"{split}_objectnav.jsonl"
            if p.exists():
                p.unlink()
                print(f"Deleted {p.name}")

    # Build work queue: one entry per map (all its PN tasks)
    import collections
    todo: dict[str, tuple] = {}  # map_name → (split, [pn_tasks])
    for split in ("train", "test"):
        for rec in load_jsonl(DATASET_DIR / f"{split}_pointnav.jsonl"):
            m = rec["map"]
            if m not in todo:
                todo[m] = (split, [])
            todo[m][1].append(rec)  # type: ignore

    if args.resume:
        before = len(todo)
        todo = {m: v for m, v in todo.items()
                if not (done_dir / m / "done").exists()}
        print(f"RESUME: skipping {before - len(todo)} already-done")

    print(f"Maps to process: {len(todo)}")
    if args.dry_run:
        for m, (split, tasks) in sorted(todo.items()):
            print(f"  [{split}] {m}: {len(tasks)} ON tasks")
        return

    write_lock = threading.Lock()
    def append_on(split, records):
        with write_lock:
            with open(DATASET_DIR / f"{split}_objectnav.jsonl", "a") as f:
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
            on_recs = process_map(slot, map_name, split, pn_tasks, log_fn)
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

    n_ok    = sum(1 for _, n in results if n > 0)
    total   = sum(n for _, n in results)
    print(f"\n=== DONE {n_ok}/{len(results)} maps, {total} ON records ===")
    print("\nFinal counts:")
    for split in ("train", "test"):
        for task in ("pointnav", "objectnav"):
            p = DATASET_DIR / f"{split}_{task}.jsonl"
            n = sum(1 for _ in open(p)) if p.exists() else 0
            print(f"  {split}_{task}: {n}")


if __name__ == "__main__":
    main()
