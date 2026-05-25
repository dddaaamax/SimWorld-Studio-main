"""Per-episode logger for navigation runs.

Layout under ``runs/<run_id>/``::

    meta.json          # config snapshot, model, episode, git SHA
    episode.jsonl      # one line per env.step
    llm_raw.jsonl      # one line per LLM call (full vendor response)
    frames/step_000.png ... step_NNN.png
    summary.json       # final SR / SPL / SoftSPL / token totals
    run.log            # text log piped from python's logging module

The logger is intentionally cheap: each line is appended immediately
so a crashed run still leaves a partial trace on disk for debug.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import time
from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

import numpy as np

log = logging.getLogger(__name__)


def _now_ts() -> str:
    return datetime.now(timezone.utc).isoformat()


def _git_sha() -> Optional[str]:
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "HEAD"],
            stderr=subprocess.DEVNULL,
            text=True,
        ).strip()
    except Exception:
        return None


class _NumpyJSONEncoder(json.JSONEncoder):
    def default(self, obj):
        if isinstance(obj, (np.integer,)):
            return int(obj)
        if isinstance(obj, (np.floating,)):
            return float(obj)
        if isinstance(obj, (np.ndarray,)):
            return obj.tolist()
        if isinstance(obj, bytes):
            return f"<{len(obj)} bytes>"
        if is_dataclass(obj):
            return asdict(obj)
        try:
            return super().default(obj)
        except TypeError:
            return repr(obj)


def _dumps(obj: Any) -> str:
    return json.dumps(obj, cls=_NumpyJSONEncoder, ensure_ascii=False)


class EpisodeLogger:
    """Owns one run directory; writes JSONL + PNG frames as the run progresses."""

    def __init__(
        self,
        run_name: str,
        *,
        root: str = "runs",
        save_frames: bool = True,
        annotate_frames: bool = False,
        meta: Optional[Dict[str, Any]] = None,
        timestamp_dir: bool = True,
        install_log_handler: bool = True,
    ) -> None:
        if timestamp_dir:
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            self.run_id = f"{ts}_{run_name}"
        else:
            self.run_id = run_name
        self.dir = Path(root) / self.run_id
        self.frames_dir = self.dir / "frames"
        self.dir.mkdir(parents=True, exist_ok=True)
        self.frames_dir.mkdir(parents=True, exist_ok=True)
        self.save_frames = save_frames
        self.annotate_frames = annotate_frames

        self._episode_path = self.dir / "episode.jsonl"
        self._llm_path = self.dir / "llm_raw.jsonl"
        self._summary_path = self.dir / "summary.json"
        self._meta_path = self.dir / "meta.json"
        self._log_path = self.dir / "run.log"

        # Pipe python logging into run.log.  Batch mode installs one
        # shared handler at the batch-root level and creates many
        # per-episode loggers with install_log_handler=False so messages
        # are not duplicated N times.
        self._fh: Optional[logging.FileHandler] = None
        if install_log_handler:
            self._fh = logging.FileHandler(self._log_path, encoding="utf-8")
            self._fh.setLevel(logging.DEBUG)
            self._fh.setFormatter(logging.Formatter(
                "%(asctime)s %(levelname)-5s %(name)s | %(message)s"
            ))
            logging.getLogger().addHandler(self._fh)

        meta = dict(meta or {})
        meta.setdefault("run_id", self.run_id)
        meta.setdefault("created_at", _now_ts())
        meta.setdefault("git_sha", _git_sha())
        self._meta_path.write_text(_dumps(meta), encoding="utf-8")

        self._token_totals = {"input": 0, "output": 0, "calls": 0}
        log.info("EpisodeLogger writing to %s", self.dir)

    # ------------------------------------------------------------------
    # Step + LLM logging
    # ------------------------------------------------------------------

    def log_step(
        self,
        t: int,
        action: Optional[Dict[str, Any]],
        obs: Dict[str, Any],
        reward: float,
        done: bool,
        truncated: bool,
        info: Dict[str, Any],
    ) -> None:
        # Strip image from obs before logging — frames go to disk separately
        obs_view = {k: v for k, v in obs.items() if k != "rgb"}
        record = {
            "ts": _now_ts(),
            "t": t,
            "action": action,
            "reward": float(reward),
            "done": bool(done),
            "truncated": bool(truncated),
            "obs": obs_view,
            "info": info,
        }
        with self._episode_path.open("a", encoding="utf-8") as f:
            f.write(_dumps(record) + "\n")

        if self.save_frames and "rgb" in obs:
            caption = (
                self._build_caption(t, action, reward, info)
                if self.annotate_frames else None
            )
            self._save_frame(t, obs["rgb"], caption=caption)

    def log_llm(self, t: int, model: str, response_obj) -> None:
        usage = getattr(response_obj, "usage", {}) or {}
        if usage.get("input_tokens"):
            self._token_totals["input"] += int(usage["input_tokens"])
        if usage.get("output_tokens"):
            self._token_totals["output"] += int(usage["output_tokens"])
        self._token_totals["calls"] += 1

        record = {
            "ts": _now_ts(),
            "t": t,
            "model": model,
            "text": getattr(response_obj, "text", None),
            "reasoning": getattr(response_obj, "reasoning", None),
            "tool_calls": [
                {"id": tc.id, "name": tc.name, "arguments": tc.arguments}
                for tc in getattr(response_obj, "tool_calls", []) or []
            ],
            "usage": usage,
            "raw": getattr(response_obj, "raw", None),
        }
        with self._llm_path.open("a", encoding="utf-8") as f:
            f.write(_dumps(record) + "\n")

    def log_summary(self, metrics: Dict[str, Any]) -> None:
        merged = dict(metrics)
        merged["token_totals"] = dict(self._token_totals)
        merged["finished_at"] = _now_ts()
        self._summary_path.write_text(_dumps(merged), encoding="utf-8")
        log.info("summary written: %s", merged)

    # ------------------------------------------------------------------

    @staticmethod
    def _build_caption(
        t: int,
        action: Optional[Dict[str, Any]],
        reward: float,
        info: Dict[str, Any],
    ) -> str:
        """Compose the per-frame annotation text.

        Three short lines so a 240×320 frame can still display them
        without wrapping.  ``action is None`` marks the initial reset
        frame (no action taken yet).
        """
        if action is None:
            label = "START"
        else:
            label = action.get("tool") or action.get("name") or "UNKNOWN"
        d = info.get("distance_to_goal_cm", 0.0) or 0.0
        cum = info.get("cumulative_reward", 0.0) or 0.0
        return (
            f"step {t}: {label}\n"
            f"reward={reward:+.2f}  cum={cum:+.1f}\n"
            f"d_goal={d:.0f}cm"
        )

    def _save_frame(
        self,
        t: int,
        rgb: np.ndarray,
        caption: Optional[str] = None,
    ) -> None:
        """Save one trajectory frame, optionally with a caption banner.

        When ``caption`` is given the saved PNG has a black bar
        appended below the original frame containing the caption text
        in white — a self-contained "what happened" view that's easy
        to scrub through after the run.
        """
        try:
            from PIL import Image
            img = Image.fromarray(rgb.astype(np.uint8))
            if caption:
                img = self._annotate(img, caption)
            img.save(self.frames_dir / f"step_{t:04d}.png")
        except Exception as exc:
            log.warning("frame save failed at t=%d: %s", t, exc)

    @staticmethod
    def _annotate(img, caption: str):
        """Append a black caption bar below the image.

        Falls back to PIL's default font if no truetype is available.
        Caption may contain ``\\n`` for line breaks; lines render top
        to bottom inside the bar with a small left margin.
        """
        from PIL import Image, ImageDraw, ImageFont
        w, h = img.size
        lines = caption.split("\n")
        # Tune bar size to image — small frames get a proportionally
        # bigger banner so the text stays readable.
        line_h = max(14, h // 16)
        pad = 4
        bar_h = pad * 2 + line_h * len(lines)

        canvas = Image.new("RGB", (w, h + bar_h), (0, 0, 0))
        canvas.paste(img, (0, 0))
        draw = ImageDraw.Draw(canvas)

        try:
            font = ImageFont.truetype("arial.ttf", line_h - 2)
        except Exception:
            try:
                font = ImageFont.truetype("DejaVuSans.ttf", line_h - 2)
            except Exception:
                font = ImageFont.load_default()

        for i, line in enumerate(lines):
            draw.text(
                (pad, h + pad + i * line_h),
                line,
                fill=(255, 255, 255),
                font=font,
            )
        return canvas

    def close(self) -> None:
        if self._fh is None:
            return
        try:
            logging.getLogger().removeHandler(self._fh)
            self._fh.close()
        except Exception:
            pass
        self._fh = None
