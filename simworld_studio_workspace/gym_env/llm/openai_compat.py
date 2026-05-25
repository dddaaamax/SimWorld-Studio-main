"""OpenAI-compatible LLM client.

Handles three model families through one code path by varying
``base_url`` + ``api_key``:

  * GPT (default OpenAI endpoint)
  * Gemini (Google's OpenAI-compatible endpoint)
  * Qwen (DashScope OpenAI-compatible endpoint)

All three accept tool schemas in the standard OpenAI format and emit
``tool_calls`` blocks.  This module deliberately does not try to map
vendor-specific reasoning fields — if a provider exposes them later we
can add a small probe in :meth:`_extract_reasoning`.
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


_API_KEY_ENV = {
    "gpt": ("OPENAI_API_KEY",),
    "gemini": ("GEMINI_API_KEY", "GOOGLE_API_KEY"),
    "qwen": ("DASHSCOPE_API_KEY", "QWEN_API_KEY"),
}


class OpenAICompatClient(LLMClient):
    """OpenAI Python SDK pointed at any OpenAI-compatible endpoint."""

    def __init__(
        self,
        *,
        name: str,
        model: str,
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
        text_action_mode: bool = False,
    ) -> None:
        try:
            from openai import OpenAI
        except ImportError as exc:
            raise ImportError("OpenAICompatClient requires `pip install openai`") from exc

        self.name = name
        self.model = model
        if api_key is None:
            for env in _API_KEY_ENV.get(name, ("OPENAI_API_KEY",)):
                api_key = os.environ.get(env)
                if api_key:
                    break
        # Defer client construction to per-call: reusing a single httpx pool
        # across unrealcv.connect() on Windows reproduces WinError 10061 on
        # every subsequent outbound request in the same process. See
        # co_evolve/loop.py::_make_llm_call for the same mitigation.
        self._api_key = api_key
        self._base_url = base_url
        self._openai_cls = OpenAI
        self._text_action_mode = text_action_mode

    @property
    def _client(self):
        return self._openai_cls(api_key=self._api_key, base_url=self._base_url)

    # ------------------------------------------------------------------

    def chat(
        self,
        messages: List[LLMMessage],
        tools: List[Dict[str, Any]],
        *,
        max_tokens: int = 1024,
        temperature: float = 0.0,
    ) -> LLMResponse:
        oai_messages = self._convert_messages(messages)
        oai_tools = [
            {
                "type": "function",
                "function": {
                    "name": t["name"],
                    "description": t["description"],
                    "parameters": t["parameters"],
                },
            }
            for t in tools
        ]

        # Try with tool_calls first.  If the server doesn't support
        # tools (vLLM without --enable-auto-tool-choice), fall back to
        # plain text where we parse the action name from the response.
        if not self._text_action_mode:
            try:
                log.debug("[%s] sending %d messages, %d tools",
                          self.name, len(oai_messages), len(oai_tools))
                resp = self._client.chat.completions.create(
                    model=self.model,
                    messages=oai_messages,
                    tools=oai_tools,
                    max_tokens=max_tokens,
                    temperature=temperature,
                    timeout=120,
                )
                result = self._parse_response(resp)
                # vLLM without --enable-auto-tool-choice accepts tools
                # param but returns empty tool_calls.  The model may have
                # embedded the action in text (Qwen <tool_call> XML).
                if not result.tool_calls and result.text:
                    tool_names = [t["name"] for t in tools]
                    fallback = self._parse_text_action(resp, tool_names)
                    if fallback.tool_calls:
                        return fallback
                return result
            except Exception as exc:
                if "tool" in str(exc).lower() and "400" in str(exc):
                    log.warning(
                        "[%s] server rejected tools param (%s); "
                        "switching to text-action mode for this session",
                        self.name, exc,
                    )
                    self._text_action_mode = True
                else:
                    raise

        # ── Text-action fallback: tell the model which action names are
        # valid and how to format its answer.  Do NOT inject a navigation
        # strategy / decision tree — the model should decide based on
        # the observation (text + image), and any domain guidance
        # belongs in the upstream system prompt (e.g. the navigation
        # agent's base prompt) or in the memory / rethink hints the
        # runner already prepends.  Embedding a hard bearing rule here
        # would turn every fallback-mode run into a rule-based baseline
        # regardless of whether RGB, thinking, or memory is in play.
        tool_names = [t["name"] for t in tools]
        tool_desc = ", ".join(tool_names)
        inject = (
            f"\n\nAvailable actions (reply with exactly one of these "
            f"names, nothing else): {tool_desc}"
        )
        patched = list(oai_messages)
        if patched and patched[0].get("role") == "system":
            patched[0] = dict(patched[0])
            patched[0]["content"] = patched[0]["content"] + inject
        else:
            patched.insert(0, {"role": "system", "content": inject.strip()})

        # In text-action mode there are no real tool_call_ids.  Convert
        # role=tool → role=user and role=assistant with tool_calls →
        # plain assistant text so the model sees a clean user/assistant
        # alternation with action feedback.  IMPORTANT: we preserve
        # list-form content (text + image_url blocks) — vision models
        # like Qwen-VL accept mixed content on OpenAI-compatible
        # endpoints without any tool-choice plumbing, and dropping the
        # image here was silently turning RGB runs into text-only.
        cleaned: List[Dict[str, Any]] = []
        for m in patched:
            if m.get("role") == "tool":
                # Merge tool result into a user message; keep content as-is
                # whether it's a plain string or a list of blocks.
                cleaned.append({"role": "user", "content": m.get("content", "ok")})
            elif m.get("role") == "assistant" and m.get("tool_calls"):
                # Strip tool_calls; keep the text portion as plain string.
                cleaned.append({"role": "assistant", "content": m.get("content") or ""})
            else:
                cleaned.append(dict(m))

        # Merge consecutive same-role messages.  Many providers (Anthropic,
        # Google, some vLLM configs) require strict user/assistant
        # alternation, so we concatenate adjacent messages of the same
        # role.  Text ↔ text concatenates with a newline; anything
        # involving a list (image blocks) normalises both sides to lists
        # so image_url dicts survive.
        def _as_blocks(c) -> List[Dict[str, Any]]:
            """Normalise content into a list of message blocks."""
            if c is None or c == "":
                return []
            if isinstance(c, str):
                return [{"type": "text", "text": c}]
            if isinstance(c, list):
                out: List[Dict[str, Any]] = []
                for b in c:
                    if isinstance(b, dict):
                        out.append(b)
                    elif isinstance(b, str):
                        out.append({"type": "text", "text": b})
                return out
            return [{"type": "text", "text": str(c)}]

        def _merge_content(a, b):
            # If both are plain strings, keep string form (smaller).
            if isinstance(a, str) and isinstance(b, str):
                return a + "\n" + b
            ab = _as_blocks(a) + _as_blocks(b)
            return ab if ab else ""

        merged: List[Dict[str, Any]] = []
        for m in cleaned:
            if merged and merged[-1]["role"] == m["role"]:
                merged[-1]["content"] = _merge_content(
                    merged[-1].get("content"), m.get("content"),
                )
            else:
                merged.append(dict(m))
        patched = merged

        log.debug("[%s] text-action mode: %d messages", self.name, len(patched))
        # Cap output tokens in text-action mode.  Thinking models
        # (e.g. Qwen3-VL-*-Thinking) emit a full <think>…</think> chain
        # before the action and easily need 2–4k tokens; capping them
        # tightly truncates the reasoning mid-thought and the parser
        # finds no action name (step becomes "no_tool_call").  Non
        # thinking models need ~3 tokens for the action word and a
        # small cap saves latency.
        # Only bump max_tokens for models that are explicitly marked as
        # "thinking" variants (chain-of-thought output).  Qwen3.5 is a
        # hybrid model whose thinking is gated by a chat-template kwarg
        # (`enable_thinking`), so leave its cap at the small action-name
        # budget unless the model name spells out "thinking".
        is_thinking = "thinking" in self.model.lower()
        # 512 tokens cover a brief preamble + the action name without
        # the model hitting `finish_reason=length` mid-sentence.
        # 256 was too tight: Qwen3.5 always emits a brief analysis even
        # with enable_thinking=False, and the parser then sees no action
        # token at all (→ no_tool_call). Empirically epoch_010 had 30%
        # no_tool_call at 256.
        text_max = max(max_tokens, 4096) if is_thinking else min(max_tokens, 512)

        # Disable thinking by default for Qwen3 hybrid models — each
        # ghost just needs to emit an action name.  vLLM serving Qwen3
        # accepts this via extra_body -> chat_template_kwargs and falls
        # back gracefully for models that don't recognise it.
        extra_body: Dict[str, Any] = {}
        if "qwen3" in self.model.lower() and not is_thinking:
            extra_body["chat_template_kwargs"] = {"enable_thinking": False}

        resp = self._client.chat.completions.create(
            model=self.model,
            messages=patched,
            max_tokens=text_max,
            temperature=temperature,
            timeout=60,
            extra_body=extra_body or None,
        )
        return self._parse_text_action(resp, tool_names)

    # ------------------------------------------------------------------

    @staticmethod
    def _convert_messages(messages: List[LLMMessage]) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        for msg in messages:
            if msg.role == "tool":
                # OpenAI requires content to be a string for role=tool
                text_blocks = [b["text"] for b in msg.content if b["type"] == "text"]
                out.append({
                    "role": "tool",
                    "tool_call_id": msg.tool_call_id,
                    "content": "\n".join(text_blocks) or "ok",
                })
                continue

            if msg.role == "assistant":
                m: Dict[str, Any] = {"role": "assistant"}
                text_blocks = [b["text"] for b in msg.content if b["type"] == "text"]
                if text_blocks:
                    m["content"] = "\n".join(text_blocks)
                else:
                    m["content"] = None
                if msg.tool_calls:
                    m["tool_calls"] = [
                        {
                            "id": tc.id,
                            "type": "function",
                            "function": {
                                "name": tc.name,
                                "arguments": _json_dumps(tc.arguments),
                            },
                        }
                        for tc in msg.tool_calls
                    ]
                out.append(m)
                continue

            # system / user
            content = _convert_content_blocks(msg.content)
            if msg.role == "system":
                # Some providers require text-only system content; flatten.
                text_only = "\n".join(
                    b["text"] for b in content if b.get("type") == "text"
                )
                out.append({"role": "system", "content": text_only})
            else:
                out.append({"role": "user", "content": content})
        return out

    @staticmethod
    def _parse_response(resp) -> LLMResponse:
        choice = resp.choices[0]
        msg = choice.message
        tool_calls: List[ToolCall] = []
        for tc in (msg.tool_calls or []):
            try:
                args = _json_loads(tc.function.arguments or "{}")
            except Exception:
                args = {}
            tool_calls.append(ToolCall(
                id=tc.id,
                name=tc.function.name,
                arguments=args,
            ))
        usage = {}
        if getattr(resp, "usage", None):
            usage = {
                "input_tokens": resp.usage.prompt_tokens,
                "output_tokens": resp.usage.completion_tokens,
                "total_tokens": resp.usage.total_tokens,
            }
        try:
            raw = resp.model_dump()
        except Exception:
            raw = {"_repr": repr(resp)}
        return LLMResponse(
            text=msg.content,
            tool_calls=tool_calls,
            reasoning=None,  # most OpenAI-compat providers don't expose this
            usage=usage,
            raw=raw,
        )

    @staticmethod
    def _parse_text_action(resp, valid_names: List[str]) -> LLMResponse:
        """Parse an action name from the model's plain-text response.

        Scans lines for one that exactly matches a known action name.
        Falls back to scanning for the name anywhere in the text.
        """
        import uuid
        choice = resp.choices[0]
        text = choice.message.content or ""

        # Try exact line match first (most reliable)
        found = None
        for line in text.strip().splitlines():
            cleaned = line.strip().upper()
            if cleaned in valid_names:
                found = cleaned
                break
        # Fallback: find first occurrence of any action name in text
        if found is None:
            upper = text.upper()
            for name in valid_names:
                if name in upper:
                    found = name
                    break

        tool_calls: List[ToolCall] = []
        if found:
            tool_calls.append(ToolCall(
                id=f"text_{uuid.uuid4().hex[:8]}",
                name=found,
                arguments={},
            ))

        usage = {}
        if getattr(resp, "usage", None):
            usage = {
                "input_tokens": resp.usage.prompt_tokens,
                "output_tokens": resp.usage.completion_tokens,
                "total_tokens": resp.usage.total_tokens,
            }
        try:
            raw = resp.model_dump()
        except Exception:
            raw = {"_repr": repr(resp)}
        return LLMResponse(
            text=text,
            tool_calls=tool_calls,
            reasoning=None,
            usage=usage,
            raw=raw,
        )


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _convert_content_blocks(blocks: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for b in blocks:
        if b["type"] == "text":
            out.append({"type": "text", "text": b["text"]})
        elif b["type"] == "image":
            data_url = "data:image/png;base64," + _np_to_b64_png(b["image"])
            out.append({
                "type": "image_url",
                "image_url": {"url": data_url},
            })
    return out


def _np_to_b64_png(arr: np.ndarray) -> str:
    from PIL import Image
    img = Image.fromarray(arr.astype(np.uint8))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode("ascii")


def _json_dumps(obj):
    import json
    return json.dumps(obj, ensure_ascii=False)


def _json_loads(s):
    import json
    return json.loads(s)
