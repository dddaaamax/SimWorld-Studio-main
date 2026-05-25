"""DiverseMaps50 evaluation: PointNav + ObjectNav with Qwen3.5-27B.

Runs both settings in parallel (4 UE instances) using ghost-mode agents.
One map per UE slot; 20 ghost agents per wave (or 2 waves of 10).

Usage:
  cd simworld_studio_workspace
  python -m experiments.diverse50_eval.run_eval --setting pointnav --split test
  python -m experiments.diverse50_eval.run_eval --setting objectnav --split test
  python -m experiments.diverse50_eval.run_eval --setting pointnav --split train --resume
  python -m experiments.diverse50_eval.run_eval --setting both --split test --parallel 4

Resume: rerun the same command; completed episodes are skipped automatically.
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
from datetime import datetime, timezone
from typing import Optional

_THIS = pathlib.Path(__file__).resolve()
WORKSPACE = _THIS.parent.parent.parent
sys.path.insert(0, str(WORKSPACE))

UE_EDITOR = os.environ.get("UE_EDITOR", "/data/koe/UE_5.3.2/Engine/Binaries/Linux/UnrealEditor")
# NOTE: unrealcv plugin was patched to read config from
# <uproject_dir>/Saved/unrealcv.ini (per-instance) instead of the shared
# engine binary dir.  Each slot writes its own ini → no cross-process lock needed.
DATASET   = pathlib.Path(os.environ.get("EVAL_DATASET",
                                        str(WORKSPACE / "datasets" / "diverse50")))
RESULTS   = pathlib.Path(os.environ.get("EVAL_RESULTS",
                                        str(WORKSPACE / "results" / "diverse50_eval")))

LLM_MODEL    = "qwen"
LLM_MODEL_ID = "Qwen3.5-27B"
# Multiple Qwen endpoints, round-robin across slots.  Each UE slot is
# pinned to exactly one endpoint for its whole lifetime (sticky) so
# vLLM's prefix cache can reuse the system+L3 prefix across all 10
# ghosts in a wave.  Pure per-request RR would destroy that cache.
# With 8 slots over 5 endpoints (slot_idx_abs % 5):
#   PN: slot 0→ep0, 1→ep1, 2→ep2, 3→ep3
#   ON: slot 4→ep4, 5→ep0, 6→ep1, 7→ep2
# Resulting load: ep0/1/2 = 2 slots each (20 concurrent), ep3/4 = 1 slot (10).
# Override via EVAL_LLM_URLS=url1,url2,... env var (comma-separated).
LLM_URLS_DEFAULT = (
    "http://132.239.95.133:8000/v1,"
    "http://132.239.95.133:8001/v1,"
    "http://132.239.95.133:8002/v1,"
    "http://132.239.95.133:8003/v1,"
    "http://132.239.95.15:8007/v1"
)
LLM_API_KEY  = "EMPTY"
MEMORY_BACKEND = "hierarchical"
MAX_STEPS    = 40          # 40 × 2s walking ≈ 160m budget; covers 99% of dataset
VISION_DEPTH = 1           # only current step's image; old frames replaced with "[image omitted]"
WAVE_SIZE    = 10          # ghost agents per wave — 2 slots/endpoint × 10 = 20 concurrent/endpoint
                           # (paired with 5-turn history truncation to cap KV pressure)
MAX_WAVE     = 20          # try single wave first, split if agent count > this
MAX_EUCLIDEAN_M = 120      # filter out tasks where straight-line distance > 120m (4.7% of train)

SLOTS = [
    {"mcp_port": 55558, "ucv_port": 9010, "gpu": 0, "uproject": "/data/koe/simworld_studio_inst_0/SimWorld.uproject", "ue_bin": "/data/koe/ue_launch_inst_0/UnrealEditor"},
    {"mcp_port": 55560, "ucv_port": 9011, "gpu": 1, "uproject": "/data/koe/simworld_studio_inst_1/SimWorld.uproject", "ue_bin": "/data/koe/ue_launch_inst_1/UnrealEditor"},
    {"mcp_port": 55574, "ucv_port": 9012, "gpu": 3, "uproject": "/data/koe/simworld_studio_inst_2/SimWorld.uproject", "ue_bin": "/data/koe/ue_launch_inst_2/UnrealEditor"},
    {"mcp_port": 55564, "ucv_port": 9013, "gpu": 4, "uproject": "/data/koe/simworld_studio_inst_3/SimWorld.uproject", "ue_bin": "/data/koe/ue_launch_inst_3/UnrealEditor"},
    {"mcp_port": 55576, "ucv_port": 9014, "gpu": 5, "uproject": "/data/koe/simworld_studio_inst_8/SimWorld.uproject", "ue_bin": "/data/koe/ue_launch_inst_4/UnrealEditor"},
    {"mcp_port": 55568, "ucv_port": 9015, "gpu": 6, "uproject": "/data/koe/simworld_studio_inst_5/SimWorld.uproject", "ue_bin": "/data/koe/ue_launch_inst_5/UnrealEditor"},
    {"mcp_port": 55570, "ucv_port": 9016, "gpu": 7, "uproject": "/data/koe/simworld_studio_inst_6/SimWorld.uproject", "ue_bin": "/data/koe/ue_launch_inst_6/UnrealEditor"},
    {"mcp_port": 55572, "ucv_port": 9017, "gpu": 4, "uproject": "/data/koe/simworld_studio_inst_7/SimWorld.uproject", "ue_bin": "/data/koe/ue_launch_inst_7/UnrealEditor"},
    # Test-only slots (used while train is running on slots 0-7)
    {"mcp_port": 55600, "ucv_port": 9024, "gpu": 0, "uproject": "/data/koe/simworld_studio_inst_9/SimWorld.uproject",  "ue_bin": "/data/koe/ue_launch_inst_8/UnrealEditor"},
    {"mcp_port": 55601, "ucv_port": 9025, "gpu": 1, "uproject": "/data/koe/simworld_studio_inst_10/SimWorld.uproject", "ue_bin": "/data/koe/ue_launch_inst_9/UnrealEditor"},
    {"mcp_port": 55602, "ucv_port": 9026, "gpu": 3, "uproject": "/data/koe/simworld_studio_inst_11/SimWorld.uproject", "ue_bin": "/data/koe/ue_launch_inst_10/UnrealEditor"},
    {"mcp_port": 55603, "ucv_port": 9027, "gpu": 5, "uproject": "/data/koe/simworld_studio_inst_12/SimWorld.uproject", "ue_bin": "/data/koe/ue_launch_inst_11/UnrealEditor"},
]

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Episode conversion: our JSONL → NavigationEpisode
# ---------------------------------------------------------------------------

def jsonl_to_episode(rec: dict):
    """Convert a record from our JSONL dataset to NavigationEpisode."""
    from nav_task.episode import (
        NavigationEpisode, Position, ReferencePath, SuccessCriteria,
        EvaluationMetrics, WorldConfig, RewardConfig, ObjectGoal, ObjectViewPoint,
    )

    sp = rec["start_position"]
    gp = rec.get("goal_position") or (rec.get("gt_path") or [{}])[-1]
    sc = rec.get("success_criteria", {})
    geo = rec.get("geodesic_distance_cm", 0.0)

    waypoints = tuple(
        Position(x=float(wp["x"]), y=float(wp["y"]), node_type=wp.get("node_type","navmesh"))
        for wp in rec.get("gt_path", [])
    )
    if not waypoints:
        s = Position(x=float(sp["x"]), y=float(sp["y"]), node_type="navmesh")
        g = Position(x=float(gp.get("x",0)), y=float(gp.get("y",0)), node_type="navmesh")
        waypoints = (s, g)

    task_type = rec.get("task_type", "pointnav")
    # ObjectNav: object_category = category (fire_hydrant etc.),
    # target_description stored as object_goal.object_category so env can use it
    obj_cat = rec.get("target_category") if task_type == "objectnav" else None
    obj_goal  = None
    if task_type == "objectnav":
        gx = float(gp.get("x", 0)); gy = float(gp.get("y", 0))
        obj_goal = ObjectGoal(
            object_id       = rec.get("target_actor_label", "ObjTarget"),
            object_type     = rec.get("target_category", "object"),
            object_category = rec.get("target_description",
                                      rec.get("target_category","object")),
            position    = Position(x=gx, y=gy, node_type="object"),
            view_points = (),
        )

    return NavigationEpisode(
        episode_id      = rec["episode_id"],
        seed            = 0,
        world           = WorldConfig(map_file=rec.get("umap_path", "unknown"),
                                      coordinate_unit="cm"),
        start_position  = Position(x=float(sp["x"]), y=float(sp["y"]),
                                   node_type=sp.get("node_type","navmesh")),
        goal_position   = Position(x=float(gp.get("x",0)), y=float(gp.get("y",0)),
                                   node_type=gp.get("node_type","navmesh")),
        reference_path  = ReferencePath(waypoints=waypoints,
                                        shortest_path_length_cm=float(geo)),
        success_criteria= SuccessCriteria(
            success_distance_cm = float(sc.get("success_distance_cm", 200.0)),
            max_steps           = int(sc.get("max_steps", MAX_STEPS)),
        ),
        evaluation_metrics = EvaluationMetrics(
            success_distance_cm = float(sc.get("success_distance_cm", 200.0)),
            shortest_path_length_cm = float(geo),
        ),
        generated_at    = rec.get("generated_at", datetime.now(timezone.utc).isoformat()),
        task_type       = task_type,
        object_category = obj_cat,
        object_goal     = obj_goal,
    )


def load_jsonl(path: pathlib.Path) -> list:
    recs = [json.loads(l) for l in open(path) if l.strip()]
    return recs


# ---------------------------------------------------------------------------
# UE boot helpers
# ---------------------------------------------------------------------------

def wait_for_mcp(log_path: pathlib.Path, port: int, timeout=180) -> bool:
    bind_ok   = f"UnrealMCPBridge: Server started on 127.0.0.1:{port}"
    bind_fail = f"Failed to bind listener socket to 127.0.0.1:{port}"
    t0 = time.time()
    while time.time() - t0 < timeout:
        if log_path.exists():
            txt = log_path.read_text(errors="ignore")
            if bind_ok in txt:   return True
            if bind_fail in txt or "Assertion failed" in txt or "Signal 11 caught" in txt:
                return False
        time.sleep(2)
    return False


def find_ucv_port(ue_pid: int, mcp_port: int, timeout=40) -> Optional[int]:
    """Find actual UnrealCV TCP port by inode matching in /proc/{pid}/fd."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            fd_dir = pathlib.Path(f"/proc/{ue_pid}/fd")
            owned_inodes = set()
            for fd_link in fd_dir.iterdir():
                try:
                    target = str(fd_link.resolve())
                    if "socket:" in target:
                        inode = target.split("socket:[")[1].rstrip("]")
                        owned_inodes.add(inode)
                except Exception:
                    pass
            if owned_inodes:
                net_tcp = pathlib.Path(f"/proc/{ue_pid}/net/tcp").read_text()
                for line in net_tcp.splitlines()[1:]:
                    parts = line.split()
                    if len(parts) < 10 or parts[3] != "0A":
                        continue
                    if parts[9] not in owned_inodes:
                        continue
                    port = int(parts[1].split(":")[1], 16)
                    if port > 1024 and port != mcp_port:
                        return port
        except Exception:
            pass
        time.sleep(1)
    return None


# ---------------------------------------------------------------------------
# Per-map episode runner
# ---------------------------------------------------------------------------

def run_map_episodes(
    slot: dict,
    map_name: str,
    umap_path: str,
    episodes: list,   # list of NavigationEpisode
    setting: str,
    run_dir: pathlib.Path,
    memory,
    llm,
    log_fn,
    resume: bool,
) -> list:
    """Boot UE for one map, run all ghost-mode episodes, return result dicts."""
    from gym_env.batch_runner import run_wave
    from gym_env.mcp_client import MCPClient
    from gym_env.ucv_client import UCVClient

    uproject = slot["uproject"]
    work_dir = pathlib.Path(f"/tmp/koe_eval/{setting}/{map_name}")
    work_dir.mkdir(parents=True, exist_ok=True)
    ue_log = work_dir / "ue.log"

    # Resume: skip already-done episodes
    map_dir = run_dir / map_name
    map_dir.mkdir(parents=True, exist_ok=True)
    done_ids = set()
    all_results = []
    if resume:
        for sp in map_dir.rglob("summary.json"):
            try:
                s = json.loads(sp.read_text())
                eid = s.get("episode_id")
                if eid:
                    done_ids.add(eid)
                    all_results.append(s)
            except Exception:
                pass
    todo = [ep for ep in episodes if ep.episode_id not in done_ids]
    if not todo:
        log_fn(f"[{map_name}] all {len(episodes)} episodes already done, skip")
        return all_results

    ucv_port = slot["ucv_port"]
    log_fn(f"[{map_name}] BOOT mcp={slot['mcp_port']} ucv={ucv_port} ({len(todo)} episodes)")

    # Per-instance ini path: plugin reads <uproject_dir>/Saved/unrealcv.ini
    # 320x240 → 4× less pixels vs 640x480 (ReadPixels is sync GPU→CPU = slow)
    uproject_dir = pathlib.Path(uproject).parent
    ucv_ini_path = uproject_dir / "Saved" / "unrealcv.ini"
    ucv_ini_path.parent.mkdir(parents=True, exist_ok=True)
    ucv_ini_path.write_text(
        f"[UnrealCV.Core]\nPort={ucv_port}\nWidth=320\nHeight=240\nFOV=90\n"
        f"EnableInput=True\nEnableRightEye=False\n"
    )

    ue_proc = subprocess.Popen(
        [UE_EDITOR, uproject, umap_path,
         f"-MCPPort={slot['mcp_port']}",
         "-Unattended", "-NOSPLASH", "-NOSOUND", "-Messaging",
         "-ResX=1280", "-ResY=720", "-FPSMAX=15", "-RenderOffScreen",
         f"-graphicsadapter={slot['gpu']}", "-log"],
        stdout=open(ue_log, "w"), stderr=subprocess.STDOUT,
    )

    try:
        if not wait_for_mcp(ue_log, slot["mcp_port"], timeout=180):
            log_fn(f"[{map_name}] MCP_BIND_FAIL"); return all_results
        log_fn(f"[{map_name}] MCP bound → settle 40s")
        time.sleep(40)
        if "Assertion failed" in ue_log.read_text(errors="ignore"):
            log_fn(f"[{map_name}] UE_CRASHED"); return all_results

        mcp = MCPClient(host="127.0.0.1", port=slot["mcp_port"])

        # NavMesh NOT needed: env uses EuclideanNavigationInterface, GT paths pre-computed.
        # Just start PIE and connect UCVClient.
        mcp.start_pie(wait_seconds=15.0)

        # ucv_port is known (written to ini before spawn), no need to search
        log_fn(f"[{map_name}] connecting UCVClient on port {ucv_port}")
        ucv = UCVClient(host="127.0.0.1", port=ucv_port)
        for attempt in range(15):
            try:
                ucv.connect()
                break
            except Exception as exc:
                log_fn(f"[{map_name}] connect attempt {attempt+1}: {exc}")
                time.sleep(3)
        else:
            log_fn(f"[{map_name}] UCV_CONNECT_FAIL"); return all_results
        log_fn(f"[{map_name}] UCVClient connected")

        # Run ghost waves
        wave_size = min(WAVE_SIZE, len(todo))
        n_waves = (len(todo) + wave_size - 1) // wave_size
        for wi in range(n_waves):
            wave_eps = todo[wi*wave_size:(wi+1)*wave_size]
            is_first = wi == 0
            is_last  = wi == n_waves - 1
            log_fn(f"[{map_name}] wave {wi+1}/{n_waves}: {len(wave_eps)} agents")
            batch_dir = map_dir / f"w{wi}"
            try:
                results, _ = run_wave(
                    ucv, mcp, llm, wave_eps,
                    max_steps     = MAX_STEPS,
                    vision_depth  = VISION_DEPTH,
                    memory        = memory,
                    batch_dir     = batch_dir,
                    save_frames   = False,
                    capture_rgb   = True,
                    image_kind    = "rgb",
                    reuse_agents  = not is_first,
                    skip_destroy  = not is_last,
                )
                all_results.extend(results)
                log_fn(f"[{map_name}] wave {wi+1} done: "
                       f"SR={sum(1 for r in results if r.get('SR',0)>0)}/{len(results)}")
            except Exception as exc:
                log_fn(f"[{map_name}] wave {wi+1} EXCEPTION: {exc}")

        return all_results

    except Exception as exc:
        log_fn(f"[{map_name}] EXCEPTION: {exc}")
        return all_results
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
        time.sleep(8)


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------

def _summarize(results: list) -> dict:
    n = len(results)
    if not n:
        return {"n": 0, "SR": 0, "SPL": 0, "SoftSPL": 0, "nDTW": 0}
    return {
        "n": n,
        "SR":       sum(r.get("SR",0) for r in results) / n,
        "SPL":      sum(r.get("SPL",0) for r in results) / n,
        "SoftSPL":  sum(r.get("SoftSPL",0) for r in results) / n,
        "nDTW":     sum(r.get("nDTW",0) for r in results) / n,
        "avg_steps":sum(r.get("steps",0) for r in results) / n,
    }


def run_setting(setting: str, split: str, parallel: int, resume: bool):
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-5s | %(message)s",
    )

    from gym_env.llm import make_llm
    from gym_env.memory import build_memory, ReadOnlyMemory

    # Load episodes
    jsonl_path = DATASET / f"{split}_{setting.replace('nav','nav')}.jsonl"
    if not jsonl_path.exists():
        print(f"ERROR: {jsonl_path} not found"); return

    recs = load_jsonl(jsonl_path)
    n_loaded = len(recs)

    # Filter: drop tasks where straight-line start->goal exceeds the
    # MAX_EUCLIDEAN_M budget.  At MAX_STEPS × 2s × 200cm/s = 240m
    # nominal budget per episode, anything over 120m needs ≥50% of the
    # budget burned just on forward motion — leaves no room for turns
    # or backtracking, so the success rate is essentially 0 and just
    # adds noise to SR.  Also catches dataset-generation artifacts
    # where the stored geodesic_distance_cm is much smaller than the
    # actual euclidean distance (~10% of records).
    import math as _math
    def _within_budget(r):
        sp = r.get("start_position") or {}
        gp = r.get("goal_position") or {}
        if not gp: gp = (r.get("gt_path") or [{}])[-1]
        dx = float(sp.get("x", 0)) - float(gp.get("x", 0))
        dy = float(sp.get("y", 0)) - float(gp.get("y", 0))
        return _math.sqrt(dx*dx + dy*dy) / 100.0 <= MAX_EUCLIDEAN_M
    recs_kept = [r for r in recs if _within_budget(r)]
    n_dropped = n_loaded - len(recs_kept)
    recs = recs_kept
    print(f"Loaded {n_loaded} {setting} {split} records; "
          f"filtered {n_dropped} ({100*n_dropped/max(1,n_loaded):.1f}%) "
          f"with euclidean > {MAX_EUCLIDEAN_M}m → {len(recs)} kept")

    # Group by map
    map_groups: dict = {}
    for rec in recs:
        m = rec["map"]
        map_groups.setdefault(m, []).append(rec)

    # Run dir
    run_id = f"{setting}_{split}_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    if resume:
        # find latest run with same setting+split
        existing = sorted(RESULTS.glob(f"{setting}_{split}_*"))
        if existing:
            run_id = existing[-1].name
            print(f"Resuming from {run_id}")
    run_dir = RESULTS / run_id
    run_dir.mkdir(parents=True, exist_ok=True)

    # Multi-endpoint: each pair of UE slots gets its own vLLM server so the
    # 5 concurrent ghost requests from one slot stay in one server's prefix
    # cache.  slot->endpoint mapping is slot_idx // 2 (see SLOTS comment).
    urls_raw = os.environ.get("EVAL_LLM_URLS", LLM_URLS_DEFAULT)
    llm_urls = [u.strip() for u in urls_raw.split(",") if u.strip()]
    print(f"LLM endpoints ({len(llm_urls)}):")
    for i, u in enumerate(llm_urls):
        # Show which absolute slots will hit this endpoint via slot % len(urls).
        owners = [s for s in range(8) if s % len(llm_urls) == i]
        print(f"  urls[{i}] = {u}  (slots {owners})")

    # Save meta
    meta = {
        "setting": setting, "split": split,
        "model": LLM_MODEL_ID, "llm_urls": llm_urls,
        "memory": MEMORY_BACKEND, "max_steps": MAX_STEPS,
        "wave_size": WAVE_SIZE, "vision_depth": VISION_DEPTH,
        "n_maps": len(map_groups),
        "n_episodes": len(recs), "created_at": datetime.now(timezone.utc).isoformat(),
    }
    (run_dir / "meta.json").write_text(json.dumps(meta, indent=2))
    print(f"Results → {run_dir}")
    print(f"Maps: {len(map_groups)}, Episodes: {len(recs)}, Parallel: {parallel}")

    def make_slot_llm(slot_idx_abs: int):
        # Sticky per-slot: same endpoint for the slot's whole lifetime so
        # vLLM prefix-cache can reuse the system+L3 prefix across the
        # 10 ghosts in a wave (and across waves on the same map/setting).
        url = llm_urls[slot_idx_abs % len(llm_urls)]
        c = make_llm(LLM_MODEL, model=LLM_MODEL_ID, base_url=url,
                     api_key=LLM_API_KEY)
        c._text_action_mode = True  # bypass tool-call API, parse action from text
        return c

    # Memory persist dir: shared per setting so train writes and test reads the same store.
    # HierarchicalMemory needs a DIRECTORY (it keeps L2 / L3 / episode_count as separate files).
    # Can be overridden via EVAL_MEMORY_DIR (e.g. to point at a snapshot for test).
    memory_dir = pathlib.Path(os.environ.get(
        "EVAL_MEMORY_DIR", str(RESULTS / f"memory_{setting}")))
    memory_dir.mkdir(parents=True, exist_ok=True)

    # Memory backend can be overridden via EVAL_MEMORY_BACKEND — useful to run
    # a no-memory baseline ("none") while the trained run keeps "hierarchical".
    memory_backend = os.environ.get("EVAL_MEMORY_BACKEND", MEMORY_BACKEND)
    print(f"Memory backend: {memory_backend}  dir: {memory_dir}")

    # Memory's own LLM (for L3 distill) uses the first configured endpoint.
    memory = build_memory(
        memory_backend,
        agent_id=f"diverse50_{setting}",
        config={"persist_dir": str(memory_dir)},
        llm_model=LLM_MODEL_ID,
        llm_base_url=llm_urls[0],
        llm_api_key=LLM_API_KEY,
    )

    if split == "test":
        # Test phase: load trained memory, freeze it (no updates during eval)
        l2_file = memory_dir / "l2_episodic.json"
        if not l2_file.exists():
            print(f"WARNING: no trained memory found at {memory_dir}")
            print("  Run train split first: --split train")
        else:
            print(f"Loaded trained memory from {memory_dir}")
        from gym_env.memory import ReadOnlyMemory
        memory = ReadOnlyMemory(memory)
    else:
        # Train phase: memory is writable and updated after each episode
        print(f"Train mode: memory will be updated → {memory_dir}")

    log_lock = threading.Lock()
    def log_fn(msg):
        with log_lock:
            print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)

    all_map_summaries = {}
    all_results_lock = threading.Lock()

    jobq: queue.Queue = queue.Queue()
    for map_name, map_recs in sorted(map_groups.items()):
        episodes = [jsonl_to_episode(r) for r in map_recs]
        umap_path = map_recs[0]["umap_path"]
        jobq.put((map_name, umap_path, episodes))

    n_total = sum(len(v) for v in map_groups.values())
    n_done  = 0
    n_done_lock = threading.Lock()

    def worker(slot_idx):
        nonlocal n_done
        slot = SLOTS[slot_idx]
        # Per-slot LLM client so all 5 ghosts in this slot hit the same
        # vLLM endpoint (prefix cache locality).  Constructing it here
        # instead of at module scope keeps one client per worker thread.
        slot_llm = make_slot_llm(slot_idx)
        while True:
            try:
                map_name, umap_path, episodes = jobq.get_nowait()
            except queue.Empty:
                return
            results = run_map_episodes(
                slot, map_name, umap_path, episodes,
                setting, run_dir, memory, slot_llm, log_fn, resume,
            )
            summary = _summarize(results)
            with all_results_lock:
                all_map_summaries[map_name] = summary
                n_done += len(results)
            # Write per-map summary
            (run_dir / map_name / "map_summary.json").write_text(
                json.dumps({"map": map_name, **summary}, indent=2))
            # Periodic overall summary
            with all_results_lock:
                overall = _summarize([r for ms in [] for r in []])
            log_fn(f"[{map_name}] MAP DONE  SR={summary['SR']:.2f}  SPL={summary['SPL']:.3f}"
                   f"  n={summary['n']}  [{n_done}/{n_total} total]")
            # Write running overall summary
            _write_overall(run_dir, all_map_summaries, setting, split)
            jobq.task_done()

    slot_offset = int(os.environ.get("EVAL_SLOT_OFFSET", "0"))
    slot_indices = list(range(slot_offset, slot_offset + min(parallel, len(SLOTS) - slot_offset)))
    threads = [threading.Thread(target=worker, args=(idx,), daemon=True)
               for idx in slot_indices]
    for t in threads: t.start()
    for t in threads: t.join()

    _write_overall(run_dir, all_map_summaries, setting, split)
    print(f"\n=== FINAL {setting} {split} ===")
    all_r = []
    for ms in all_map_summaries.values():
        pass  # already aggregated
    overall = _compute_overall(run_dir)
    for k, v in overall.items():
        if isinstance(v, float):
            print(f"  {k}: {v:.4f}")
        else:
            print(f"  {k}: {v}")


def _write_overall(run_dir, map_summaries, setting, split):
    flat = []
    for ms in map_summaries.values():
        flat.extend([ms] * ms.get("n", 0))  # weight by episode count
    n = sum(ms.get("n", 0) for ms in map_summaries.values())
    if n == 0:
        return
    overall = {
        "setting": setting, "split": split,
        "n_maps_done": len(map_summaries),
        "n_episodes": n,
        "SR":      sum(ms.get("SR",0)*ms.get("n",0) for ms in map_summaries.values()) / n,
        "SPL":     sum(ms.get("SPL",0)*ms.get("n",0) for ms in map_summaries.values()) / n,
        "SoftSPL": sum(ms.get("SoftSPL",0)*ms.get("n",0) for ms in map_summaries.values()) / n,
        "nDTW":    sum(ms.get("nDTW",0)*ms.get("n",0) for ms in map_summaries.values()) / n,
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "per_map": map_summaries,
    }
    (run_dir / "running_summary.json").write_text(json.dumps(overall, indent=2))


def _compute_overall(run_dir):
    summary_path = run_dir / "running_summary.json"
    if summary_path.exists():
        return json.loads(summary_path.read_text())
    return {}


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--setting", choices=["pointnav","objectnav","both"], default="pointnav")
    ap.add_argument("--split",   choices=["train","test","both"], default="test")
    ap.add_argument("--parallel",type=int, default=4)
    ap.add_argument("--resume",  action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    RESULTS.mkdir(parents=True, exist_ok=True)

    settings = ["pointnav","objectnav"] if args.setting == "both" else [args.setting]
    splits   = ["train","test"]         if args.split == "both"   else [args.split]

    if args.dry_run:
        for setting in settings:
            for split in splits:
                p = DATASET / f"{split}_{setting}.jsonl"
                recs = load_jsonl(p) if p.exists() else []
                maps = set(r["map"] for r in recs)
                print(f"{setting} {split}: {len(recs)} episodes, {len(maps)} maps")
        return

    for setting in settings:
        for split in splits:
            print(f"\n{'='*60}")
            print(f"  {setting.upper()} — {split.upper()}")
            print(f"{'='*60}")
            run_setting(setting, split, args.parallel, args.resume)


if __name__ == "__main__":
    main()
