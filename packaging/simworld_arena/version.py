"""Version checking and auto-update for SimWorld Studio."""
import subprocess
import sys
import json
import urllib.request
import urllib.error

__version__ = "0.2.0"

MANIFEST_URL = "https://raw.githubusercontent.com/SimWorld-AI/SimWorld-Studio/main/version.json"


def get_installed_version():
    return __version__


def check_for_updates(quiet=False):
    """Check remote manifest for newer version. Returns (needs_update, latest, url) or (False, current, None)."""
    try:
        resp = urllib.request.urlopen(MANIFEST_URL, timeout=10)
        manifest = json.loads(resp.read().decode())
    except (urllib.error.URLError, json.JSONDecodeError, OSError) as e:
        if not quiet:
            print(f"[simworld-studio] Could not check for updates: {e}")
        return False, __version__, None

    latest = manifest.get("latest", __version__)
    pkg_url = manifest.get("url")

    if latest != __version__:
        if not quiet:
            print(f"[simworld-studio] Update available: {__version__} -> {latest}")
        return True, latest, pkg_url

    if not quiet:
        print(f"[simworld-studio] v{__version__} is up to date.")
    return False, __version__, None


def auto_update():
    """Check for updates and install if available."""
    needs_update, latest, url = check_for_updates(quiet=False)
    if needs_update and url:
        print(f"[simworld-studio] Installing v{latest}...")
        subprocess.check_call([sys.executable, "-m", "pip", "install", "-q", url])
        print(f"[simworld-studio] Updated to v{latest}. Please restart the runtime.")
        return True
    return False
