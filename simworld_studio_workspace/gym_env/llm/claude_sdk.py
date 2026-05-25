"""Claude Agent SDK adapter.

Routes the chat() call through ``claude-agent-sdk`` instead of the
``anthropic`` SDK.  Two reasons you might prefer this:

  * **Auth via Claude Code login** — no ``ANTHROPIC_API_KEY`` needed.
    The SDK uses your local Claude Code installation's credentials.
  * **Future hooks into the agent runtime** — once we want Claude to
    e.g. inspect run logs between steps, we can swap built-in tools
    (Read, Bash, Glob) into ``allowed_tools`` and the same client works.

Adaptation notes
----------------
The agent SDK is built for **multi-step agent loops**, but our env's
step() wants exactly one nav action per chat() call.  We adapt by:

  1. Defining the 4 nav actions as in-process SDK MCP tools whose
     function bodies *record the call* and return a placeholder
     instead of doing the action.
  2. Running the agent with ``max_turns=1`` so Claude picks one tool,
     the SDK invokes our recorder, and the loop stops.
  3. Returning the recorded ToolCall(s) in our normalized
     :class:`LLMResponse`.

The actual UE action still happens later, in ``SimWorldNavEnv.step()``,
exactly the same path as for ClaudeClient.

Caveats
-------
* No vision: ``ClaudeSDKClient.query()`` takes a string prompt; we
  flatten the message history into text and drop image blocks (with a
  placeholder note).  If you need vision, use ``make_llm("claude")``.
* Each chat() call spawns a fresh agent session — no caching across
  steps.  Token usage is reported per-call.
* Runs the SDK's async event loop in a worker thread so chat() stays
  synchronous and works inside Jupyter notebooks.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from typing import Any, Dict, List, Optional

from .base import LLMClient, LLMMessage, LLMResponse, ToolCall

log = logging.getLogger(__name__)


class ClaudeAgentSDKClient(LLMClient):
    """LLMClient backed by ``claude-agent-sdk``."""

    name = "claude-sdk"

    def __init__(
        self,
        *,
        model: str = "claude-opus-4-6",
        max_turns: int = 2,
        permission_mode: str = "bypassPermissions",
        only_first_call: bool = True,
    ) -> None:
        """
        Parameters
        ----------
        max_turns : int, default 2
            Agent SDK loop iteration cap.  Empirically the SDK only
            invokes our recorder if max_turns >= 2 (turn 1 = Claude
            picks the tool, turn 2 = SDK runs the recorder + lets
            Claude react).  Lower values silently drop the tool call.
        only_first_call : bool, default True
            If True, ``LLMResponse.tool_calls`` contains only the
            **first** action Claude picked.  Subsequent calls Claude
            makes within the same chat() are dropped — our env wants
            one action per step.
        """
        try:
            import claude_agent_sdk  # noqa: F401
        except ImportError as exc:
            raise ImportError(
                "ClaudeAgentSDKClient requires `pip install claude-agent-sdk`"
            ) from exc
        self.model = model
        self._max_turns = max_turns
        self._permission_mode = permission_mode
        self._only_first_call = only_first_call

    # ------------------------------------------------------------------

    def chat(
        self,
        messages: List[LLMMessage],
        tools: List[Dict[str, Any]],
        *,
        max_tokens: int = 1024,
        temperature: float = 0.0,
    ) -> LLMResponse:
        # Run the async SDK in a worker thread so chat() stays sync and
        # we don't fight an existing event loop (Jupyter / runner).
        result_box: List[Any] = []
        error_box: List[BaseException] = []

        def runner() -> None:
            try:
                result_box.append(
                    asyncio.run(self._chat_async(messages, tools))
                )
            except BaseException as exc:  # noqa: BLE001
                error_box.append(exc)

        t = threading.Thread(target=runner, daemon=True)
        t.start()
        t.join()

        if error_box:
            raise error_box[0]
        return result_box[0]

    # ------------------------------------------------------------------

    async def _chat_async(
        self,
        messages: List[LLMMessage],
        tools: List[Dict[str, Any]],
    ) -> LLMResponse:
        from claude_agent_sdk import (
            AssistantMessage,
            ClaudeAgentOptions,
            ClaudeSDKClient,
            ResultMessage,
            SystemMessage,
            TextBlock,
            create_sdk_mcp_server,
            tool,
        )

        # ── 1. Build in-process MCP tools that record (not execute) ────
        recorded: List[ToolCall] = []

        def make_recorder(tool_name: str):
            async def _fn(args: Dict[str, Any]):
                recorded.append(
                    ToolCall(
                        id=f"sdk_call_{len(recorded)}",
                        name=tool_name,
                        arguments=dict(args or {}),
                    )
                )
                # The SDK requires a content list reply.  Empty schemas
                # → no useful side info to return; "ok" is enough.
                return {"content": [{"type": "text", "text": "ok"}]}
            return _fn

        sdk_tools = []
        tool_names: List[str] = []
        for t in tools:
            name = t["name"]
            desc = t["description"]
            # Our nav tools are param-free; the dict-schema form supports
            # an empty {} for that case.  If we ever add params we'd map
            # JSON-schema types to Python types here.
            decorated = tool(name, desc, {})(make_recorder(name))
            sdk_tools.append(decorated)
            tool_names.append(name)

        server = create_sdk_mcp_server("nav-tools", tools=sdk_tools)

        # ── 2. Flatten our message history into the SDK's text prompt ──
        system_prompt, user_prompt = self._split_messages(messages)

        # In-process MCP tools register as "mcp__<server>__<tool>"
        allowed = [f"mcp__nav-tools__{n}" for n in tool_names]

        options = ClaudeAgentOptions(
            model=self.model,
            mcp_servers={"nav-tools": server},
            allowed_tools=allowed,
            disallowed_tools=[],  # explicit empty
            max_turns=self._max_turns,
            permission_mode=self._permission_mode,
            system_prompt=system_prompt or None,
        )

        # ── 3. Run the agent for one turn ──────────────────────────────
        text_parts: List[str] = []
        usage: Dict[str, Any] = {}
        raw_messages: List[Dict[str, Any]] = []
        stop_reason: Optional[str] = None

        async with ClaudeSDKClient(options=options) as client:
            await client.query(user_prompt)
            async for message in client.receive_response():
                raw_messages.append(_serialize_msg(message))

                if isinstance(message, AssistantMessage):
                    for block in message.content:
                        if isinstance(block, TextBlock):
                            text_parts.append(block.text)
                    if getattr(message, "usage", None):
                        u = message.usage
                        for k in ("input_tokens", "output_tokens",
                                  "cache_read_input_tokens",
                                  "cache_creation_input_tokens"):
                            if k in u and u[k] is not None:
                                usage[k] = usage.get(k, 0) + int(u[k])

                elif isinstance(message, ResultMessage):
                    stop_reason = getattr(message, "stop_reason", None) \
                        or stop_reason
                    if hasattr(message, "result") and not text_parts:
                        # ResultMessage carries final text when the agent
                        # produced any (e.g. when no tool was called).
                        text_parts.append(str(message.result))

                elif isinstance(message, SystemMessage):
                    pass  # init / metadata — already in raw_messages

        if stop_reason:
            usage["stop_reason"] = stop_reason

        # Optionally drop subsequent tool calls — env.step() expects one
        # action per chat() invocation, and Claude in agent mode often
        # calls the tool 2-3 times in a row before producing final text.
        out_calls = recorded[:1] if self._only_first_call else recorded

        return LLMResponse(
            text="\n".join(text_parts) if text_parts else None,
            tool_calls=out_calls,
            reasoning=None,
            usage=usage,
            raw={"messages": raw_messages, "all_recorded": [
                {"name": tc.name, "arguments": tc.arguments} for tc in recorded
            ]},
        )

    # ------------------------------------------------------------------

    @staticmethod
    def _split_messages(messages: List[LLMMessage]) -> tuple[str, str]:
        """Pull the system message out, flatten everything else to text."""
        system_parts: List[str] = []
        body_parts: List[str] = []
        for m in messages:
            for b in m.content:
                if b["type"] == "text":
                    text = b["text"]
                elif b["type"] == "image":
                    text = "(image omitted — claude-sdk mode is text-only)"
                else:
                    text = f"({b['type']} block)"
                if m.role == "system":
                    system_parts.append(text)
                elif m.role == "user":
                    body_parts.append(f"USER: {text}")
                elif m.role == "assistant":
                    body_parts.append(f"ASSISTANT: {text}")
                elif m.role == "tool":
                    body_parts.append(f"TOOL_RESULT: {text}")
        return "\n\n".join(system_parts), "\n\n".join(body_parts)


def _serialize_msg(msg: Any) -> Dict[str, Any]:
    """Best-effort dict view of an SDK message for raw logging."""
    out: Dict[str, Any] = {"_class": type(msg).__name__}
    for attr in ("subtype", "stop_reason", "result", "data", "session_id"):
        if hasattr(msg, attr):
            try:
                val = getattr(msg, attr)
                if val is not None:
                    out[attr] = val if isinstance(val, (str, int, float, bool, dict, list)) else repr(val)
            except Exception:
                pass
    if hasattr(msg, "content"):
        try:
            out["content"] = [
                {"type": getattr(b, "type", None) or type(b).__name__,
                 "text": getattr(b, "text", None)}
                for b in msg.content
            ]
        except Exception:
            pass
    return out
