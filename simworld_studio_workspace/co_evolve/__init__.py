"""Co-evolution module: coding agent designs tasks, embodied agent executes them.

Public API:
    CoEvolutionRunner  — main loop orchestrator
    CoEvolveConfig     — configuration dataclass
"""
from .config import CoEvolveConfig
from .coding_memory import CodingAgentMemory
from .loop import CoEvolutionRunner

__all__ = ["CoEvolveConfig", "CodingAgentMemory", "CoEvolutionRunner"]
