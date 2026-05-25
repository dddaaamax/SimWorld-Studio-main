"""Quick script to grab depth image from UnrealCV camera."""
import sys
import io
import time

# Adjust path so gym_env package is importable
sys.path.insert(0, r"c:\Users\28262\Desktop\PlayGorund\SimWorld-Studio-Dev\simworld_studio_workspace")

from gym_env.ucv_client import UCVClient, UCVError

HOST = "127.0.0.1"
PORT = 9002

client = UCVClient(host=HOST, port=PORT, name="grab_depth")
client.connect()
print(f"Connected to UnrealCV at {HOST}:{PORT}")

# Try camera 0 first (scene camera), fallback to 1
for cam_id in [0, 1]:
    for fmt in ["depth", "depth png"]:
        cmd = f"vget /camera/{cam_id}/{fmt}"
        print(f"Trying: {cmd}")
        try:
            raw = client.send_bytes(cmd, timeout=15)
        except UCVError as e:
            print(f"  UCVError: {e}")
            raw = b""

        if raw[:8] == b"\x89PNG\r\n\x1a\n":
            out = f"depth_cam{cam_id}.png"
            with open(out, "wb") as f:
                f.write(raw)
            print(f"  Saved PNG -> {out} ({len(raw)} bytes)")
            break
        elif raw[:6] == b"\x93NUMPY":
            out = f"depth_cam{cam_id}.npy"
            with open(out, "wb") as f:
                f.write(raw)
            print(f"  Saved NPY -> {out} ({len(raw)} bytes)")
            break
        else:
            # might be a file path string
            text = raw.decode("latin-1").strip().strip('"')
            if text and not text.lower().startswith("error"):
                print(f"  Got path string: {text}")
                try:
                    with open(text, "rb") as f:
                        data = f.read()
                    import shutil
                    out = f"depth_cam{cam_id}.npy"
                    shutil.copy(text, out)
                    print(f"  Copied to {out}")
                    break
                except Exception as ex:
                    print(f"  Could not copy file: {ex}")
            else:
                print(f"  Response was not PNG/NPY/path: {raw[:80]!r}")

client.disconnect()
print("Done.")
