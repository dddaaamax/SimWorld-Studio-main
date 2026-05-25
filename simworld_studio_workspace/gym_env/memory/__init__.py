"""Agent memory module.

Public surface:
  * :class:`AgentMemory`  — Protocol every backend implements.
  * :class:`NullMemory`   — the no-op backend (default).
  * :func:`build_memory`  — factory used by the runner / CLI.

Concrete backends live in sibling modules and are imported lazily so
that e.g. a user running ``--memory none`` doesn't need ``mem0ai``
installed.

Typical usage from the runner::

    from gym_env.memory import build_memory
    memory = build_memory("mem0", agent_id="GymNavAgent_0")
    memory.reset()                          # start of episode
    ctx = memory.query(user_text, k=5)      # before LLM call
    memory.insert(step_summary)             # after env step
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from .backend import AgentMemory, NullMemory, ReadOnlyMemory

__all__ = ["AgentMemory", "NullMemory", "ReadOnlyMemory", "build_memory"]


def build_memory(
    kind: str = "none",
    *,
    agent_id: str = "default",
    config: Optional[Dict[str, Any]] = None,
    llm_model: Optional[str] = None,
    llm_base_url: Optional[str] = None,
    llm_api_key: Optional[str] = None,
) -> AgentMemory:
    """Construct a memory backend by short name.

    Parameters
    ----------
    kind
        ``"none"`` → :class:`NullMemory` (default, no-op, no deps).
        ``"mem0"`` → :class:`Mem0Memory` (requires ``pip install mem0ai``).
    agent_id
        Logical identity of the agent — used by backends that support
        per-agent memory scoping.
    config
        Backend-specific config dict, passed through verbatim.  When
        ``None`` (the default) the backend builds a sensible default
        from the ``llm_*`` args below.
    llm_model, llm_base_url, llm_api_key
        Optional LLM endpoint to use for the memory backend's own
        internal calls (mem0 uses an LLM to extract facts from each
        observation).  Pass the **same** values as you give the agent
        LLM so the experiment has a single source of truth: one
        endpoint, one model, both for the agent and its memory.
    """
    kind = (kind or "none").lower()
    if kind in ("none", "null", "off", "disabled"):
        return NullMemory()
    if kind == "text":
        from .text_backend import TextMemory
        path = (config or {}).get("path", "agent_memory.json")
        return TextMemory(path=path)
    if kind == "mem0":
        from .mem0_backend import Mem0Memory
        return Mem0Memory(
            agent_id=agent_id,
            config=config,
            llm_model=llm_model,
            llm_base_url=llm_base_url,
            llm_api_key=llm_api_key,
        )
    if kind == "strategy":
        from .strategy_backend import StrategyMemory
        default_path = f"strategy_memory_{agent_id}.json" if agent_id != "default" else "strategy_memory.json"
        path = (config or {}).get("path", default_path)
        warmup = (config or {}).get("warmup")
        llm_call = None
        if llm_base_url and llm_model:
            from openai import OpenAI
            _client = OpenAI(
                api_key=llm_api_key or "EMPTY",
                base_url=llm_base_url,
            )
            def llm_call(prompt: str) -> str:
                resp = _client.chat.completions.create(
                    model=llm_model,
                    messages=[{"role": "user", "content": prompt}],
                    max_tokens=4096,
                    temperature=0.3,
                    timeout=120,
                )
                return resp.choices[0].message.content or ""
        return StrategyMemory(path=path, llm_call=llm_call, warmup=warmup)
    if kind == "hierarchical":
        from .hierarchical import HierarchicalMemory
        cfg = config or {}
        return HierarchicalMemory(
            persist_dir=cfg.get("persist_dir", "./nav_memory"),
            llm_call=cfg.get("llm_call"),
        )
    raise ValueError(f"Unknown memory kind: {kind!r}")
