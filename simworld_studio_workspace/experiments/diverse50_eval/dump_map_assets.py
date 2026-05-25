"""One-shot dump of per-map actor labels for the DiverseMaps50 dataset.

Boots UE headless on each map under /Game/DiverseMaps50/, queries
`eas.get_all_level_actors()` via MCP, and appends to
datasets/diverse50/map_assets.json as:
    { "<map_name>": ["<actor_label>", ...], ... }

The main visualize_dataset.py script picks the dump up automatically once
it exists and renders the asset-type violin plot against real actor classes.

Usage:
    # dump for all 54 maps found in datasets/diverse50 (sequential, ~25s/map)
    python3 dump_map_assets.py

    # dump only a subset (useful while eval is still running on some slots)
    python3 dump_map_assets.py --maps map_01_wintertown_native,map_08_cave_remix

    # pick a different MCP port (default 55690 — well outside the eval range)
    python3 dump_map_assets.py --mcp-port 55690 --ue-editor /path/to/UnrealEditor
"""
from __future__ import annotations

import argparse
import json
import os
import pathlib
import subprocess
import sys
import time

WORKSPACE = pathlib.Path(__file__).resolve().parent.parent.parent
DATASET_DIR = WORKSPACE / "datasets" / "diverse50"
OUT_FILE = DATASET_DIR / "map_assets.json"

DEFAULT_UE_EDITOR = os.environ.get("UE_EDITOR", "/data/koe/UE_5.3.2/Engine/Binaries/Linux/UnrealEditor")
DEFAULT_UPROJECT  = os.environ.get("UE_PROJECT", "/data/koe/simworld_studio_projects/SimWorld.uproject")
UE_EDITOR = DEFAULT_UE_EDITOR
UPROJECT  = DEFAULT_UPROJECT

sys.path.insert(0, str(WORKSPACE))


def discover_maps() -> list[str]:
    maps: set[str] = set()
    for f in ("train_pointnav.jsonl", "test_pointnav.jsonl",
              "train_objectnav.jsonl", "test_objectnav.jsonl"):
        p = DATASET_DIR / f
        if not p.exists():
            continue
        for line in open(p):
            if line.strip():
                maps.add(json.loads(line)["map"])
    return sorted(maps)


def wait_for_mcp(log_path: pathlib.Path, port: int, timeout: int = 180) -> bool:
    bind_ok  = f"UnrealMCPBridge: Server started on 127.0.0.1:{port}"
    bind_err = f"Failed to bind listener socket to 127.0.0.1:{port}"
    deadline = time.time() + timeout
    while time.time() < deadline:
        if log_path.exists():
            txt = log_path.read_text(errors="ignore")
            if bind_ok in txt:
                return True
            if bind_err in txt or "Assertion failed" in txt or "Signal 11 caught" in txt:
                return False
        time.sleep(2)
    return False


def query_actors(mcp, map_name: str) -> dict[str, int] | None:
    """Return {class_name: count} for actors on the currently-loaded map, or None.

    Classification happens on the UE side (strip trailing `_<digits>` auto-numbering,
    strip `_C` blueprint suffix) so the MCP reply is a compact dict per map rather
    than tens of thousands of label lines.
    """
    script = r"""
import unreal, re, json
eas = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)
SKIP = ("Floor","Ground","Plane","Landscape","Terrain","SM_Floor","SM_Ground",
        "SM_Pavement","SM_Sidewalk","Arena_Env","WorldDataLayers","LevelBounds",
        "AbstractNavData","NavMesh","RecastNavMesh","PlayerStart",
        "DirectionalLight","SkyLight","SkyAtmosphere","VolumetricCloud",
        "ExponentialHeightFog","PostProcessVolume","ReflectionCapture",
        "AtmosphericFog","LightmassImportanceVolume","WorldSettings",
        "CameraActor","SphereReflectionCapture","BoxReflectionCapture",
        "LensFlareSource","LightmassCharacterIndirectDetailVolume",
        "BrushShape","BrushComponent","NavMeshBoundsVolume")
counts = {}
total = 0
for a in eas.get_all_level_actors():
    try:
        lbl = a.get_actor_label()
        total += 1
        if any(s in lbl for s in SKIP):
            continue
        cls = re.sub(r"(_\\d+)+$", "", lbl)
        cls = re.sub(r"_C$", "", cls)
        if not cls:
            continue
        counts[cls] = counts.get(cls, 0) + 1
    except Exception:
        pass
print("DUMP_TOTAL=" + str(total))
print("DUMP_JSON=" + json.dumps(counts))
"""
    r = mcp.execute_python(script, timeout=120)
    logs = []
    if isinstance(r, dict):
        inner = r.get("result") or r
        if isinstance(inner, dict):
            logs = inner.get("python_logs", []) or []
    import re as _re
    for line in logs:
        clean = _re.sub(r"^\[\s*\d+\]\s*", "", line)
        if clean.startswith("DUMP_JSON="):
            try:
                import json as _json
                return _json.loads(clean[len("DUMP_JSON="):])
            except Exception:
                return None
    return None


def run_one_map(map_name: str, mcp_port: int, gpu: int, log_fn) -> dict[str, int] | None:
    from gym_env.mcp_client import MCPClient  # type: ignore

    ue_map_path = f"/Game/DiverseMaps50/{map_name}"
    work_dir = pathlib.Path(f"/tmp/koe_asset_dump/{map_name}")
    work_dir.mkdir(parents=True, exist_ok=True)
    ue_log = work_dir / "ue.log"

    log_fn(f"[{map_name}] BOOT port={mcp_port} gpu={gpu}")
    ue_proc = subprocess.Popen(
        [UE_EDITOR, UPROJECT, ue_map_path,
         f"-MCPPort={mcp_port}",
         "-Unattended", "-NOSPLASH", "-NOSOUND", "-Messaging",
         "-ResX=640", "-ResY=480", "-FPSMAX=10", "-RenderOffScreen",
         f"-graphicsadapter={gpu}", "-log"],
        stdout=open(ue_log, "w"), stderr=subprocess.STDOUT,
    )
    try:
        if not wait_for_mcp(ue_log, mcp_port, timeout=180):
            log_fn(f"[{map_name}] MCP bind failed")
            return None
        time.sleep(20)  # let editor finish streaming sublevels

        mcp = MCPClient(host="127.0.0.1", port=mcp_port)
        counts = query_actors(mcp, map_name)
        if not counts:
            log_fn(f"[{map_name}] actor query returned nothing")
            return None
        log_fn(f"[{map_name}] got {sum(counts.values())} actors across {len(counts)} classes")
        return counts
    finally:
        try:
            ue_proc.terminate()
            ue_proc.wait(timeout=15)
        except Exception:
            ue_proc.kill()


def main() -> int:
    global UE_EDITOR  # noqa: PLW0603
    ap = argparse.ArgumentParser()
    ap.add_argument("--maps", default="",
                    help="comma-separated subset (default: all maps in dataset)")
    ap.add_argument("--mcp-port", type=int, default=55690)
    ap.add_argument("--gpu", type=int, default=0)
    ap.add_argument("--ue-editor", default=UE_EDITOR)
    args = ap.parse_args()
    UE_EDITOR = args.ue_editor

    maps = [m.strip() for m in args.maps.split(",") if m.strip()] or discover_maps()
    print(f"Dumping actors for {len(maps)} maps → {OUT_FILE}")

    out: dict[str, dict[str, int]] = {}
    if OUT_FILE.exists():
        try:
            raw = json.loads(OUT_FILE.read_text())
            # migrate old list-of-labels format to the new counts dict
            for k, v in raw.items():
                if isinstance(v, list):
                    import collections as _c
                    import re as _re
                    c = _c.Counter()
                    for lbl in v:
                        cls = _re.sub(r"(_\d+)+$", "", lbl)
                        cls = _re.sub(r"_C$", "", cls)
                        if cls:
                            c[cls] += 1
                    out[k] = dict(c)
                elif isinstance(v, dict):
                    out[k] = v
        except Exception:
            out = {}

    def log(msg: str) -> None:
        print(time.strftime("[%H:%M:%S]"), msg, flush=True)

    for i, m in enumerate(maps, 1):
        if m in out and out[m]:
            log(f"[{i}/{len(maps)}] {m} already dumped — skip")
            continue
        try:
            labels = run_one_map(m, args.mcp_port, args.gpu, log)
        except Exception as exc:
            log(f"[{i}/{len(maps)}] {m} raised {type(exc).__name__}: {exc} — skip")
            labels = None
        if labels:
            out[m] = labels
            OUT_FILE.write_text(json.dumps(out, indent=2))
            log(f"[{i}/{len(maps)}] saved incrementally ({len(out)} maps in file)")
        else:
            log(f"[{i}/{len(maps)}] FAILED — leaving blank, rerun later")

    print(f"\nDone. {len(out)} maps dumped to {OUT_FILE}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
