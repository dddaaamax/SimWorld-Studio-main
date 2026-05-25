"""mem0-backed implementation of :class:`AgentMemory`.

Wraps the OSS ``mem0ai`` library.  Kept deliberately thin: the runner
calls ``insert`` / ``query`` and this file translates to mem0's
``add`` / ``search``.  mem0 internally does the LLM-based fact
extraction and vector storage — we don't second-guess it.

``mem0ai`` is imported lazily so projects that never use this backend
don't need the dependency installed.

Config
------
``_default_config()`` builds a fully local stack that needs nothing
beyond the experiment's own LLM endpoint:

  * **LLM** = OpenAI-compat client pointed at whatever endpoint serves
    the experiment's own VLM.  Picked up from env vars
    ``MEM0_LLM_BASE_URL`` / ``MEM0_LLM_MODEL`` / ``MEM0_LLM_API_KEY``
    so the runner CLI can forward its ``--base-url`` etc. to mem0
    without us having to thread the args through every layer.
    Falls back to ``OPENAI_BASE_URL`` / ``OPENAI_API_KEY`` so a normal
    OpenAI key still works.
  * **Embedder** = ``fastembed`` (in-process ONNX, ~30 MB).  No
    extra service to run, no remote ``/v1/embeddings`` requirement —
    important because most vLLM deployments serve chat-completions
    only.
  * **Vector store** = local Chroma at ``./.simworld_memory/chroma``.

Pass a custom ``config`` dict to :class:`Mem0Memory` to override any
of these.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from .backend import AgentMemory

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Custom prompts for embodied navigation
# ---------------------------------------------------------------------------
#
# mem0's default extractor prompt is written for chat assistants — it
# looks for "user preferences", names, dates, etc.  That's the wrong
# target for an embodied agent whose memories should be action-outcome
# causal patterns and control heuristics.
#
# We override `custom_fact_extraction_prompt` with a navigation-aware
# version.  IMPORTANT: the output schema must stay `{"facts": [...]}`
# because mem0/memory/main.py parses exactly that — see main.py:555
# `json.loads(cleaned_response)["facts"]`.
#
# We do NOT override `custom_update_memory_prompt` — the default
# ADD/UPDATE/DELETE/NONE semantics apply fine to text facts of any
# domain.

NAV_FACT_EXTRACTION_PROMPT = """\
You are a Trajectory Causal-Pattern Extractor for an embodied
navigation agent operating in a 3D city scene.  You will be given one
or more step descriptions from the agent's trajectory; each step
contains an action (MOVE_FORWARD / TURN_LEFT / TURN_RIGHT / STOP),
the reward, the distance to goal before and after the action, the
delta, and the agent's yaw.

Your job is to distill this into a small set of reusable, declarative
control memories that a *future* episode of the same agent could
consult to act better.  Focus on these memory types:

1. Action-Outcome Causal Patterns — concise cause-effect claims tying
   an action in a specific situation to its measured effect on d_goal,
   e.g. "MOVE_FORWARD when d_goal is decreasing typically reduces
   d_goal by 200-400 cm" or "TURN_LEFT while d_goal increases
   indicates the goal is now behind the agent".

2. Control Heuristics — general if-then rules derivable from the
   evidence, e.g. "When d_goal has not decreased for 3+ consecutive
   MOVE_FORWARDs, switch to a TURN action" or "STOP only when d_goal
   is below 200 cm".

3. Failure Modes — anti-patterns the step demonstrates, e.g.
   "Repeated TURN_LEFT with no forward motion oscillates yaw without
   changing d_goal" or "MOVE_FORWARD with delta near 0 indicates a
   wall or obstacle ahead".

Rules:
- Each fact must be a single self-contained English sentence in
  state-action-outcome form, including numeric thresholds where the
  step provides them.
- Do NOT record absolute coordinates, step indices, or episode IDs.
- Do NOT invent facts that are not supported by the step data.
- If nothing reliable can be extracted from the step, return an
  empty list.

Return ONLY a JSON object of the form:
{"facts": ["fact 1", "fact 2", ...]}
"""


def _default_config(
    *,
    llm_model: Optional[str] = None,
    llm_base_url: Optional[str] = None,
    llm_api_key: Optional[str] = None,
) -> Dict[str, Any]:
    """Build a fully local mem0 config from explicit args.

    The runner constructs this with the **same** model / base_url /
    api_key it's giving the agent LLM, so the experiment has a single
    source of truth: one ``--model``, one ``--base-url``, one
    ``--api-key`` on the CLI.  Both the agent and mem0's fact
    extractor talk to the same endpoint.

    Embedder is fastembed (in-process ONNX, ~30 MB) so we don't need
    a separate ``/v1/embeddings`` route on the agent's endpoint —
    most vLLM deployments serve chat-completions only.
    """
    llm_cfg: Dict[str, Any] = {
        "model": llm_model or "gpt-4o-mini",
        "temperature": 0.0,
        "api_key": llm_api_key or "EMPTY",  # vLLM ignores; client requires *some* str
    }
    if llm_base_url:
        llm_cfg["openai_base_url"] = llm_base_url

    return {
        "llm": {
            "provider": "openai",
            "config": llm_cfg,
        },
        "embedder": {
            "provider": "fastembed",
            "config": {"model": "BAAI/bge-small-en-v1.5"},
        },
        "vector_store": {
            "provider": "chroma",
            "config": {
                "collection_name": "simworld_agent",
                "path": "./.simworld_memory/chroma",
            },
        },
        "custom_fact_extraction_prompt": NAV_FACT_EXTRACTION_PROMPT,
    }


class Mem0Memory(AgentMemory):
    """mem0-backed memory, scoped by ``agent_id`` + per-episode ``run_id``."""

    name = "mem0"

    def __init__(
        self,
        *,
        agent_id: str = "default",
        config: Optional[Dict[str, Any]] = None,
        llm_model: Optional[str] = None,
        llm_base_url: Optional[str] = None,
        llm_api_key: Optional[str] = None,
    ) -> None:
        try:
            from mem0 import Memory  # type: ignore
        except ImportError as exc:
            raise ImportError(
                "mem0 backend requested but `mem0ai` is not installed. "
                "Install with: pip install mem0ai"
            ) from exc

        self._Memory = Memory
        if config is None:
            config = _default_config(
                llm_model=llm_model,
                llm_base_url=llm_base_url,
                llm_api_key=llm_api_key,
            )
        self._mem = Memory.from_config(config)
        self.agent_id = agent_id
        self._run_id: Optional[str] = None
        log.info(
            "Mem0Memory initialised (agent_id=%s, llm_model=%s, base_url=%s)",
            agent_id, llm_model or "default", llm_base_url or "default",
        )

    # ------------------------------------------------------------------
    # AgentMemory interface
    # ------------------------------------------------------------------

    def insert(self, text: str, metadata: Optional[Dict[str, Any]] = None) -> None:
        try:
            self._mem.add(
                [{"role": "user", "content": text}],
                agent_id=self.agent_id,
                run_id=self._run_id,
                metadata=metadata or {},
            )
        except Exception as exc:  # mem0 can raise provider errors; don't kill the loop
            log.warning("Mem0.insert failed: %s", exc)

    def query(self, text: str, k: int = 5) -> List[str]:
        try:
            res = self._mem.search(
                query=text,
                agent_id=self.agent_id,
                run_id=self._run_id,
                limit=k,
            )
        except Exception as exc:
            log.warning("Mem0.search failed: %s", exc)
            return []

        # mem0 returns either {"results": [...]} or a bare list depending
        # on version; handle both.
        items = res.get("results", res) if isinstance(res, dict) else res
        out: List[str] = []
        for item in items or []:
            if isinstance(item, dict) and "memory" in item:
                out.append(str(item["memory"]))
            elif isinstance(item, str):
                out.append(item)
        return out

    def reset(self) -> None:
        # New episode = new run scope.  Long-term (agent_id) memories
        # persist; per-run memories start fresh.
        import uuid
        self._run_id = f"ep-{uuid.uuid4().hex[:8]}"
        log.info("Mem0Memory reset: new run_id=%s", self._run_id)
