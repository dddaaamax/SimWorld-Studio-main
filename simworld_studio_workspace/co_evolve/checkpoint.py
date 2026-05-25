"""Checkpoint / resume for co-evolution experiments.

Saves full experiment state after each epoch so experiments can be
resumed after UE crashes, machine restarts, etc.

State includes:
  - Which epoch we're on
  - Current scene (object list + scene_id)
  - Both agents' memories (file paths)
  - All generation results so far
  - Scene history (for map reconstruction on resume)
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any, Dict, List, Optional

log = logging.getLogger(__name__)


class CheckpointManager:
    """Save and restore co-evolution experiment state."""

    # Default umap source path — set via UE_PROJECT env var or override in config
    UMAP_SOURCE = Path(os.environ.get("UE_PROJECT", "")).parent / "Content" / "Maps" / "agent_test.umap" if os.environ.get("UE_PROJECT") else None

    def __init__(self, output_dir: Path):
        self.output_dir = output_dir
        self.checkpoint_path = output_dir / "checkpoint.json"
        self.maps_dir = output_dir / "maps"
        self.maps_dir.mkdir(parents=True, exist_ok=True)

    def save(
        self,
        epoch: int,
        gen_results: List[Dict[str, Any]],
        current_scene_id: str,
        scene_objects: List[Dict[str, Any]],
        config_dict: Dict[str, Any],
    ):
        """Save checkpoint after an epoch completes."""
        state = {
            "last_epoch": epoch,
            "current_scene_id": current_scene_id,
            "current_scene_objects": scene_objects,
            "coding_memory_path": "coding_memory.json",
            "nav_memory_path": "strategy_memory.json",
            "n_gen_results": len(gen_results),
            "config": config_dict,
        }
        self.checkpoint_path.write_text(
            json.dumps(state, indent=2, default=str), encoding="utf-8"
        )
        log.info("Checkpoint saved: epoch=%d, scene=%s, %d results",
                 epoch, current_scene_id, len(gen_results))

    def save_scene(self, scene_id: str, objects: List[Dict[str, Any]],
                   description: str = ""):
        """Save a scene's object list + copy the current .umap file."""
        import shutil

        # Save object list as JSON
        scene_path = self.maps_dir / f"{scene_id}.json"
        scene_data = {
            "scene_id": scene_id,
            "description": description,
            "objects": objects,
        }
        scene_path.write_text(
            json.dumps(scene_data, indent=2), encoding="utf-8"
        )

        # Copy the actual .umap file from UE project
        umap_dest = self.maps_dir / f"{scene_id}.umap"
        if self.UMAP_SOURCE.exists():
            try:
                shutil.copy2(str(self.UMAP_SOURCE), str(umap_dest))
                log.info("Scene saved: %s (%d objects) + umap copied",
                         scene_id, len(objects))
            except Exception as exc:
                log.warning("umap copy failed: %s", exc)
        else:
            log.warning("umap source not found: %s", self.UMAP_SOURCE)
            log.info("Scene saved: %s (%d objects) (no umap)", scene_id, len(objects))

    def save_epoch_data(
        self,
        epoch: int,
        scene_spec_dict: Dict[str, Any],
        episodes_data: List[Dict[str, Any]],
        trajectories: List[List[Dict[str, Any]]],
        gen_result: Dict[str, Any],
    ):
        """Save all data for one epoch."""
        epoch_dir = self.output_dir / f"epoch_{epoch:03d}"
        epoch_dir.mkdir(parents=True, exist_ok=True)

        (epoch_dir / "scene_spec.json").write_text(
            json.dumps(scene_spec_dict, indent=2, default=str), encoding="utf-8"
        )
        (epoch_dir / "episodes.json").write_text(
            json.dumps(episodes_data, indent=2, default=str), encoding="utf-8"
        )
        (epoch_dir / "trajectories.json").write_text(
            json.dumps(trajectories, indent=2, default=str), encoding="utf-8"
        )
        (epoch_dir / "gen_result.json").write_text(
            json.dumps(gen_result, indent=2, default=str), encoding="utf-8"
        )

    def load(self) -> Optional[Dict[str, Any]]:
        """Load checkpoint if it exists. Returns None if no checkpoint."""
        if not self.checkpoint_path.exists():
            return None
        try:
            state = json.loads(self.checkpoint_path.read_text(encoding="utf-8"))
            log.info("Checkpoint loaded: epoch=%d, scene=%s",
                     state["last_epoch"], state["current_scene_id"])
            return state
        except Exception as exc:
            log.warning("Checkpoint load failed: %s", exc)
            return None

    def load_scene(self, scene_id: str) -> Optional[List[Dict[str, Any]]]:
        """Load a saved scene's object list."""
        scene_path = self.maps_dir / f"{scene_id}.json"
        if not scene_path.exists():
            return None
        try:
            data = json.loads(scene_path.read_text(encoding="utf-8"))
            return data.get("objects", [])
        except Exception:
            return None

    def load_gen_results(self) -> List[Dict[str, Any]]:
        """Reconstruct gen_results from saved epoch data."""
        results = []
        epoch_dirs = sorted(self.output_dir.glob("epoch_*"))
        for epoch_dir in epoch_dirs:
            result_path = epoch_dir / "gen_result.json"
            if result_path.exists():
                try:
                    results.append(
                        json.loads(result_path.read_text(encoding="utf-8"))
                    )
                except Exception:
                    pass
        return results
