"""Difficulty teacher: pluggable curriculum controller.

Each epoch the runner asks the teacher for a target difficulty and an
acceptable band [lo, hi].  The coding agent must produce a scene whose
predicted (and ultimately observed) difficulty falls inside that band,
otherwise it is asked to redesign.  After episodes are run, the teacher
is updated with the observed difficulty and the per-episode SRs.

Implementations
---------------
- ``FixedTeacher``      No-op baseline (wide band, fixed target).
- ``EpsilonGreedyTeacher``  Discrete-bin bandit, ε-greedy over Gaussian-
  shaped reward centred at ``target_sr``.
- ``ALPGMMTeacher``     Continuous teacher driven by Absolute Learning
  Progress (ALP).  Maintains a buffer of (difficulty, SR) tuples and
  resamples past difficulties weighted by the local |ΔSR| signal.
  No external dependency; behaves like Portelas et al. 2020 ALP-GMM
  with a degenerate per-point Gaussian kernel.

All teachers persist via ``state_dict`` / ``load_state_dict`` and the
``save`` / ``load`` helpers, so curricula survive ``--resume``.
"""
from __future__ import annotations

import json
import logging
import math
import random
from abc import ABC, abstractmethod
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Deque, Dict, List, Tuple

log = logging.getLogger(__name__)


@dataclass
class DifficultyProposal:
    """One curriculum step: a target difficulty and an acceptable band."""
    target: float          # nominal target in [d_min, d_max]
    band_lo: float         # acceptable lower bound for realised difficulty
    band_hi: float         # acceptable upper bound for realised difficulty
    rationale: str = ""    # for logging only

    def contains(self, d: float) -> bool:
        return self.band_lo <= d <= self.band_hi


# ---------------------------------------------------------------------------
# Base class
# ---------------------------------------------------------------------------
class Teacher(ABC):
    name: str = "abstract"

    @abstractmethod
    def propose(self) -> DifficultyProposal: ...

    @abstractmethod
    def update(
        self,
        target: float,
        observed_difficulty: float,
        sr_per_episode: List[float],
    ) -> None: ...

    @abstractmethod
    def state_dict(self) -> Dict[str, Any]: ...

    @abstractmethod
    def load_state_dict(self, state: Dict[str, Any]) -> None: ...

    # -- persistence --------------------------------------------------------
    def save(self, path: Path) -> None:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"name": self.name, "state": self.state_dict()}
        path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    def load(self, path: Path) -> bool:
        path = Path(path)
        if not path.exists():
            return False
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            log.warning("Teacher load: bad JSON at %s: %s", path, exc)
            return False
        if data.get("name") != self.name:
            log.warning("Teacher load: name mismatch (file=%s current=%s)",
                        data.get("name"), self.name)
            return False
        try:
            self.load_state_dict(data.get("state", {}))
            log.info("Teacher %s loaded from %s", self.name, path)
            return True
        except Exception as exc:
            log.warning("Teacher load_state_dict failed: %s", exc)
            return False


# ---------------------------------------------------------------------------
# Fixed (no-op) — baseline / ablation
# ---------------------------------------------------------------------------
class FixedTeacher(Teacher):
    name = "fixed"

    def __init__(self, target: float = 5.0, tol: float = 10.0, **_):
        self.target = float(target)
        self.tol = float(tol)

    def propose(self) -> DifficultyProposal:
        return DifficultyProposal(
            target=self.target,
            band_lo=max(0.0, self.target - self.tol),
            band_hi=min(10.0, self.target + self.tol),
            rationale="fixed",
        )

    def update(self, target, observed_difficulty, sr_per_episode):
        return

    def state_dict(self):
        return {"target": self.target, "tol": self.tol}

    def load_state_dict(self, s):
        self.target = float(s.get("target", self.target))
        self.tol = float(s.get("tol", self.tol))


# ---------------------------------------------------------------------------
# Discrete ε-greedy bandit
# ---------------------------------------------------------------------------
class EpsilonGreedyTeacher(Teacher):
    name = "epsilon_greedy"

    def __init__(
        self,
        d_min: float = 1.0,
        d_max: float = 10.0,
        n_bins: int = 10,
        epsilon: float = 0.2,
        target_sr: float = 0.6,
        sr_sigma: float = 0.2,
        tol: float = 2.0,
        seed: int = 0,
    ):
        self.d_min, self.d_max = float(d_min), float(d_max)
        self.n_bins = int(n_bins)
        self.epsilon = float(epsilon)
        self.target_sr = float(target_sr)
        self.sr_sigma = float(sr_sigma)
        self.tol = float(tol)
        self._rng = random.Random(seed)
        self.counts: List[int] = [0] * self.n_bins
        self.values: List[float] = [0.0] * self.n_bins

    # -- helpers ------------------------------------------------------------
    def _bin_to_d(self, k: int) -> float:
        step = (self.d_max - self.d_min) / self.n_bins
        return self.d_min + step * (k + 0.5)

    def _d_to_bin(self, d: float) -> int:
        step = (self.d_max - self.d_min) / self.n_bins
        k = int((d - self.d_min) / max(step, 1e-9))
        return max(0, min(self.n_bins - 1, k))

    # -- API ----------------------------------------------------------------
    def propose(self) -> DifficultyProposal:
        if self._rng.random() < self.epsilon or sum(self.counts) == 0:
            k = self._rng.randrange(self.n_bins)
            rationale = f"explore (eps={self.epsilon:.2f}) bin={k}"
        else:
            best_v = max(self.values)
            best_ks = [k for k, v in enumerate(self.values) if v >= best_v - 1e-9]
            k = self._rng.choice(best_ks)
            rationale = f"exploit bin={k} mean_reward={best_v:.3f}"
        d = self._bin_to_d(k)
        return DifficultyProposal(
            target=d,
            band_lo=max(self.d_min, d - self.tol),
            band_hi=min(self.d_max, d + self.tol),
            rationale=rationale,
        )

    def update(self, target, observed_difficulty, sr_per_episode):
        if not sr_per_episode:
            return
        k = self._d_to_bin(observed_difficulty)
        sr_mean = sum(sr_per_episode) / len(sr_per_episode)
        # Gaussian-shaped reward (peak at target_sr).
        r = math.exp(-((sr_mean - self.target_sr) ** 2) / (2 * self.sr_sigma ** 2))
        n = self.counts[k]
        self.values[k] = (self.values[k] * n + r) / (n + 1)
        self.counts[k] = n + 1

    def state_dict(self):
        return {
            "counts": list(self.counts),
            "values": list(self.values),
            "n_bins": self.n_bins,
            "d_min": self.d_min, "d_max": self.d_max,
            "epsilon": self.epsilon, "tol": self.tol,
            "target_sr": self.target_sr, "sr_sigma": self.sr_sigma,
        }

    def load_state_dict(self, s):
        self.counts = list(s.get("counts", self.counts))
        self.values = list(s.get("values", self.values))
        if len(self.counts) != self.n_bins or len(self.values) != self.n_bins:
            log.warning("EpsGreedy: bin count mismatch on load, resetting")
            self.counts = [0] * self.n_bins
            self.values = [0.0] * self.n_bins
        self.epsilon = float(s.get("epsilon", self.epsilon))
        self.tol = float(s.get("tol", self.tol))


# ---------------------------------------------------------------------------
# ALP-GMM (continuous, learning-progress driven)
# ---------------------------------------------------------------------------
class ALPGMMTeacher(Teacher):
    """Absolute Learning Progress teacher.

    For each historical (d_i, sr_i) we compute
        ALP_i = | sr_i  −  sr_j |     where j is the d-nearest neighbour ≠ i.
    A new difficulty is sampled by:
        1. with prob ``p_random``  → uniform on [d_min, d_max] (explore)
        2. otherwise               → resample one historical d weighted by
                                     ALP_i, then add Gaussian noise of
                                     stdev ``noise_frac * tol`` to keep it
                                     continuous.
    The band is ``[d − tol, d + tol]`` clipped to the valid range.
    """

    name = "alpgmm"

    def __init__(
        self,
        d_min: float = 1.0,
        d_max: float = 10.0,
        tol: float = 2.0,
        p_random: float = 0.2,
        buffer_size: int = 200,
        warmup_n: int = 10,
        noise_frac: float = 0.25,
        seed: int = 0,
    ):
        self.d_min, self.d_max, self.tol = float(d_min), float(d_max), float(tol)
        self.p_random = float(p_random)
        self.warmup_n = int(warmup_n)
        self.noise_sigma = float(noise_frac) * float(tol)
        self._rng = random.Random(seed)
        self.history: Deque[Tuple[float, float]] = deque(maxlen=int(buffer_size))

    # -- API ----------------------------------------------------------------
    def propose(self) -> DifficultyProposal:
        if len(self.history) < self.warmup_n or self._rng.random() < self.p_random:
            d = self._rng.uniform(self.d_min, self.d_max)
            rationale = f"explore (uniform, n={len(self.history)})"
        else:
            entries = list(self.history)
            n = len(entries)
            alps: List[float] = []
            for i, (d_i, sr_i) in enumerate(entries):
                best_j = -1
                best_dd = float("inf")
                for j, (d_j, _) in enumerate(entries):
                    if j == i:
                        continue
                    dd = abs(d_i - d_j)
                    if dd < best_dd:
                        best_dd = dd
                        best_j = j
                sr_nb = entries[best_j][1] if best_j >= 0 else sr_i
                alps.append(abs(sr_i - sr_nb) + 1e-3)
            idx = self._rng.choices(range(n), weights=alps, k=1)[0]
            base_d = entries[idx][0]
            d = base_d + self._rng.gauss(0.0, self.noise_sigma)
            d = max(self.d_min, min(self.d_max, d))
            rationale = (f"alp-sample base_d={base_d:.2f} alp={alps[idx]:.3f} "
                         f"buf={n}")
        return DifficultyProposal(
            target=d,
            band_lo=max(self.d_min, d - self.tol),
            band_hi=min(self.d_max, d + self.tol),
            rationale=rationale,
        )

    def update(self, target, observed_difficulty, sr_per_episode):
        # One buffer entry per episode (per ghost agent) — the wave_size=10
        # setup gives the teacher 10 samples per epoch.
        for sr in sr_per_episode:
            self.history.append((float(observed_difficulty), float(sr)))

    def state_dict(self):
        return {
            "history": list(self.history),
            "d_min": self.d_min, "d_max": self.d_max,
            "tol": self.tol, "p_random": self.p_random,
            "warmup_n": self.warmup_n, "noise_sigma": self.noise_sigma,
        }

    def load_state_dict(self, s):
        self.history.clear()
        for d, sr in s.get("history", []):
            self.history.append((float(d), float(sr)))
        self.tol = float(s.get("tol", self.tol))
        self.p_random = float(s.get("p_random", self.p_random))


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------
TEACHERS: Dict[str, type] = {
    "alpgmm": ALPGMMTeacher,
    "epsilon_greedy": EpsilonGreedyTeacher,
    "fixed": FixedTeacher,
}


def make_teacher(name: str, **kwargs) -> Teacher:
    key = (name or "alpgmm").lower()
    if key not in TEACHERS:
        raise ValueError(
            f"Unknown teacher '{name}'. Available: {sorted(TEACHERS)}"
        )
    cls = TEACHERS[key]
    # Filter kwargs to ones the class actually accepts to avoid TypeError
    # when callers pass a superset config.
    import inspect
    sig = inspect.signature(cls.__init__)
    accepted = {k: v for k, v in kwargs.items() if k in sig.parameters}
    return cls(**accepted)


# ---------------------------------------------------------------------------
# Cheap difficulty predictor (no episode generation needed)
# ---------------------------------------------------------------------------
def predict_spec_difficulty(spec, blocked_ratio: float = 0.0) -> float:
    """Estimate the rubric difficulty of a SceneSpec without sampling.

    Used as a pre-filter before paying the cost of NavMesh episode
    generation. Mirrors :func:`co_evolve.difficulty.score_task_difficulty`
    but uses the spec's path range midpoint and an assumed average heading.
    """
    geo_est = (float(spec.min_path_cm) + float(spec.max_path_cm)) / 2.0
    n_obj = len(spec.objects) if getattr(spec, "objects", None) else 0
    detour = 1.0 + min(0.6, n_obj * 0.05)
    # path_score (0-2.5)
    path_score = min(2.5, geo_est / 1000.0)
    # detour_score (0-2.5)
    detour_score = max(0.0, min(2.5, (detour - 1.0) * 2.5))
    # scene_score (0-2.5) from prior epoch's blocked_ratio measurement
    scene_score = min(2.5, max(0.0, blocked_ratio) * 5.0)
    # heading: assume average 90 deg (= 0.5)
    heading_score = 0.5
    task_score = 0.0 if getattr(spec, "task_type", "pointnav") == "pointnav" else 1.5
    total = path_score + detour_score + scene_score + heading_score + task_score
    return round(min(10.0, total), 2)
