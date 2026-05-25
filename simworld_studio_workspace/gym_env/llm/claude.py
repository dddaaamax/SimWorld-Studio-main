"""Anthropic SDK adapter.

Why a dedicated adapter (vs the OpenAI-compat client): Claude exposes
``thinking`` content blocks in messages.create responses that we want
to capture for experiment logs.  The OpenAI-compat layer drops them.
"""

from __future__ import annotations

import base64
import io
import logging
import os
from typing import Any, Dict, List, Optional

import numpy as np

from .base import LLMClient, LLMMessage, LLMResponse, ToolCall

log = logging.getLogger(__name__)


class ClaudeClient(LLMClient):
    name = "claude"

    def __init__(
        self,
        *,
        model: str = "claude-opus-4-6",
        api_key: Optional[str] = None,
        thinking_budget_tokens: Optional[int] = None,
    ) -> None:
        try:
            import anthropic
        except ImportError as exc:
            raise ImportError(
                "ClaudeClient requires `pip install anthropic`"
            ) from exc
        self.model = model
        self._client = anthropic.Anthropic(
            api_key=api_key or os.environ.get("ANTHROPIC_API_KEY"),
        )
        self._thinking_budget = thinking_budget_tokens

    # ------------------------------------------------------------------

    def chat(
        self,
        messages: List[LLMMessage],
        tools: List[Dict[str, Any]],
        *,
        max_tokens: int = 1024,
        temperature: float = 0.0,
    ) -> LLMResponse:
        system_prompt, anthropic_msgs = self._convert_messages(messages)
        anthropic_tools = [
            {
                "name": t["name"],
                "description": t["description"],
                "input_schema": t["parameters"],
            }
            for t in tools
        ]

        kwargs: Dict[str, Any] = dict(
            model=self.model,
            max_tokens=max_tokens,
            temperature=temperature,
            messages=anthropic_msgs,
            tools=anthropic_tools,
        )
        if system_prompt:
            kwargs["system"] = system_prompt
        if self._thinking_budget:
            kwargs["thinking"] = {
                "type": "enabled",
                "budget_tokens": self._thinking_budget,
            }

        log.debug("[claude] sending %d messages, %d tools",
                  len(anthropic_msgs), len(anthropic_tools))
        resp = self._client.messages.create(**kwargs)

        return self._parse_response(resp)

    # ------------------------------------------------------------------

    @staticmethod
    def _convert_messages(messages: List[LLMMessage]):
        system_text_parts: List[str] = []
        out: List[Dict[str, Any]] = []
        for msg in messages:
            if msg.role == "system":
                for block in msg.content:
                    if block["type"] == "text":
                        system_text_parts.append(block["text"])
                continue

            if msg.role == "tool":
                out.append({
                    "role": "user",
                    "content": [{
                        "type": "tool_result",
                        "tool_use_id": msg.tool_call_id,
                        "content": [
                            b for b in _serialize_blocks_for_anthropic(msg.content)
                        ],
                    }],
                })
                continue

            if msg.role == "assistant":
                blocks: List[Dict[str, Any]] = []
                for block in msg.content:
                    if block["type"] == "text":
                        blocks.append({"type": "text", "text": block["text"]})
                for tc in msg.tool_calls:
                    blocks.append({
                        "type": "tool_use",
                        "id": tc.id,
                        "name": tc.name,
                        "input": tc.arguments,
                    })
                out.append({"role": "assistant", "content": blocks})
                continue

            # role == "user"
            out.append({
                "role": "user",
                "content": _serialize_blocks_for_anthropic(msg.content),
            })

        system_prompt = "\n\n".join(system_text_parts) if system_text_parts else None
        return system_prompt, out

    @staticmethod
    def _parse_response(resp) -> LLMResponse:
        text_parts: List[str] = []
        thinking_parts: List[str] = []
        tool_calls: List[ToolCall] = []
        for block in resp.content:
            btype = getattr(block, "type", None)
            if btype == "text":
                text_parts.append(block.text)
            elif btype == "thinking":
                thinking_parts.append(getattr(block, "thinking", "") or "")
            elif btype == "tool_use":
                tool_calls.append(ToolCall(
                    id=block.id,
                    name=block.name,
                    arguments=dict(block.input or {}),
                ))
        usage = {
            "input_tokens": getattr(resp.usage, "input_tokens", None),
            "output_tokens": getattr(resp.usage, "output_tokens", None),
        }
        try:
            raw = resp.model_dump()
        except Exception:
            raw = {"_repr": repr(resp)}
        return LLMResponse(
            text="\n".join(text_parts) if text_parts else None,
            tool_calls=tool_calls,
            reasoning="\n".join(thinking_parts) if thinking_parts else None,
            usage=usage,
            raw=raw,
        )


def _serialize_blocks_for_anthropic(blocks: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for block in blocks:
        if block["type"] == "text":
            out.append({"type": "text", "text": block["text"]})
        elif block["type"] == "image":
            out.append({
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": "image/png",
                    "data": _np_to_b64_png(block["image"]),
                },
            })
    return out


def _np_to_b64_png(arr: np.ndarray) -> str:
    from PIL import Image
    img = Image.fromarray(arr.astype(np.uint8))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode("ascii")
