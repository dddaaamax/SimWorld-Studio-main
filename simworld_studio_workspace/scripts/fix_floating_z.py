"""Post-process pass: fix floating objects in DiverseMaps50.

Directly calls UE Python via MCPClient — no LLM involved.
Each map: boot UE, rebuild nav, delete agents, move large buildings to background,
fix floating Z, rebuild nav again, save.

Usage:
  cd simworld_studio_workspace
  python3 -u scripts/fix_floating_z.py --parallel 8
  python3 -u scripts/fix_floating_z.py --parallel 8 --resume
  python3 -u scripts/fix_floating_z.py --only map_05_middleeast_native,map_07_castleriver_remix
"""
import argparse
import json
import os
import pathlib
import queue
import shutil
import subprocess
import sys
import threading
import time

_THIS = pathlib.Path(__file__).resolve()
WORKSPACE = _THIS.parent.parent
sys.path.insert(0, str(WORKSPACE / "gym_env"))
from mcp_client import MCPClient  # noqa: E402

UE_EDITOR = os.environ.get("UE_EDITOR", "/data/koe/UE_5.3.2/Engine/Binaries/Linux/UnrealEditor")
MAPS_DIR = pathlib.Path("/data/koe/simworld_studio_projects/Content/DiverseMaps50")
DONE_DIR = WORKSPACE / "arena_output" / "diverse50_fixedz"
FLOAT_THRESHOLD = 40   # UU above navmesh surface = floating

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

# ---------- Python scripts sent to UE directly ----------

_STEP_DELETE_AGENTS = """
import unreal
eas = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)
PATTERNS = ("BP_Agent","BP_DeliveryMan","BP_RobotDog","BP_Drone",
            "DeliveryMan","RobotDog","SimAgent","NavAgent",
            "BP_Character","BP_NPC","BP_Humanoid")
deleted = []
for a in eas.get_all_level_actors():
    try:
        n = a.get_actor_label()
        if any(p.lower() in n.lower() for p in PATTERNS):
            eas.destroy_actor(a); deleted.append(n)
    except: pass
print(f"AGENTS_DELETED={len(deleted)}")
"""

_STEP_FIX = f"""
import unreal, math, random
eas = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)
world = unreal.get_editor_subsystem(unreal.UnrealEditorSubsystem).get_editor_world()

SKIP_CLASSES = {{"PlayerStart","NavMeshBoundsVolume","DirectionalLight","SkyLight",
    "SkyAtmosphere","ExponentialHeightFog","PostProcessVolume",
    "ReflectionCapture","AtmosphericFog","LightmassImportanceVolume","WorldSettings"}}
SKIP_PFX = ("Floor","Ground","Plane","Landscape","Terrain",
    "SM_Floor","SM_Ground","SM_Pavement","SM_Sidewalk","Arena_Env","WorldDataLayers","LevelBounds")
BG_MIN, BG_MAX = 2500.0, 5000.0

ps_actors = unreal.GameplayStatics.get_all_actors_of_class(world, unreal.PlayerStart)
if not ps_actors:
    print("MOVED_TO_BG=0")
    print("FIXED_Z=0")
    print("NO_PLAYERSTART=True")
    raise SystemExit(0)

ps = ps_actors[0].get_actor_location()
ps_z = ps.z
ground_z = ps_z - 100

# Safety: PS at Z≈0 means objects at Z=0 are already on ground — skip to avoid pushing underground.
# Use abs() so scenes at large negative Z (e.g. CastleRiver at -22435) are handled correctly.
if abs(ps_z) < 50:
    print(f"MOVED_TO_BG=0")
    print(f"FIXED_Z=0")
    print(f"PS_Z={{ps_z:.1f}} GROUND_Z={{ground_z:.1f}} SKIP=ps_z_too_low")
    raise SystemExit(0)

# Use navmesh projection to find actual ground Z at PS position.
# This handles cases where PS was placed at non-standard height (e.g. 242 above Z=0 ground).
# Falls back to ps_z-100 if nav projection returns zero vector.
nav_pt = unreal.NavigationSystemV1.project_point_to_navigation(
    world, unreal.Vector(ps.x, ps.y, ps_z), None, None)
if nav_pt and not (nav_pt.x == 0 and nav_pt.y == 0 and nav_pt.z == 0):
    ground_z = nav_pt.z
    print("GROUND_Z_FROM_NAV=%.1f" % ground_z)
else:
    print("GROUND_Z_FROM_FALLBACK=%.1f" % ground_z)

all_actors = eas.get_all_level_actors()
max_dist = 3000.0
for a in all_actors:
    try:
        l = a.get_actor_location(); n = a.get_actor_label()
        if any(n.startswith(p) for p in SKIP_PFX): continue
        if "BP_Building_" in n: continue
        d = math.sqrt((l.x-ps.x)**2+(l.y-ps.y)**2)
        if d > max_dist: max_dist = d
    except: pass
bg_max = min(max_dist*0.9, BG_MAX)

random.seed(99)
moved_bg, fixed_z = [], []

for actor in all_actors:
    try:
        cls = actor.get_class().get_name(); name = actor.get_actor_label()
        if cls in SKIP_CLASSES: continue
        if any(name.startswith(p) for p in SKIP_PFX): continue
        loc = actor.get_actor_location()

        # --- Large buildings inside navmesh → push to background ring ---
        if "BP_Building_" in name:
            dist = math.sqrt((loc.x-ps.x)**2+(loc.y-ps.y)**2)
            if dist < BG_MIN:
                angle = math.atan2(loc.y-ps.y, loc.x-ps.x)
                r = random.uniform(BG_MIN, bg_max)
                nx, ny = ps.x+r*math.cos(angle), ps.y+r*math.sin(angle)
                actor.set_actor_location(unreal.Vector(nx, ny, ground_z), False, False)
                moved_bg.append(name)
            continue

        # --- Fix floating: actor.z ≈ ps_z means it was spawned at PS pivot height ---
        # Only fix BP_ actors (coding-agent spawns). SM_* are template geometry at correct Z.
        if not (name.startswith("BP_") or name.startswith("bp_")): continue

        # Check 1: actor at Z≈ps_z (spawned at PS pivot height — original bug)
        # Check 2: actor at Z≈ps_z-100 but nav says real ground is much lower (second-pass fix)
        # Use per-actor nav projection to find actual ground under each actor.
        nav_here = unreal.NavigationSystemV1.project_point_to_navigation(
            world, unreal.Vector(loc.x, loc.y, loc.z + 500), None, None)
        if nav_here and not (nav_here.x == 0 and nav_here.y == 0 and nav_here.z == 0):
            actual_ground = nav_here.z
        else:
            actual_ground = ground_z  # fallback to PS-derived ground_z

        # Fix if actor is > 40 UU above actual ground (floating)
        diff = loc.z - actual_ground
        if diff > 40:
            actor.set_actor_location(unreal.Vector(loc.x, loc.y, actual_ground), False, False)
            fixed_z.append(f"{{name}}:{{loc.z:.0f}}->{{actual_ground:.0f}}(diff {{diff:.0f}})")
    except: pass

print(f"MOVED_TO_BG={{len(moved_bg)}}")
print(f"FIXED_Z={{len(fixed_z)}}")
print(f"PS_Z={{ps_z:.1f}} GROUND_Z={{ground_z:.1f}}")
for x in fixed_z[:30]: print(f"  FIX:{{x}}")
"""

_STEP_REACHABLE = """
import unreal
world = unreal.get_editor_subsystem(unreal.UnrealEditorSubsystem).get_editor_world()
ps = unreal.GameplayStatics.get_all_actors_of_class(world, unreal.PlayerStart)
center = ps[0].get_actor_location() if ps else unreal.Vector(0,0,0)
hits = 0
for _ in range(20):
    try:
        pt = unreal.NavigationSystemV1.get_random_reachable_point_in_radius(world, center, 15000)
        if pt and not (pt.x==0 and pt.y==0 and pt.z==0): hits += 1
    except: pass
print(f"REACHABLE={hits}/20")
"""

_STEP_SAVE = """
import unreal
world = unreal.get_editor_subsystem(unreal.UnrealEditorSubsystem).get_editor_world()
pkg = world.get_outer()
try: pkg.mark_package_dirty()
except: pass
# save_map("") saves to the map's current path (in-place overwrite)
ok = unreal.EditorLoadingAndSavingUtils.save_map(world, "")
if not ok:
    ok = unreal.EditorLoadingAndSavingUtils.save_map(world, world.get_path_name())
if not ok:
    try: ok = unreal.EditorAssetLibrary.save_asset(world.get_path_name(), only_if_is_dirty=False)
    except: pass
print(f"SAVE_OK={ok}")
"""

# ---------- helpers ----------

def wait_for_mcp(log_path: pathlib.Path, port: int, timeout: int = 180) -> bool:
    bind_ok = f"UnrealMCPBridge: Server started on 127.0.0.1:{port}"
    bind_fail = f"Failed to bind listener socket to 127.0.0.1:{port}"
    start = time.time()
    while time.time() - start < timeout:
        if log_path.exists():
            txt = log_path.read_text(errors="ignore")
            if bind_ok in txt: return True
            if bind_fail in txt or "Assertion failed" in txt or "Signal 11 caught" in txt:
                return False
        time.sleep(2)
    return False


def py_logs(result: dict) -> list[str]:
    try:
        return result["result"]["python_logs"]
    except Exception:
        return []


def parse_val(logs: list[str], key: str, default="?") -> str:
    for line in logs:
        if f"{key}=" in line:
            return line.split(f"{key}=", 1)[1].strip().split()[0]
    return default


def fix_one_map(slot: dict, map_name: str, log) -> dict:
    uproject = slot["uproject"]
    ue_map_path = f"/Game/DiverseMaps50/{map_name}"
    work_dir = pathlib.Path(f"/tmp/koe_fixz/{map_name}")
    work_dir.mkdir(parents=True, exist_ok=True)
    ue_log = work_dir / "ue.log"

    log(f"[{map_name}] BOOT port={slot['mcp_port']}")
    ue_proc = subprocess.Popen(
        [UE_EDITOR, uproject, ue_map_path,
         f"-MCPPort={slot['mcp_port']}", f"-UnrealCVPort={slot['ucv_port']}",
         "-Unattended", "-NOSPLASH", "-NOSOUND", "-Messaging",
         "-ResX=1280", "-ResY=720", "-FPSMAX=15", "-RenderOffScreen",
         f"-graphicsadapter={slot['gpu']}", "-log"],
        stdout=open(ue_log, "w"), stderr=subprocess.STDOUT,
    )

    stats = {"name": map_name, "ok": False,
             "agents_deleted": "?", "moved_to_bg": "?",
             "fixed_z": "?", "reachable_final": "?", "reason": ""}
    try:
        if not wait_for_mcp(ue_log, slot["mcp_port"], timeout=180):
            stats["reason"] = "MCP_BIND_FAIL"; return stats
        log(f"[{map_name}] MCP bound → settle 40s")
        time.sleep(40)
        if "Assertion failed" in ue_log.read_text(errors="ignore"):
            stats["reason"] = "UE_CRASHED"; return stats

        mcp = MCPClient(port=slot["mcp_port"], timeout=120)

        # 0 rebuild navmesh so nav projection works for ground_z detection
        mcp.execute_python(
            "import unreal\nworld=unreal.get_editor_subsystem(unreal.UnrealEditorSubsystem).get_editor_world()\n"
            "unreal.SystemLibrary.execute_console_command(world,'RebuildNavigation')\nprint('NAV_OK')",
            timeout=30)
        time.sleep(25)

        # 1 delete agents
        r = mcp.execute_python(_STEP_DELETE_AGENTS, timeout=90)
        stats["agents_deleted"] = parse_val(py_logs(r), "AGENTS_DELETED", "0")

        # 2 fix Z using per-actor nav projection + move large buildings to bg
        r = mcp.execute_python(_STEP_FIX, timeout=240)
        logs3 = py_logs(r)
        stats["moved_to_bg"] = parse_val(logs3, "MOVED_TO_BG", "0")
        stats["fixed_z"]     = parse_val(logs3, "FIXED_Z", "0")
        # log PS_Z for debugging
        ps_z_line = next((l for l in logs3 if "PS_Z=" in l), "")
        if ps_z_line: log(f"[{map_name}] {ps_z_line}")

        # 3 sample reachability (no nav rebuild needed — we didn't add obstacles)
        r = mcp.execute_python(_STEP_REACHABLE, timeout=90)
        stats["reachable_final"] = parse_val(py_logs(r), "REACHABLE", "?")

        # 4 save
        r = mcp.execute_python(_STEP_SAVE, timeout=90)
        # Check if NO_PLAYERSTART was reported in the fix step output
        if any("NO_PLAYERSTART=True" in l for l in logs3):
            stats["ok"] = True
            stats["reason"] = "NO_PLAYERSTART_SKIP"
            return stats

        save_ok = parse_val(py_logs(r), "SAVE_OK") in ("True", "true")
        no_changes = stats["moved_to_bg"] == "0" and stats["fixed_z"] == "0" and stats["agents_deleted"] == "0"
        stats["ok"] = save_ok or no_changes
        if not stats["ok"]: stats["reason"] = "NO_SAVE_OK"

        # write agent_log for --resume detection
        log_txt = (f"AGENTS_DELETED={stats['agents_deleted']}\n"
                   f"MOVED_TO_BG={stats['moved_to_bg']}\n"
                   f"FIXED_Z={stats['fixed_z']}\n"
                   f"REACHABLE={stats['reachable_final']}\n"
                   f"SAVE_OK={save_ok}\n")
        (work_dir / "agent.log").write_text(log_txt)
        return stats

    except Exception as e:
        stats["reason"] = f"EXCEPTION:{e}"; return stats
    finally:
        ue_proc.kill()
        try: ue_proc.wait(timeout=15)
        except: pass
        subprocess.run(["fuser", "-k", f"{slot['mcp_port']}/tcp"], capture_output=True)
        time.sleep(8)
        done_dir = DONE_DIR / map_name
        done_dir.mkdir(parents=True, exist_ok=True)
        try:
            if ue_log.exists(): shutil.copy2(ue_log, done_dir / "ue_log.txt")
            wl = work_dir / "agent.log"
            if wl.exists(): shutil.copy2(wl, done_dir / "agent_log.txt")
        except: pass


# ---------- orchestrator ----------

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--parallel", type=int, default=8)
    ap.add_argument("--resume", action="store_true")
    ap.add_argument("--only", type=str, default=None)
    args = ap.parse_args()

    DONE_DIR.mkdir(parents=True, exist_ok=True)
    all_maps = sorted(p.stem for p in MAPS_DIR.glob("*.umap"))
    if args.only:
        all_maps = [m for m in all_maps if m in args.only.split(",")]
    if args.resume:
        before = len(all_maps)
        all_maps = [m for m in all_maps if not (DONE_DIR / m / "agent_log.txt").exists()]
        print(f"RESUME: skipping {before-len(all_maps)} already-fixed")
    print(f"Queue: {len(all_maps)} maps, parallel={args.parallel}")

    log_lock = threading.Lock()
    def log(msg):
        with log_lock:
            print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)

    jobq: queue.Queue = queue.Queue()
    for m in all_maps: jobq.put(m)
    results, results_lock = [], threading.Lock()

    def worker(slot_idx):
        slot = SLOTS[slot_idx]
        while True:
            try: name = jobq.get_nowait()
            except queue.Empty: return
            try: res = fix_one_map(slot, name, log)
            except Exception as e:
                res = {"name": name, "ok": False, "agents_deleted": "?",
                       "moved_to_bg": "?", "fixed_z": "?",
                       "reachable_final": "?", "reason": f"EXCEPTION:{e}"}
            with results_lock:
                results.append(res)
                status = "OK  " if res["ok"] else "FAIL"
                log(f"[{name}] {status}  agents={res['agents_deleted']} "
                    f"bg={res['moved_to_bg']} fixZ={res['fixed_z']} "
                    f"reach={res['reachable_final']}  {res['reason']}")
            jobq.task_done()

    threads = [threading.Thread(target=worker, args=(i,), daemon=True)
               for i in range(min(args.parallel, len(SLOTS)))]
    for t in threads: t.start()
    for t in threads: t.join()

    n_ok = sum(1 for r in results if r["ok"])
    print(f"\n=== DONE {n_ok}/{len(results)} ===")
    print(f"{'MAP':<45} {'ST':>4} {'DEL':>4} {'BG':>4} {'FIX':>4} {'REACH':>6}")
    print("-" * 72)
    total_bg, total_fz, total_ag = 0, 0, 0
    for r in sorted(results, key=lambda x: x["name"]):
        m = "OK" if r["ok"] else "FAIL"
        print(f"  {r['name']:<43} {m:>4} "
              f"{r['agents_deleted']:>4} {r['moved_to_bg']:>4} "
              f"{r['fixed_z']:>4} {r['reachable_final']:>6}  {r.get('reason','')}")
        try: total_ag += int(r['agents_deleted'])
        except: pass
        try: total_bg += int(r['moved_to_bg'])
        except: pass
        try: total_fz += int(r['fixed_z'])
        except: pass
    print(f"\nTOTAL: agents_deleted={total_ag}  moved_to_bg={total_bg}  fixed_z={total_fz}")

    summary = DONE_DIR / f"summary_{time.strftime('%Y%m%d_%H%M%S')}.json"
    summary.write_text(json.dumps(results, indent=2))
    print(f"summary: {summary}")


if __name__ == "__main__":
    main()
