"""Configuration for the co-evolution loop.

All paths and URLs can be overridden via CLI args or environment variables.
No hardcoded machine-specific paths.
"""
from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass
class CoEvolveConfig:
    # ── Mode ──
    mode: str = "live"                 # "live" (UE) or "sim" (2D simulation)

    # ── Loop ──
    generations: int = 30
    episodes_per_gen: int = 8          # default; coding agent can override.
                                       # 8 gives 9 SR levels {0, .125, ..., 1.0},
                                       # cutting single-epoch SR stderr roughly
                                       # in half vs. the old 4 (which quantized
                                       # to 5 noisy levels).
    max_steps: int = 40                # hard cap
    wave_size: int = 10                # ghost agents per parallel wave (1 UE,
                                       # N agents). episodes_per_gen episodes
                                       # are split into ceil(n/wave_size) waves.
    nav_model: str = "qwen"
    nav_model_id: str = os.environ.get("NAV_MODEL_ID", "Qwen3.5-9B")
    nav_base_url: str = os.environ.get("NAV_BASE_URL", "http://132.239.95.15:8002/v1")
    nav_api_key: str = os.environ.get("NAV_API_KEY", "EMPTY")
    nav_memory: str = "strategy"
    vision_depth: int = 1
    capture_rgb: bool = True

    # ── Coding agent LLM ──
    coding_model_id: str = os.environ.get("CODING_MODEL_ID", "Qwen3.5-9B")
    coding_base_url: str = os.environ.get("CODING_BASE_URL", "http://132.239.95.15:8002/v1")
    coding_api_key: str = os.environ.get("CODING_API_KEY", "EMPTY")

    # ── UE connection ──
    ucv_host: str = "127.0.0.1"
    ucv_port: int = int(os.environ.get("UCV_PORT", "9002"))
    mcp_host: str = "127.0.0.1"
    mcp_port: int = int(os.environ.get("MCP_PORT", "55558"))

    # ── Output ──
    output_dir: str = "runs/co_evolve"
    seed: int = 42

    # ── Curriculum teacher ──
    # Difficulty controller. One of: "alpgmm", "epsilon_greedy", "fixed".
    teacher: str = "alpgmm"
    # Half-width of the acceptable difficulty band around the teacher's
    # target. The coding agent must produce a scene whose predicted
    # rubric difficulty falls in [target-tol, target+tol], otherwise it
    # is asked to redesign.
    difficulty_tolerance: float = 2.0
    # Probability of uniform exploration (ALP-GMM / ε-greedy).
    teacher_p_random: float = 0.2
    # Max number of LLM regeneration attempts when the spec misses the band.
    teacher_max_regen: int = 0  # band is advisory; LLM is not asked to regenerate
    # Lower / upper bounds of the difficulty axis for the teacher.
    teacher_d_min: float = 1.0
    teacher_d_max: float = 10.0
