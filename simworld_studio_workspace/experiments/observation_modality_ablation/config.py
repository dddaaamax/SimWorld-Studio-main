"""Observation modality ablation — config.

Research question
-----------------
Does perceptual input affect ObjectNav performance?  We hold everything
except the sensor pipeline constant and vary the modality across
{rgb, depth, text_only}.  Expected qualitative result: rgb ≥ depth ≫
text_only (less information, worse performance).

Design
------
* Task: **ObjectNav** — spawn a small distinctive object at a navmesh
  position, agent must navigate within 200 cm of it.  Unlike PointNav,
  the agent must recognise the target object by sight (depth or rgb)
  or infer purely from scalar sensors (text-only).
* Maps: reuse the 17 AblationMaps from env_diversity_ablation
  (``/Game/AblationMaps/ablation_NN``).  **15 train + 2 test** → seen
  = training maps, unseen = held-out test maps (indices 0 and 5).
  3 episodes per map.
* Models: Qwen3.5-{2B, 9B, 27B}.  Each model runs all 3 modalities on
  the full seen+unseen split, resume-enabled.
* Modalities:
    - ``rgb`` : first-person lit PNG (default pipeline).
    - ``depth`` : 16-bit depth float → grayscale PNG fed to the LLM
      in place of RGB.  Requires the post-2026-04-18 UnrealCV plugin
      build (DepthCamSensor::bIgnoreTransparentObjects default flipped).
    - ``text_only`` : no image at all — only GPS / compass / objectgoal
      scalars rendered as text.
"""

import os
from pathlib import Path

# ── Paths ──────────────────────────────────────────────────────────────
PROJECT_ROOT = Path("/data/koe/SimWorld-Studio-Internal/simworld_studio_workspace")
EXP_DIR = PROJECT_ROOT / "experiments" / "observation_modality_ablation"
TASKS_DIR = EXP_DIR / "tasks"           # per-map generated ObjectNav episodes
# Per-model/modality results live under results/{model_tag}/{modality}/
RESULTS_DIR = EXP_DIR / "results"

UE_PROJECT_A = Path("/data/koe/SimWorld-Internal")     # first instance
UE_PROJECT_B = Path("/data/koe/SimWorld-Internal-B")   # second instance (parallel)

# ── UE network endpoints ──────────────────────────────────────────────
# Two parallel UE instances so we can run 2 (model × modality) pairs
# concurrently on different GPUs.  Reuse env_diversity_ablation's port
# choice (55558/9002) for instance A; pick 55559/9003 for B.
MCP_HOST = "127.0.0.1"
UCV_HOST = "127.0.0.1"

UE_A = {"mcp_port": 55558, "ucv_port": 9002, "project": UE_PROJECT_A, "gpu": 1}
UE_B = {"mcp_port": 55559, "ucv_port": 9003, "project": UE_PROJECT_B, "gpu": 2}

# ── Map inventory (17 total; copy of env_diversity_ablation) ──────────
# Indices 0 and 5 are the held-out TEST (unseen) maps.  Remaining 15
# are TRAIN (seen).  These .umap assets were already copied to both
# SimWorld-Internal and SimWorld-Internal-B under /Game/AblationMaps.
ALL_MAPS = [
    ("main_experiment_claude-opus_s2_hard",            95),
    ("main_experiment_claude-opus_s1_hard",            68),
    ("special_isometric_village",                      53),
    ("all_scenes_eval_2026-04-11T22-50-07",           51),
    ("all_scenes_eval_2026-04-11T22-48-33",           38),
    ("all_scenes_eval_2026-04-11T22-46-52",           30),
    ("all_scenes_eval_2026-04-11T22-45-57",           27),
    ("all_scenes_eval_2026-04-11T22-18-50",           26),
    ("main_experiment_claude-qwen3.5-9b_s1_easy",     26),
    ("all_scenes_eval_2026-04-11T22-39-17",           25),
    ("all_scenes_eval_2026-04-11T22-20-56",           23),
    ("all_scenes_eval_2026-04-11T22-41-25",           22),
    ("main_experiment_claude-opus_s1_mid",             22),
    ("all_scenes_eval_2026-04-11T22-37-11",           20),
    ("all_scenes_eval_2026-04-11T22-16-34",           18),
    ("all_scenes_eval_2026-04-11T22-44-23",           16),
    ("main_experiment_claude-opus_s2_mid",             15),
]

TEST_MAP_INDICES = [0, 5]  # "unseen"
TRAIN_MAP_INDICES = [i for i in range(len(ALL_MAPS)) if i not in TEST_MAP_INDICES]  # "seen"


def ue_asset_path(idx: int) -> str:
    return f"/Game/AblationMaps/ablation_{idx:02d}"


def map_label(idx: int) -> str:
    return f"ablation_{idx:02d}"


# ── Task generation ───────────────────────────────────────────────────
EPISODES_PER_MAP = 3
TASK_SEED = 42
MIN_GEODESIC_CM = 500.0
MAX_GEODESIC_CM = 4000.0
SUCCESS_DISTANCE_CM = 200.0
MAX_STEPS = 20
MAX_EPISODE_TIME_S = 300.0

# Min horizontal spacing between spawned target objects on a single map,
# so each ObjectNav episode has a distinct goal even when the same map
# is used for several episodes.
MIN_TARGET_SPACING_CM = 700.0

# ── LLM endpoints (3 models) ──────────────────────────────────────────
MODELS = {
    "qwen25_2b": {
        "model": "qwen",
        "model_id": "Qwen3.5-2B",
        "base_url": "http://132.239.95.15:8003/v1",
        "api_key": "EMPTY",
    },
    "qwen25_9b": {
        "model": "qwen",
        "model_id": "Qwen3.5-9B",
        "base_url": "http://132.239.95.15:8002/v1",
        "api_key": "EMPTY",
    },
    "qwen25_27b": {
        "model": "qwen",
        "model_id": "Qwen3.5-27B",
        "base_url": "http://132.239.95.133:8001/v1",
        "api_key": "EMPTY",
    },
}

# ── Observation modalities ────────────────────────────────────────────
#
# Each modality is a dict of kwargs forwarded to SimWorldNavEnv +
# batch_runner.run_wave.  ``image_kind`` is our own tag consumed by the
# prompt builder (``_build_user_text``) to decide what to show:
#     "rgb"   — first-person lit PNG
#     "depth" — grayscale depth rendered from the depth float buffer
#     "none"  — no image at all, scalars-only (text_only)
MODALITIES = {
    "rgb": {
        "capture_rgb": True,
        "capture_depth": False,
        "image_kind": "rgb",
    },
    "depth": {
        "capture_rgb": False,
        "capture_depth": True,
        "image_kind": "depth",
    },
    "rgb_depth": {
        "capture_rgb": True,
        "capture_depth": True,
        "image_kind": "rgb_depth",
    },
    "text_only": {
        "capture_rgb": False,
        "capture_depth": False,
        "image_kind": "none",
    },
}

# ── Object pool ───────────────────────────────────────────────────────
# We use gym_env.object_pool entries.  These are visually distinct,
# small objects (hydrant, traffic cone, cardboard box, soda cans) —
# unlike buildings or trees, they are unique enough that the VLM can
# discriminate them from background clutter.  Verified via
# probe_depth_and_spawn.py that each candidate actually materialises
# in the scene and shows up in RGB before running the experiment.
OBJECT_POOL_CHOICE = "curated_small_objects"
