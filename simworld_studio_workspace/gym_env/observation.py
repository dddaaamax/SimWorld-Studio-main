"""Builds the observation dict the LLM (and any future RL agent) sees.

Habitat-shape: ``rgb`` (uint8 H×W×3), ``gps`` (float32[2]),
``compass`` (float32[1]), and one of ``pointgoal_with_gps_compass``
(float32[2] = distance, angle) or ``objectgoal`` (int32[1]) depending
on task type.

This module is intentionally pure: it does not own state.  The env
constructs an :class:`ObservationBuilder` once and calls
:meth:`ObservationBuilder.observe` from ``reset`` / ``step``.

Modality switching
------------------
The observation modality ablation uses three independent flags:

  * ``capture_rgb`` — set True for the RGB condition.
  * ``capture_depth`` — set True for the depth condition (returns an
    ``np.ndarray[float32, (H, W)]`` under key ``"depth"``, plus a
    pre-colorised ``uint8[H, W, 3]`` under key ``"depth_rgb"`` so
    image-capable LLMs can see it).
  * Text-only is the degenerate case where both flags are False — the
    agent receives no image, only scalar sensors (gps / compass /
    pointgoal or objectgoal + step / task_prompt).  The env-level
    caller (``batch_runner._build_user_text``) already emits those
    scalars as text; no change needed here beyond skipping image capture.

The depth command (``vget /camera/{id}/depth npy``) returns a numpy
payload sized ``(H, W)`` with float32 plane-depth in UE's native unit
(cm).  We keep the raw array for metric use and also produce an
8-bit colour map so it can be dropped straight into the LLM's
multimodal prompt the same way RGB is.
"""

from __future__ import annotations

import io
import logging
import math
from dataclasses import dataclass
from typing import Any, Dict, Optional, Tuple

import numpy as np

from nav_task.episode import NavigationEpisode
from nav_task.task_spec import compute_pointgoal, compute_objectgoal_id

from .ucv_client import UCVClient

log = logging.getLogger(__name__)


# A depth pixel at or above this cm value is treated as "sky / miss".
# We clamp before colour-mapping so a single miss pixel doesn't crush
# the visible dynamic range.
_DEPTH_FAR_CM = 10000.0

# Float16 maximum.  When the plugin has no primitives to render (the old
# DepthCamSensor default before the 2026-04 fix) it returns an entire
# buffer filled with this value.  Callers use this to detect the broken
# plugin so they can fail loud rather than silently feed a blank image
# to the agent.
_FLOAT16_MAX = 65504.0


@dataclass
class ObservationBuilder:
    """Pulls a single observation from the live UE scene.

    Parameters
    ----------
    ucv : UCVClient
        Shared UnrealCV connection (also used by the env for actions).
    agent_name : str
        UE actor name to query.
    camera_id : int
        UnrealCV camera index for RGB / depth capture.
    image_size : tuple
        ``(height, width)`` of the captured RGB / depth frame.
    capture_rgb : bool
        Whether to populate ``obs["rgb"]``.
    capture_depth : bool
        Whether to populate ``obs["depth"]`` and ``obs["depth_rgb"]``.
    """

    ucv: UCVClient
    agent_name: str
    camera_id: int = 0
    image_size: Tuple[int, int] = (240, 320)
    capture_rgb: bool = True
    capture_depth: bool = False

    # ------------------------------------------------------------------

    def observe(
        self,
        episode: NavigationEpisode,
        start_xy: Tuple[float, float],
    ) -> Dict[str, Any]:
        """Build a single Habitat-style observation dict.

        ``start_xy`` is the agent's spawn position; ``gps`` is reported
        as a relative displacement from this point (Habitat convention).
        """
        loc = self.ucv.vget_location(self.agent_name)
        rot = self.ucv.vget_rotation(self.agent_name)
        x, y, _z = loc
        _pitch, yaw_deg, _roll = rot
        yaw_rad = math.radians(yaw_deg)

        gps = np.array(
            [x - start_xy[0], y - start_xy[1]],
            dtype=np.float32,
        )
        compass = np.array([yaw_rad], dtype=np.float32)

        obs: Dict[str, Any] = {
            "gps": gps,
            "compass": compass,
            "agent_xy": np.array([x, y], dtype=np.float32),
            "agent_yaw_deg": float(yaw_deg),
        }

        if episode.task_type == "pointnav":
            d, ang = compute_pointgoal(x, y, yaw_deg, episode.goal_position)
            obs["pointgoal_with_gps_compass"] = np.array([d, ang], dtype=np.float32)
        elif episode.task_type == "objectnav":
            cat = episode.object_category or ""
            try:
                cid = compute_objectgoal_id(cat) if cat else -1
            except Exception:
                cid = -1
            obs["objectgoal"] = np.array([cid], dtype=np.int32)
            # Always include distance+bearing to the goal point for
            # objectnav too — all modalities share this scalar baseline
            # so the ablation measures the *incremental* value of
            # perceptual info (RGB vs depth vs none), not the loss of
            # goal-direction knowledge.
            d, ang = compute_pointgoal(x, y, yaw_deg, episode.goal_position)
            obs["pointgoal_with_gps_compass"] = np.array([d, ang], dtype=np.float32)

        if self.capture_rgb:
            obs["rgb"] = self._capture_rgb()

        if self.capture_depth:
            depth = self._capture_depth()
            if depth is not None:
                obs["depth"] = depth
                obs["depth_rgb"] = depth_to_rgb(depth)

        return obs

    # ------------------------------------------------------------------

    def _capture_rgb(self) -> np.ndarray:
        try:
            png = self.ucv.vget_camera_png(camera_id=self.camera_id, mode="lit")
            if not png:
                raise RuntimeError("empty PNG payload")
            from PIL import Image
            img = Image.open(io.BytesIO(png)).convert("RGB")
            target_h, target_w = self.image_size
            if img.size != (target_w, target_h):
                img = img.resize((target_w, target_h), Image.BILINEAR)
            return np.array(img, dtype=np.uint8)
        except Exception as exc:
            log.warning("RGB capture failed: %s — returning zeros", exc)
            h, w = self.image_size
            return np.zeros((h, w, 3), dtype=np.uint8)

    def _capture_depth(self) -> Optional[np.ndarray]:
        """Fetch ``vget /camera/{id}/depth npy`` and return an (H, W) float32.

        Returns ``None`` (not zeros) on failure so the caller can tell
        the difference between "legitimately everywhere zero" and "the
        sensor did not respond."  The env surfaces this by omitting the
        ``depth`` key from the obs dict in that case.
        """
        cmd = f"vget /camera/{self.camera_id}/depth npy"
        try:
            payload = self.ucv.send_bytes(cmd, timeout=20)
        except Exception as exc:
            log.warning("depth capture send failed: %s", exc)
            return None
        if not payload or payload[:6] != b"\x93NUMPY":
            log.warning(
                "depth capture: non-npy payload (first8=%r, len=%d)",
                payload[:8], len(payload),
            )
            return None
        try:
            arr = np.load(io.BytesIO(payload))
        except Exception as exc:
            log.warning("depth npy decode failed: %s", exc)
            return None
        # Shape can be (H, W) or (H, W, 1).
        if arr.ndim == 3 and arr.shape[-1] == 1:
            arr = arr[..., 0]
        # Detect the old plugin bug: entire buffer == float16 max.
        # (The rebuilt plugin renders scene primitives so values cluster
        # in cm-meaningful ranges.)
        if arr.size and np.all(arr == _FLOAT16_MAX):
            log.warning(
                "depth buffer is uniformly float16-max — plugin is likely "
                "the pre-fix DepthCamSensor; returning None"
            )
            return None
        target_h, target_w = self.image_size
        if arr.shape != (target_h, target_w):
            # Down/up-sample to match the RGB image_size (nearest to
            # preserve depth discontinuities).
            from PIL import Image
            resized = Image.fromarray(arr.astype(np.float32)).resize(
                (target_w, target_h), Image.NEAREST,
            )
            arr = np.array(resized, dtype=np.float32)
        return arr.astype(np.float32, copy=False)


# ---------------------------------------------------------------------
# Depth -> RGB helpers
# ---------------------------------------------------------------------

def depth_to_rgb(
    depth: np.ndarray,
    *,
    far_cm: float = _DEPTH_FAR_CM,
) -> np.ndarray:
    """Map an (H, W) depth array in cm to a uint8 (H, W, 3) colour image.

    We use a simple perceptually-ordered grayscale ramp (near = bright,
    far = dark) — monotonic, matches human intuition, and avoids the
    hue-cycle ambiguity of a jet colormap.  Values ≥ ``far_cm`` are
    mapped to black.
    """
    if depth.ndim == 3 and depth.shape[-1] == 1:
        depth = depth[..., 0]
    d = np.asarray(depth, dtype=np.float32)
    valid = np.isfinite(d) & (d > 0) & (d < far_cm)
    if not valid.any():
        return np.zeros((*d.shape, 3), dtype=np.uint8)
    lo = float(np.percentile(d[valid], 1))
    hi = float(np.percentile(d[valid], 99))
    hi = max(hi, lo + 1.0)
    # Near (lo) -> 255, Far (hi) -> 32 so there's visible contrast end-to-end.
    norm = 1.0 - np.clip((d - lo) / (hi - lo), 0, 1)
    gray = (32 + norm * (255 - 32)).astype(np.uint8)
    gray[~valid] = 0
    return np.stack([gray, gray, gray], axis=-1)
