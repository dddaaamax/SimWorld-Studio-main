"""Vendor-neutral LLM interface used by the gym_env runner.

The runner stays vendor-agnostic by talking to subclasses of
:class:`LLMClient` only.  Each subclass is responsible for:

  1. Translating our :class:`LLMMessage` list to the vendor-native
     message shape (text + base64 image content blocks).
  2. Translating our normalized tool schema (the OpenAI-style dicts
     emitted by :func:`gym_env.action_space.nav_tool_schemas`) to the
     vendor-native tool schema.
  3. Parsing the vendor-native response back into our normalized
     :class:`LLMResponse` (text, tool calls, reasoning, raw, usage).

The "raw" field stores the unmodified provider response (as a JSON-
serializable dict) so the logger can persist it without re-querying.
"""

from __future__ import annotations

import abc
from dataclasses import dataclass, field
from typing import Any, Dict, List, Literal, Optional

import numpy as np


# ---------------------------------------------------------------------------
# Message + tool-call shapes
# ---------------------------------------------------------------------------

@dataclass
class LLMMessage:
    """A single chat turn in the runner-internal format.

    ``content`` is a list of content blocks; each block is either
    ``{"type": "text", "text": str}`` or
    ``{"type": "image", "image": np.ndarray}`` (uint8 H×W×3).
    Vendor adapters convert these to the SDK's native shape.
    """
    role: Literal["system", "user", "assistant", "tool"]
    content: List[Dict[str, Any]] = field(default_factory=list)
    # When role == "tool", this links back to the assistant's tool_call.
    tool_call_id: Optional[str] = None
    # When role == "assistant", record the tool_calls the model made
    # so the next turn's "tool" message can reference them.
    tool_calls: List["ToolCall"] = field(default_factory=list)

    @classmethod
    def text(cls, role: str, text: str) -> "LLMMessage":
        return cls(role=role, content=[{"type": "text", "text": text}])  # type: ignore[arg-type]

    @classmethod
    def user_with_image(cls, text: str, image: np.ndarray) -> "LLMMessage":
        return cls(
            role="user",
            content=[
                {"type": "text", "text": text},
                {"type": "image", "image": image},
            ],
        )

    @classmethod
    def user_with_images(
        cls, text: str, images: List[np.ndarray],
        captions: Optional[List[str]] = None,
    ) -> "LLMMessage":
        """User turn with multiple images (e.g. rgb + depth side-by-side).

        ``captions`` (optional) is a per-image label rendered as a text
        block right before the image — useful so the VLM knows which
        modality each picture represents.
        """
        blocks: List[Dict[str, Any]] = [{"type": "text", "text": text}]
        for i, img in enumerate(images):
            if captions and i < len(captions) and captions[i]:
                blocks.append({"type": "text", "text": captions[i]})
            blocks.append({"type": "image", "image": img})
        return cls(role="user", content=blocks)


@dataclass
class ToolCall:
    """A normalized tool call emitted by the model."""
    id: str
    name: str
    arguments: Dict[str, Any]

    def to_action_dict(self) -> Dict[str, Any]:
        """Shape expected by :func:`gym_env.action_space.translate_action`."""
        return {"tool": self.name, "params": self.arguments}


@dataclass
class LLMResponse:
    """Normalized LLM reply.

    ``raw`` is the provider response as a plain dict (JSON-serializable).
    The logger persists this verbatim so we can re-derive any field later.
    """
    text: Optional[str]
    tool_calls: List[ToolCall]
    reasoning: Optional[str]
    usage: Dict[str, Any]
    raw: Dict[str, Any]


# ---------------------------------------------------------------------------
# Abstract client
# ---------------------------------------------------------------------------

class LLMClient(abc.ABC):
    """Vendor-neutral chat client."""

    name: str  # short tag used in logs / run dirs
    model: str

    @abc.abstractmethod
    def chat(
        self,
        messages: List[LLMMessage],
        tools: List[Dict[str, Any]],
        *,
        max_tokens: int = 1024,
        temperature: float = 0.0,
    ) -> LLMResponse:
        """Single turn of chat with optional tool use."""
        ...
