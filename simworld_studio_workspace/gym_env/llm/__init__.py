"""LLM clients used by the gym_env runner.

Two backends cover four model families:

  * :class:`ClaudeClient` — native Anthropic SDK; preserves thinking
    blocks in ``LLMResponse.reasoning``.
  * :class:`OpenAICompatClient` — OpenAI Python SDK pointed at any
    OpenAI-compatible endpoint.  Used for GPT (default base URL),
    Gemini (Google's OpenAI compatibility layer), and Qwen (DashScope's
    OpenAI-compatible endpoint).

:func:`make_llm` is the user-facing factory.
"""

from __future__ import annotations

from .base import LLMClient, LLMResponse, LLMMessage, ToolCall


def make_llm(
    name: str,
    *,
    model: str | None = None,
    api_key: str | None = None,
    base_url: str | None = None,
    **kwargs,
) -> LLMClient:
    """Build an :class:`LLMClient` by short name.

    Recognised names:

      * ``claude``      — anthropic SDK (needs ``ANTHROPIC_API_KEY``).
        Supports vision and thinking blocks.
      * ``claude-sdk``  — claude-agent-sdk, auths via local Claude Code.
        No API key needed; text-only (no vision).
      * ``gpt``         — OpenAI default endpoint.
      * ``gemini``      — Google's OpenAI-compatible endpoint.
      * ``qwen``        — DashScope OpenAI-compatible endpoint.

    Pass ``model`` to override the default model id for that vendor.
    """
    n = name.lower()
    if n == "claude":
        from .claude import ClaudeClient
        return ClaudeClient(
            model=model or "claude-opus-4-6",
            api_key=api_key, **kwargs,
        )
    if n in ("claude-sdk", "claude_sdk", "claude-agent-sdk"):
        from .claude_sdk import ClaudeAgentSDKClient
        return ClaudeAgentSDKClient(
            model=model or "claude-opus-4-6",
            **kwargs,
        )
    if n in ("gpt", "openai"):
        from .openai_compat import OpenAICompatClient
        return OpenAICompatClient(
            name="gpt",
            model=model or "gpt-5",
            api_key=api_key,
            base_url=base_url,
            **kwargs,
        )
    if n in ("gemini", "google"):
        from .openai_compat import OpenAICompatClient
        return OpenAICompatClient(
            name="gemini",
            model=model or "gemini-2.5-pro",
            api_key=api_key,
            base_url=base_url or "https://generativelanguage.googleapis.com/v1beta/openai/",
            **kwargs,
        )
    if n in ("qwen", "dashscope"):
        from .openai_compat import OpenAICompatClient
        return OpenAICompatClient(
            name="qwen",
            model=model or "qwen3-vl-plus",
            api_key=api_key,
            base_url=base_url or "https://dashscope.aliyuncs.com/compatible-mode/v1",
            text_action_mode=True,
            **kwargs,
        )
    raise ValueError(
        f"unknown LLM name {name!r}; expected one of "
        "'claude', 'gpt', 'gemini', 'qwen'"
    )


__all__ = [
    "LLMClient", "LLMResponse", "LLMMessage", "ToolCall",
    "make_llm",
]
