"""Step 1: Copy the 17 validated maps into the UE project Content directory.

Usage:
    python -m experiments.env_diversity_ablation.prepare_maps

Copies each map from valid_umaps/ into Content/AblationMaps/ablation_XX.umap
so UE can reference them as /Game/AblationMaps/ablation_XX.
"""

import shutil
from pathlib import Path

from .config import ALL_MAPS, VALID_UMAPS_DIR, UE_MAP_DIR


def main():
    UE_MAP_DIR.mkdir(parents=True, exist_ok=True)

    for idx, (src_dirname, n_objects) in enumerate(ALL_MAPS):
        src = VALID_UMAPS_DIR / src_dirname / "scene.umap"
        dst = UE_MAP_DIR / f"ablation_{idx:02d}.umap"

        if not src.exists():
            print(f"  SKIP {src_dirname}: source not found at {src}")
            continue

        shutil.copy2(src, dst)
        print(f"  [{idx:2d}] {src_dirname} ({n_objects} objs) -> {dst.name}")

    print(f"\nCopied maps to {UE_MAP_DIR}")
    print("UE must be restarted to detect new Content files (or use hot-reload).")


if __name__ == "__main__":
    main()
