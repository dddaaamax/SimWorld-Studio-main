"""Standalone camera probe to nail down which camera id works after spawn.

Run this against a freshly-launched UE Editor that is currently in
**editor mode** (NOT yet in PIE).  We:

  1. Connect MCP, start PIE.
  2. Connect UnrealCV (fresh).
  3. Snapshot ``vget /cameras`` BEFORE spawn (baseline).
  4. Spawn a humanoid.
  5. Wait long enough for the BP's FusionCamSensor to register.
  6. Snapshot ``vget /cameras`` AFTER spawn (diff = our camera).
  7. Try ``vget /camera/{N}/lit png`` for N=0,1,2 with **tight timeouts**
     and report which one returns a real PNG.

No env wrapping, no async, no thread pools — just sequential print +
flush so you see exactly where it dies.

Usage::

    PYTHONPATH=C:/Users/28262/Desktop/PlayGorund/task_gen \
        python -u -m gym_env.probe_camera
"""

from __future__ import annotations

import sys
import time

from .mcp_client import MCPClient


_LOG_PATH = None  # set in main()


def _say(msg: str) -> None:
    print(msg, flush=True)
    # Also append to a file so we get the trace even if the parent
    # process buffers our stdout out of view.
    if _LOG_PATH is not None:
        try:
            with open(_LOG_PATH, "a", encoding="utf-8") as fh:
                fh.write(msg + "\n")
                fh.flush()
        except Exception:
            pass


def main() -> None:
    global _LOG_PATH
    import os
    _LOG_PATH = os.path.join(os.getcwd(), "probe_camera.log")
    # Reset log
    with open(_LOG_PATH, "w", encoding="utf-8") as fh:
        fh.write("")
    _say(f"=== camera probe (log: {_LOG_PATH}) ===")

    # 1. PIE start
    mcp = MCPClient(name="probe-mcp")
    _say("starting PIE via MCP...")
    try:
        mcp.start_pie(wait_seconds=5.0)
    except Exception as exc:
        _say(f"  PIE start failed (may already be on): {exc}")

    # 2. Fresh UnrealCV (using the raw client, not our wrapper, so we
    #    can see exactly what it does)
    import unrealcv
    _say("connecting UnrealCV...")
    c = unrealcv.Client(("127.0.0.1", 9000))
    c.connect()
    _say(f"  connected={c.isconnected()}")

    # 3. Pre-spawn snapshot
    cams_before = c.request("vget /cameras", timeout=5)
    _say(f"cameras BEFORE spawn: {cams_before!r}")

    objs_before = set(c.request("vget /objects", timeout=5).split())
    _say(f"  scene actor count BEFORE: {len(objs_before)}")
    leftovers = [n for n in objs_before
                 if n.startswith("GymNavAgent") or "Base_User_Agent_C_" in n
                 or n.startswith("ProbeAgent")]
    if leftovers:
        _say(f"  leftover agents from prior runs: {leftovers}")
        for n in leftovers:
            try:
                c.request(f"vset /object/{n}/destroy", timeout=5)
                _say(f"    destroyed {n}")
            except Exception as exc:
                _say(f"    destroy {n} err: {exc}")
        time.sleep(2)
        cams_before = c.request("vget /cameras", timeout=5)
        _say(f"cameras after cleanup: {cams_before!r}")

    # 4. Skinned-asset compile (matches scripts/test_agent_control.py)
    _say("vrun Editor.AsyncSkinnedAssetCompilation 2 ...")
    c.request("vrun Editor.AsyncSkinnedAssetCompilation 2", timeout=5)
    time.sleep(2)

    # 5. Spawn the humanoid.  Spawn closes the socket so we wrap and reconnect.
    bp = "/Game/TrafficSystem/Pedestrian/Base_User_Agent.Base_User_Agent_C"
    name = "ProbeAgent_0"
    _say(f"spawning {name}...")
    try:
        r = c.request(f"vset /objects/spawn_bp_asset {bp} {name}", timeout=15)
        _say(f"  spawn -> {r!r}")
    except Exception as exc:
        _say(f"  spawn raised (often expected on this UE): {exc}")

    time.sleep(3)

    # Brand-new client object → no stale receive-thread state
    _say("opening fresh client after spawn...")
    try:
        c.disconnect()
    except Exception:
        pass
    c = unrealcv.Client(("127.0.0.1", 9000))
    c.connect()
    _say(f"  reconnected={c.isconnected()}")

    # Confirm the agent landed
    objs_after = set(c.request("vget /objects", timeout=5).split())
    new_actors = sorted(objs_after - objs_before)
    _say(f"NEW actors after spawn: {new_actors}")

    # 6. Configure: scale + collision + mobility + speed + location +
    # rotation + EnableController.  This is the full sequence the env
    # uses; missing scale or EnableController has been observed to
    # leave the FusionCamSensor's scene capture component
    # uninitialised, which crashes UE on the first lit-capture call.
    _say("configuring agent (full sequence)...")
    for cmd in [
        f"vset /object/{name}/scale 1 1 1",
        f"vset /object/{name}/collision true",
        f"vset /object/{name}/object_mobility true",
        f"vbp {name} SetMaxSpeed 200",
        f"vset /object/{name}/location 0 0 110",
        f"vset /object/{name}/rotation 0 0 0",
        f"vbp {name} EnableController True",
    ]:
        try:
            r = c.request(cmd, timeout=5)
            _say(f"  {cmd[:60]}... -> {r!r}")
        except Exception as exc:
            _say(f"  {cmd[:60]}... ERR: {exc}")

    _say("sleeping 5s for sensor registration...")
    time.sleep(5)

    cams_after = c.request("vget /cameras", timeout=5)
    _say(f"cameras AFTER spawn+settle: {cams_after!r}")

    # 7. Probe each camera id (TIGHT timeouts so we never hang the run)
    #
    # For any camera that returns valid PNG bytes, decode the image and
    # report shape + a quick "is it all black?" sanity check, then save
    # the frame to ./probe_frames/ so the user can eyeball it.
    import os
    out_dir = os.path.join(os.getcwd(), "probe_frames")
    os.makedirs(out_dir, exist_ok=True)
    _say(f"saving frames to {out_dir}")

    for cid in (0, 1, 2, 3):
        _say(f"--- camera {cid} ---")
        try:
            loc = c.request(f"vget /camera/{cid}/location", timeout=5)
            _say(f"  /camera/{cid}/location -> {loc!r}")
        except Exception as exc:
            _say(f"  /camera/{cid}/location ERR: {exc}")

        t0 = time.time()
        try:
            data = c.request(f"vget /camera/{cid}/lit png", timeout=10)
            elapsed = time.time() - t0
            if isinstance(data, bytes) and data[:8] == b"\x89PNG\r\n\x1a\n":
                _say(f"  /camera/{cid}/lit -> {len(data)} bytes PNG in {elapsed:.2f}s  ★")
                # Save to disk
                out_path = os.path.join(out_dir, f"camera_{cid}_lit.png")
                with open(out_path, "wb") as f:
                    f.write(data)
                _say(f"    saved to {out_path}")
                # Decode + sanity check
                try:
                    from PIL import Image
                    import io
                    import numpy as np
                    img = Image.open(io.BytesIO(data))
                    arr = np.array(img)
                    h, w = arr.shape[:2]
                    ch = arr.shape[2] if arr.ndim == 3 else 1
                    mean = float(arr.mean())
                    std = float(arr.std())
                    nonzero = int((arr > 0).sum())
                    pct_nonzero = 100.0 * nonzero / arr.size
                    _say(f"    image: {w}x{h}x{ch}  mean={mean:.1f}  std={std:.1f}  "
                         f"nonzero={pct_nonzero:.1f}%")
                    if mean < 1.0:
                        _say(f"    WARNING: image is essentially black")
                    elif std < 1.0:
                        _say(f"    WARNING: image is uniform (no detail)")
                    else:
                        _say(f"    image looks valid (has variation)")
                except Exception as exc:
                    _say(f"    image decode/analyze failed: {exc}")
            elif isinstance(data, str):
                _say(f"  /camera/{cid}/lit -> error string: {data!r}")
            else:
                _say(f"  /camera/{cid}/lit -> unexpected: type={type(data).__name__} len={len(data) if hasattr(data,'__len__') else None}")
        except Exception as exc:
            _say(f"  /camera/{cid}/lit FAILED in {time.time()-t0:.2f}s: {type(exc).__name__}: {exc}")

    # 8. Cleanup
    _say("cleaning up agent...")
    try:
        c.request(f"vset /object/{name}/destroy", timeout=5)
    except Exception as exc:
        _say(f"  destroy err: {exc}")

    c.disconnect()
    _say("done")


if __name__ == "__main__":
    main()
