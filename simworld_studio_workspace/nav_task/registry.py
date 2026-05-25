"""Lightweight measure registry: maps names to Measure instances."""

from __future__ import annotations

from typing import Dict, List, Optional

from .episode import NavigationEpisode
from .measures import Measure


class MeasureRegistry:
    """Registry that holds named Measure instances.

    Args:
        measures: Optional list of Measure instances to register at init.
    """

    def __init__(self, measures: Optional[List[Measure]] = None) -> None:
        self._measures: Dict[str, Measure] = {}
        if measures:
            for m in measures:
                self.register(m)

    def register(self, measure: Measure) -> None:
        """Register a measure, overwriting any existing entry with the same name.

        Args:
            measure: Measure instance whose name property is used as key.
        """
        self._measures[measure.name] = measure

    def compute_all(
        self,
        episode: NavigationEpisode,
        **runtime_kwargs,
    ) -> Dict[str, float]:
        """Run all registered measures and return a name → value mapping.

        Args:
            episode: Ground-truth episode.
            **runtime_kwargs: Forwarded verbatim to each Measure.compute.

        Returns:
            Dictionary of measure name → scalar value.
        """
        return {
            name: measure.compute(episode, **runtime_kwargs)
            for name, measure in self._measures.items()
        }

    def get(self, name: str) -> Measure:
        """Retrieve a measure by name.

        Args:
            name: Measure name.

        Returns:
            Registered Measure instance.

        Raises:
            KeyError: If no measure with name is registered.
        """
        if name not in self._measures:
            raise KeyError(f"No measure registered with name '{name}'")
        return self._measures[name]

    def __len__(self) -> int:
        return len(self._measures)

    def __contains__(self, name: str) -> bool:
        return name in self._measures
