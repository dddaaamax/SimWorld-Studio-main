"""Batch-generate diverse-biome .umap files via coding agent (parallel-capable).

Each map = one UE boot with a specific template + one `claude -p` agent call that
edits the scene, adds NavMeshBoundsVolume + RebuildNavigation, and saves the
derivative to /Game/DiverseMaps50/<name>.

Usage:
  cd simworld_studio_workspace
  python3 scripts/generate_diverse_maps.py --count 10 --parallel 2

  # resume a partial batch
  python3 scripts/generate_diverse_maps.py --count 50 --parallel 2 --resume

Outputs:
  /data/koe/simworld_studio_projects/Content/DiverseMaps50/<name>.umap
  simworld_studio_workspace/arena_output/diverse50/<name>/
      ue_log.txt, agent_log.txt, overview.png (if saved)
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

# local imports
_THIS = pathlib.Path(__file__).resolve()
WORKSPACE = _THIS.parent.parent  # simworld_studio_workspace
sys.path.insert(0, str(WORKSPACE / "scripts"))
from prompts_diverse_50 import PROMPTS, build_prompt  # noqa: E402

# -------- paths / config --------
UE_EDITOR = os.environ.get("UE_EDITOR", "/data/koe/UE_5.3.2/Engine/Binaries/Linux/UnrealEditor")
UPROJECT = os.environ.get("UE_PROJECT", "/data/koe/simworld_studio_projects/SimWorld.uproject")
CONTENT_DIR = pathlib.Path("/data/koe/simworld_studio_projects/Content")
OUTPUT_UE_DIR = "/Game/DiverseMaps50"           # UE virtual path
OUTPUT_DISK_DIR = CONTENT_DIR / "DiverseMaps50"  # on-disk
SCREENS_DIR = WORKSPACE / "tmp" / "screens"      # where MCP take_screenshot lands
OUTPUT_COMPANION = WORKSPACE / "arena_output" / "diverse50"

SLOTS = [
    {"mcp_port": 55558, "ucv_port": 9010, "gpu": 0, "uproject": "/data/koe/simworld_studio_inst_0/SimWorld.uproject"},
    {"mcp_port": 55560, "ucv_port": 9011, "gpu": 1, "uproject": "/data/koe/simworld_studio_inst_1/SimWorld.uproject"},
    {"mcp_port": 55562, "ucv_port": 9012, "gpu": 3, "uproject": "/data/koe/simworld_studio_inst_2/SimWorld.uproject"},
    {"mcp_port": 55564, "ucv_port": 9013, "gpu": 4, "uproject": "/data/koe/simworld_studio_inst_3/SimWorld.uproject"},
    {"mcp_port": 55566, "ucv_port": 9014, "gpu": 5, "uproject": "/data/koe/simworld_studio_inst_4/SimWorld.uproject"},
    {"mcp_port": 55568, "ucv_port": 9015, "gpu": 6, "uproject": "/data/koe/simworld_studio_inst_5/SimWorld.uproject"},
    {"mcp_port": 55570, "ucv_port": 9016, "gpu": 7, "uproject": "/data/koe/simworld_studio_inst_6/SimWorld.uproject"},
    {"mcp_port": 55572, "ucv_port": 9017, "gpu": 4, "uproject": "/data/koe/simworld_studio_inst_7/SimWorld.uproject"},
]

# -------- helpers --------
def wait_for_my_mcp(log_path: pathlib.Path, port: int, timeout: int = 180) -> bool:
    """Wait until OUR UE's MCPBridge confirms bind to `port`.
    Returns False on bind failure or crash.
    """
    bind_ok = f"UnrealMCPBridge: Server started on 127.0.0.1:{port}"
    bind_fail = f"Failed to bind listener socket to 127.0.0.1:{port}"
    start = time.time()
    while time.time() - start < timeout:
        if log_path.exists():
            try:
                txt = log_path.read_text(errors="ignore")
            except Exception:
                txt = ""
            if bind_ok in txt:
                return True
            if bind_fail in txt:
                return False
            if "Assertion failed" in txt or "Signal 11 caught" in txt:
                return False
        time.sleep(2)
    return False


def make_mcp_config(work_dir: pathlib.Path, mcp_port: int) -> pathlib.Path:
    cfg_path = work_dir / "mcp.json"
    cfg = {
        "mcpServers": {
            "simworld": {
                "command": "node",
                "args": [str(WORKSPACE / "web" / "server" / "mcp-server.js")],
                "env": {
                    "UNREAL_HOST": "127.0.0.1",
                    "UNREAL_PORT": str(mcp_port),
                    "SIMWORLD_ASSETS_FILE": "assets_full.json",
                },
            }
        }
    }
    cfg_path.write_text(json.dumps(cfg, indent=2))
    return cfg_path


def run_one_map(slot: dict, entry: dict, log) -> dict:
    """Generate one map. Returns {name, ok, umap_bytes, reason}."""
    name = entry["name"]
    template_map = entry["template"]
    ue_save_path = f"{OUTPUT_UE_DIR}/{name}"
    disk_target = OUTPUT_DISK_DIR / f"{name}.umap"

    work_dir = pathlib.Path(f"/tmp/koe_diverse50/{name}")
    work_dir.mkdir(parents=True, exist_ok=True)
    ue_log = work_dir / "ue.log"
    agent_log = work_dir / "agent.log"

    prompt_text = build_prompt(entry, ue_save_path)
    (work_dir / "prompt.txt").write_text(prompt_text)

    uproject = slot.get("uproject", UPROJECT)
    log(f"[{name}] BOOT template={template_map} mcp={slot['mcp_port']}")
    ue_proc = subprocess.Popen(
        [
            UE_EDITOR, uproject, template_map,
            f"-MCPPort={slot['mcp_port']}",
            f"-UnrealCVPort={slot['ucv_port']}",
            "-Unattended", "-NOSPLASH", "-NOSOUND", "-Messaging",
            "-ResX=1280", "-ResY=720", "-FPSMAX=15", "-RenderOffScreen",
            f"-graphicsadapter={slot['gpu']}", "-log",
        ],
        stdout=open(ue_log, "w"),
        stderr=subprocess.STDOUT,
    )

    try:
        if not wait_for_my_mcp(ue_log, slot["mcp_port"], timeout=180):
            return {"name": name, "ok": False, "reason": "MCP_BIND_TIMEOUT_OR_CRASH"}
        log(f"[{name}] MCP bound → waiting 45s for map load settle")
        time.sleep(45)

        # crash guard: check log again before firing agent
        if "Assertion failed" in ue_log.read_text(errors="ignore"):
            return {"name": name, "ok": False, "reason": "UE_CRASHED_DURING_LOAD"}

        mcp_cfg = make_mcp_config(work_dir, slot["mcp_port"])

        env = os.environ.copy()
        for k in ("CLAUDECODE", "CLAUDE_CODE_SSE_PORT", "CLAUDE_CODE_ENTRYPOINT"):
            env.pop(k, None)

        log(f"[{name}] AGENT running (timeout 1800s)")
        t0 = time.time()
        try:
            subprocess.run(
                [
                    "claude", "-p", prompt_text,
                    "--output-format", "text",
                    "--dangerously-skip-permissions",
                    "--mcp-config", str(mcp_cfg),
                ],
                stdout=open(agent_log, "w"),
                stderr=subprocess.STDOUT,
                env=env,
                timeout=2400,
                check=False,
            )
        except subprocess.TimeoutExpired:
            log(f"[{name}] agent TIMEOUT")
            # umap may have been saved before timeout — treat as pass if it exists
            if disk_target.exists():
                sz = disk_target.stat().st_size
                log(f"[{name}] umap exists after timeout ({sz//1024} KB) → PASS")
                return {"name": name, "ok": True, "umap_bytes": sz, "reason": "TIMEOUT_BUT_SAVED"}
            return {"name": name, "ok": False, "reason": "AGENT_TIMEOUT"}
        dt = time.time() - t0
        log(f"[{name}] agent done in {dt:.0f}s → checking umap")

        if not disk_target.exists():
            return {"name": name, "ok": False, "reason": f"NO_UMAP at {disk_target}"}
        sz = disk_target.stat().st_size
        return {"name": name, "ok": True, "umap_bytes": sz}

    finally:
        ue_proc.kill()
        try:
            ue_proc.wait(timeout=15)
        except Exception:
            pass
        # force-release MCP port so next UE instance can bind immediately
        subprocess.run(["fuser", "-k", f"{slot['mcp_port']}/tcp"], capture_output=True)
        time.sleep(8)

        # Copy logs + overview screenshot to companion
        companion = OUTPUT_COMPANION / name
        companion.mkdir(parents=True, exist_ok=True)
        try:
            if ue_log.exists(): shutil.copy2(ue_log, companion / "ue_log.txt")
            if agent_log.exists(): shutil.copy2(agent_log, companion / "agent_log.txt")
            shutil.copy2(work_dir / "prompt.txt", companion / "prompt.txt")
            overview = SCREENS_DIR / f"{name}_overview.png"
            if overview.exists():
                shutil.copy2(overview, companion / "overview.png")
        except Exception as e:
            log(f"[{name}] companion copy err: {e}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--count", type=int, default=10)
    ap.add_argument("--start", type=int, default=0)
    ap.add_argument("--parallel", type=int, default=2)
    ap.add_argument("--resume", action="store_true")
    ap.add_argument("--only", type=str, default=None, help="Comma-separated names to run")
    args = ap.parse_args()

    SCREENS_DIR.mkdir(parents=True, exist_ok=True)
    OUTPUT_COMPANION.mkdir(parents=True, exist_ok=True)

    selected = PROMPTS[args.start:args.start + args.count]
    if args.only:
        only_names = set(args.only.split(","))
        selected = [p for p in PROMPTS if p["name"] in only_names]
    if args.resume:
        before = len(selected)
        selected = [p for p in selected if not (OUTPUT_DISK_DIR / f"{p['name']}.umap").exists()]
        print(f"RESUME: skipping {before - len(selected)} already-done")
    print(f"Queue: {len(selected)} maps, parallel={args.parallel}")
    for p in selected: print(f"  - {p['name']}")

    log_lock = threading.Lock()
    def log(msg):
        with log_lock:
            print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)

    jobq = queue.Queue()
    for p in selected: jobq.put(p)
    results = []
    results_lock = threading.Lock()

    def worker(slot_idx):
        slot = SLOTS[slot_idx]
        while True:
            try:
                entry = jobq.get_nowait()
            except queue.Empty:
                return
            try:
                res = run_one_map(slot, entry, log)
            except Exception as e:
                res = {"name": entry["name"], "ok": False, "reason": f"EXCEPTION: {e}"}
            with results_lock:
                results.append(res)
                log(f"[{entry['name']}] {'PASS' if res['ok'] else 'FAIL'} {res.get('reason','')}")
            jobq.task_done()

    threads = []
    for i in range(min(args.parallel, len(SLOTS))):
        t = threading.Thread(target=worker, args=(i,), daemon=True)
        t.start(); threads.append(t)
    for t in threads: t.join()

    # summary
    n_ok = sum(1 for r in results if r["ok"])
    print(f"\n=== DONE {n_ok}/{len(results)} ===")
    for r in sorted(results, key=lambda x: x["name"]):
        mark = "OK  " if r["ok"] else "FAIL"
        extra = f"{r.get('umap_bytes', 0) // 1024} KB" if r["ok"] else r.get("reason", "")
        print(f"  {mark} {r['name']}  {extra}")

    summary_path = OUTPUT_COMPANION / f"summary_{time.strftime('%Y%m%d_%H%M%S')}.json"
    summary_path.write_text(json.dumps(results, indent=2))
    print(f"\nsummary: {summary_path}")


if __name__ == "__main__":
    main()
