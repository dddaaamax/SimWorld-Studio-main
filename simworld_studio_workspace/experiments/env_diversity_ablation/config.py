"""Experiment configuration for environment diversity ablation study.

Research question: Does training on more diverse environments improve
downstream PointNav agent performance?

Design:
  - 17 unique maps: 2 held-out test + 15 training pool
  - Test maps: 1 complex (95 objects), 1 simple (~20 objects)
  - Conditions: 1, 5, 10, 15 training scenes (all using 30 total tasks)
  - Fixed test set across all conditions
  - 2 epochs per condition
  - Model: Qwen3.5-27B
"""

import os
from pathlib import Path

# ── Paths ──────────────────────────────────────────────────────────────
PROJECT_ROOT = Path("/data/koe/SimWorld-Studio-Internal/simworld_studio_workspace")
EXP_DIR = PROJECT_ROOT / "experiments" / "env_diversity_ablation"
TASKS_DIR = EXP_DIR / "tasks"           # per-map sampled tasks
CONDITIONS_DIR = EXP_DIR / "conditions" # composed train/test per condition
# Multi-model runs: set ABLATION_RESULTS_SUBDIR so different LLMs' cond1/5/10/15
# results don't overwrite each other.
_RES_SUB = os.environ.get("ABLATION_RESULTS_SUBDIR", "").strip()
RESULTS_DIR = (EXP_DIR / "results" / _RES_SUB) if _RES_SUB else (EXP_DIR / "results")

UE_ROOT = Path("/data/koe/UE_5.3.2")
UE_PROJECT_PATH = Path("/data/koe/SimWorld-Internal")
UE_PROJECT_CONTENT = UE_PROJECT_PATH / "Content"
UE_MAP_DIR = UE_PROJECT_CONTENT / "AblationMaps"  # where we copy .umaps

VALID_UMAPS_DIR = EXP_DIR / "valid_umaps"

# ── UE / Network ──────────────────────────────────────────────────────
MCP_HOST = "127.0.0.1"
MCP_PORT = 55558          # adjust to your assigned port
UCV_HOST = "127.0.0.1"
UCV_PORT = 9002           # adjust to your assigned port

# ── LLM ───────────────────────────────────────────────────────────────
LLM_MODEL = "qwen"
# Env var overrides so per-GPU supervisors can target different models.
LLM_MODEL_ID = os.environ.get("ABLATION_LLM_MODEL_ID", "Qwen3.5-27B")
LLM_BASE_URL = os.environ.get("ABLATION_LLM_BASE_URL", "http://132.239.95.133:8001/v1")
LLM_API_KEY = os.environ.get("ABLATION_LLM_API_KEY", "EMPTY")

# ── Map Inventory ─────────────────────────────────────────────────────
# Maps sorted by object count (desc) from the dedup analysis.
# fmt: (source_dir_name, n_real_objects)
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

# Test maps: pick 1 complex (#0, 95 objects) + 1 medium (#5, 30 objects)
TEST_MAP_INDICES = [0, 5]
TRAIN_MAP_INDICES = [i for i in range(len(ALL_MAPS)) if i not in TEST_MAP_INDICES]

# UE asset names (without /Game/ prefix or .umap extension)
def ue_asset_path(idx: int) -> str:
    return f"/Game/AblationMaps/ablation_{idx:02d}"

def map_label(idx: int) -> str:
    return f"ablation_{idx:02d}"

# ── Task Generation ───────────────────────────────────────────────────
TASKS_PER_MAP = 32        # sample enough for 1-scene condition (30) + held-out
MIN_GEODESIC_CM = 300.0
MAX_GEODESIC_CM = 6000.0
TASK_SEED = 42

TEST_TASKS_PER_MAP = 8    # tasks per test map

# ── Ablation Conditions ──────────────────────────────────────────────
TRAIN_BUDGET = 30                  # total training tasks per condition
N_SCENES_CONDITIONS = [1, 5, 10, 15]
N_EPOCHS = 2
MAX_STEPS = 20
MEMORY_BACKEND = "strategy"

CONDITION_SEED = 123  # for sampling which scenes to use
