"""Simple text-list memory backend.

Stores memories as plain strings in a list.  On ``query()`` returns
the most recent ``k`` items (no embedding, no search — just recency).
Persists to a JSON file between runs so memories survive across
process restarts.

This is the simplest possible memory that still shows the "learning
from experience" curve: after each episode the agent records what
worked / didn't, and on subsequent episodes those lessons appear in
the system prompt.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional

log = logging.getLogger(__name__)


class TextMemory:
    """Append-only text memory with JSON persistence."""

    name = "text"

    def __init__(self, path: str = "agent_memory.json", max_items: int = 50) -> None:
        self._path = Path(path)
        self._max = max_items
        self._items: List[str] = []
        self._load()

    def _load(self) -> None:
        if self._path.exists():
            try:
                data = json.loads(self._path.read_text(encoding="utf-8"))
                self._items = data.get("memories", [])[-self._max:]
                log.info("TextMemory: loaded %d memories from %s",
                         len(self._items), self._path)
            except Exception as exc:
                log.warning("TextMemory: failed to load %s: %s", self._path, exc)
                self._items = []
        else:
            self._items = []

    def _save(self) -> None:
        self._path.write_text(
            json.dumps({"memories": self._items[-self._max:]}, indent=2),
            encoding="utf-8",
        )

    def insert(self, text: str, metadata: Optional[Dict[str, Any]] = None) -> None:
        self._items.append(text)
        if len(self._items) > self._max:
            self._items = self._items[-self._max:]
        self._save()
        log.debug("TextMemory: inserted (%d total): %s", len(self._items), text[:80])

    def query(self, text: str, k: int = 5) -> List[str]:
        return self._items[-k:]

    def reset(self) -> None:
        # Don't clear — memories persist across episodes
        pass

    def clear(self) -> None:
        """Wipe all memories (for fresh experiments)."""
        self._items = []
        self._save()
