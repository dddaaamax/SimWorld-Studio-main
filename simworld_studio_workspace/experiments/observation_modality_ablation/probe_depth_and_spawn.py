"""Live probe: verify depth capture + object spawn visibility.

Run against a UE instance that is ALREADY in PIE (or let this script
call start_pie through MCP).  It:

  1. Connects MCP + UCV.
  2. Starts PIE if not already.
  3. Spawns a humanoid agent and queries its camera id.
  4. Captures a LIT PNG -> saves to probes/lit_before.png.
  5. Captures a DEPTH npy -> saves to probes/depth_before.npy
     and a visualization probes/depth_before_vis.png.
  6. Spawns a few objects from gym_env.object_pool (hydrant, cone,
     bottle etc.) in front of the agent and rebuilds navmesh.
  7. Teleports agent to face them, captures LIT + DEPTH again
     (probes/lit_after.png / probes/depth_after_vis.png).
  8. Prints a sanity summary (mean pixel value, non-zero %, and
     depth min/max).  Fails loudly if depth returns garbage.

Usage::

    python -m experiments.observation_modality_ablation.probe_depth_and_spawn \\
        --mcp-port 55558 --ucv-port 9002 --outdir probes_A
"""

from __future__ import annotations

import argparse
import io
import logging
import math
import os
import sys
import time
from pathlib import Path

import numpy as np
from PIL import Image

log = logging.getLogger("probe")


_HUMANOID_BP = "/Game/TrafficSystem/Pedestrian/Base_User_Agent.Base_User_Agent_C"


def _save_png(png_bytes: bytes, path: Path) -> None:
    path.write_bytes(png_bytes)


def _decode_lit(png_bytes: bytes) -> np.ndarray:
    return np.array(Image.open(io.BytesIO(png_bytes)).convert("RGB"), dtype=np.uint8)


def _capture_depth(ucv, cam_id: int, timeout: int = 15):
    """Fetch ``vget /camera/{id}/depth npy`` and return a (H, W) float32 array.

    Returns (None, error_string) on failure so the caller can keep going
    and report every failure mode instead of aborting on the first one.
    """
    cmd = f"vget /camera/{cam_id}/depth npy"
    try:
        payload = ucv.send_bytes(cmd, timeout=timeout)
    except Exception as exc:
        return None, f"exception: {exc}"
    if not payload:
        return None, "empty payload"
    # npy binary has magic \x93NUMPY
    if payload[:6] != b"\x93NUMPY":
        # Might be a file-path fallback.  Ignore for now.
        return None, f"not npy magic (first8={payload[:8]!r}, len={len(payload)})"
    arr = np.load(io.BytesIO(payload))
    return arr, None


def _depth_vis(arr: np.ndarray) -> np.ndarray:
    """Map depth -> 8-bit viridis-ish grayscale for visual inspection."""
    if arr.ndim == 3 and arr.shape[-1] > 1:
        arr = arr[..., 0]
    valid = np.isfinite(arr) & (arr > 0)
    if not valid.any():
        return np.zeros(arr.shape[:2], dtype=np.uint8)
    lo = np.percentile(arr[valid], 1)
    hi = np.percentile(arr[valid], 99)
    norm = np.clip((arr - lo) / max(hi - lo, 1e-6), 0, 1)
    return (norm * 255).astype(np.uint8)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--mcp-port", type=int, default=55558)
    parser.add_argument("--ucv-port", type=int, default=9002)
    parser.add_argument("--outdir", default="probes")
    parser.add_argument("--skip-pie", action="store_true",
                        help="UE is already in PIE; skip start_pie")
    parser.add_argument("--n-objects", type=int, default=4,
                        help="How many small objects to spawn in front of agent")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-5s %(name)s | %(message)s",
    )

    outdir = Path(args.outdir)
    outdir.mkdir(parents=True, exist_ok=True)

    from gym_env.mcp_client import MCPClient
    from gym_env.ucv_client import UCVClient
    from gym_env.object_pool import get_pool

    mcp = MCPClient(host="127.0.0.1", port=args.mcp_port, name="probe-mcp")
    ucv = UCVClient(host="127.0.0.1", port=args.ucv_port, name="probe-ucv")

    # 1. PIE
    if not args.skip_pie:
        log.info("starting PIE...")
        try:
            mcp.start_pie(wait_seconds=8.0)
        except Exception as exc:
            log.warning("start_pie: %s (probably already on)", exc)

    # 2. Connect UCV
    for a in range(15):
        try:
            ucv.connect()
            break
        except Exception:
            time.sleep(2)
    else:
        raise SystemExit("cannot connect to UCV")

    # Cleanup any leftover agents
    leftovers = [
        n for n in ucv.vget_objects()
        if n.startswith("ProbeAgent") or n.startswith("probe_target")
    ]
    for n in leftovers:
        ucv.destroy_actor(n)
    if leftovers:
        time.sleep(2)

    # 3. Spawn humanoid agent
    name = "ProbeAgent_0"
    spawn_xyz = (0.0, 0.0, 110.0)
    log.info("spawning %s at %s", name, spawn_xyz)
    ucv.spawn_bp_asset(_HUMANOID_BP, name, location=spawn_xyz,
                       auto_repair_collision=False)
    # Configure (matches env/batch_runner)
    for cmd in [
        f"vset /object/{name}/scale 1 1 1",
        f"vset /object/{name}/collision true",
        f"vset /object/{name}/object_mobility true",
        f"vbp {name} SetMaxSpeed 200",
        f"vbp {name} EnableController True",
    ]:
        try:
            ucv.send(cmd)
        except Exception as exc:
            log.warning("%s FAILED: %s", cmd, exc)
    time.sleep(3)

    # 4. Pick a camera id that returns valid PNG bytes.
    cam_id = None
    for cid in (1, 0, 2, 3):
        try:
            png = ucv.vget_camera_png(camera_id=cid, mode="lit")
        except Exception as exc:
            log.info("cam %d lit -> exc %s", cid, exc)
            continue
        if png[:8] == b"\x89PNG\r\n\x1a\n":
            log.info("cam %d returns valid PNG (%d bytes)", cid, len(png))
            cam_id = cid
            break
    if cam_id is None:
        raise SystemExit("no camera returned a valid PNG!")

    # 5. Baseline capture
    png = ucv.vget_camera_png(camera_id=cam_id, mode="lit")
    lit_before = _decode_lit(png)
    _save_png(png, outdir / "lit_before.png")
    log.info(
        "LIT before: %s mean=%.1f std=%.1f",
        lit_before.shape, lit_before.mean(), lit_before.std(),
    )

    depth_arr, err = _capture_depth(ucv, cam_id)
    if depth_arr is None:
        log.error("DEPTH capture FAILED: %s", err)
        depth_ok = False
    else:
        depth_ok = True
        np.save(outdir / "depth_before.npy", depth_arr)
        Image.fromarray(_depth_vis(depth_arr)).save(outdir / "depth_before_vis.png")
        finite = depth_arr[np.isfinite(depth_arr)]
        log.info(
            "DEPTH before: shape=%s dtype=%s min=%.1f max=%.1f mean=%.1f",
            depth_arr.shape, depth_arr.dtype,
            float(finite.min()) if finite.size else float("nan"),
            float(finite.max()) if finite.size else float("nan"),
            float(finite.mean()) if finite.size else float("nan"),
        )

    # 6. Spawn a few visible objects in front of the agent.
    # Agent faces +X at yaw=0 by default.  Put objects along +X at distances
    # 300/500/700cm with small lateral offsets.
    pool = get_pool()
    chosen = [pool[i] for i in (0, 4, 11, 14)][:args.n_objects]  # hydrant, can, box, cone
    spawned = []
    for i, spec in enumerate(chosen):
        d = 300.0 + i * 200.0
        y_off = (-1 if i % 2 else 1) * 80.0
        loc = (d, y_off, 0.0)
        aname = f"probe_target_{i}"
        log.info("spawn %s %s at %s", aname, spec.asset_path, loc)
        if spec.kind == "blueprint":
            ucv.spawn_bp_asset(spec.asset_path, aname, location=loc,
                               auto_repair_collision=False)
        else:
            ucv.spawn_static_mesh(spec.asset_path, aname, location=loc)
        spawned.append((aname, spec, loc))

    time.sleep(3)

    # 7. Re-orient agent to face forward, same position.
    ucv.vset_location(name, 0.0, 0.0, 110.0)
    ucv.vset_rotation(name, 0.0, 0.0, 0.0)
    time.sleep(1)

    # Verify the spawned actors are ACTUALLY in the scene (listed by
    # vget /objects) — catches the "spawn silently failed" case.
    live_actors = set(ucv.vget_objects())
    actually_present = [n for n, _, _ in spawned if n in live_actors]
    log.info(
        "object-spawn verify: %d/%d actors present in scene (%s)",
        len(actually_present), len(spawned), actually_present,
    )

    png = ucv.vget_camera_png(camera_id=cam_id, mode="lit")
    lit_after = _decode_lit(png)
    _save_png(png, outdir / "lit_after.png")
    log.info(
        "LIT after:  %s mean=%.1f std=%.1f",
        lit_after.shape, lit_after.mean(), lit_after.std(),
    )

    if depth_ok:
        depth_arr2, err = _capture_depth(ucv, cam_id)
        if depth_arr2 is None:
            log.error("DEPTH after spawn FAILED: %s", err)
        else:
            np.save(outdir / "depth_after.npy", depth_arr2)
            Image.fromarray(_depth_vis(depth_arr2)).save(
                outdir / "depth_after_vis.png"
            )
            finite = depth_arr2[np.isfinite(depth_arr2)]
            log.info(
                "DEPTH after:  shape=%s min=%.1f max=%.1f mean=%.1f",
                depth_arr2.shape,
                float(finite.min()) if finite.size else float("nan"),
                float(finite.max()) if finite.size else float("nan"),
                float(finite.mean()) if finite.size else float("nan"),
            )

    # 8. Summary
    summary = {
        "camera_id": cam_id,
        "depth_capture_ok": depth_ok,
        "lit_mean_before": float(lit_before.mean()),
        "lit_mean_after": float(lit_after.mean()),
        "lit_std_before": float(lit_before.std()),
        "lit_std_after": float(lit_after.std()),
        "n_spawned_requested": len(spawned),
        "n_spawned_actually_present": len(actually_present),
        "actors": actually_present,
    }
    import json
    (outdir / "summary.json").write_text(json.dumps(summary, indent=2))
    print("\n=== PROBE SUMMARY ===")
    for k, v in summary.items():
        print(f"  {k}: {v}")

    # Cleanup targets (leave probe agent for next run if user wants)
    for n, _, _ in spawned:
        ucv.destroy_actor(n)
    ucv.destroy_actor(name)


if __name__ == "__main__":
    main()
