"""SimWorld Gym Environment for embodied LLM navigation experiments.

A Gym-style wrapper around the SimWorld UE simulator that:

  * Bypasses the JS server / agent panel entirely (no Claude CLI dependency).
  * Supports any LLM via a unified ``LLMClient`` interface.
  * Plugs into ``task_gen.nav_task`` (the navigation task subsystem) for
    rewards and metrics.  Uses ``EuclideanNavigationInterface`` because
    scenes here are dynamically built by coding agents — no road graph.
  * Talks to UE through UnrealCV (PIE-mode safe) and only touches MCP for
    pre-PIE scene queries.

Typical use::

    from gym_env import SimWorldNavEnv, make_llm, run_episode, EpisodeLogger
    from gym_env.episode_builder import sample_pointnav_episode
    from gym_env.ucv_client import UCVClient

    ucv = UCVClient(); ucv.connect()
    episode = sample_pointnav_episode(ucv, seed=42)
    env = SimWorldNavEnv(ucv_client=ucv)
    llm = make_llm("claude")
    logger = EpisodeLogger(run_name="my_run")
    run_episode(env, llm, episode, logger)
"""

from __future__ import annotations

# ── Encoding fix: harden stdout/stderr against non-ASCII output ─────────
#
# The third-party ``unrealcv`` library prints localized OS error strings
# from inside its background receive thread when a socket dies (see
# ``unrealcv/__init__.py:61``).  On Windows the localized strings come
# back in the system code page (e.g. cp936), which then crashes Python's
# stdout encoder if stdout is anything other than UTF-8.  When that
# print crashes, the receive thread dies *without* draining its socket,
# and the next ``request()`` we issue waits forever for a response
# that's stranded in dead-thread state.
#
# We do TWO things to neutralise this entire failure mode:
#   1. Reconfigure stdout/stderr to utf-8 errors='replace' so even if
#      the print does run, it never raises.
#   2. Monkey-patch ``unrealcv.SocketMessage.ReceivePayload`` to swallow
#      print errors entirely.  Belt and suspenders — some test runners
#      capture stdout in cp1252 wrappers that ignore reconfigure().
#
# Both run at package import time so they take effect before any
# downstream module imports unrealcv.

import sys as _sys
for _stream_name in ("stdout", "stderr"):
    _stream = getattr(_sys, _stream_name, None)
    if _stream is not None and hasattr(_stream, "reconfigure"):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
del _sys, _stream_name, _stream


def _patch_unrealcv() -> None:
    """Patch known unrealcv crash/deadlock paths in its receive thread.

    The third-party ``unrealcv`` library has two distinct bugs that
    surface together whenever UE closes the socket mid-request:

      1. ``SocketMessage.ReceivePayload`` does a bare ``print(...)``
         of the localized OS error string.  On a Windows shell with
         cp1252 stdout, this raises ``UnicodeEncodeError``.
      2. ``BaseClient.disconnect()`` ends with ``self.t.join()``.
         When this is reached from **inside** ``self.t`` itself (the
         receive thread calls disconnect() after seeing EOF), join
         raises ``RuntimeError: cannot join current thread``.

    Either bug, on its own, kills the receive thread WITHOUT draining
    the socket — the next ``request()`` we send waits forever for a
    response that nobody is listening for.  We monkey-patch BOTH:

      * Wrap ``ReceivePayload`` so its ``print`` calls can never raise.
      * Replace ``disconnect`` with a version that skips
        ``self.t.join()`` when called from inside the receive thread.

    Idempotent and best-effort — failure to patch logs a warning but
    does not block import.
    """
    try:
        import threading as _threading
        import unrealcv as _ucv

        if getattr(_ucv, "_gym_env_patched", False):
            return

        # ── Patch 1: safe ReceivePayload print ─────────────────────
        SocketMessage = _ucv.SocketMessage
        _orig_receive = SocketMessage.ReceivePayload

        @classmethod
        def _safe_receive(cls, sock):
            import builtins
            _orig_print = builtins.print

            def _safe_print(*args, **kwargs):
                try:
                    _orig_print(*args, **kwargs)
                except Exception:
                    pass

            builtins.print = _safe_print
            try:
                return _orig_receive.__func__(cls, sock)
            finally:
                builtins.print = _orig_print

        SocketMessage.ReceivePayload = _safe_receive

        # ── Patch 2: disconnect-from-receive-thread deadlock ───────
        # NOTE: the class is ``unrealcv.Client``, not ``BaseClient``
        # — error messages in the source still say "BaseClient" but
        # the class definition is just ``class Client``.
        Client = _ucv.Client

        def _safe_disconnect(self):
            # Reproduce the original logic, but never join the
            # receive thread when we're already inside it.
            try:
                if self.isconnected():
                    try:
                        import socket as _sock
                        self.sock.shutdown(_sock.SHUT_RD)
                    except Exception:
                        pass
                    if self.sock is not None:
                        try:
                            self.sock.close()
                        except Exception:
                            pass
                        self.sock = None
                    import time as _time
                    _time.sleep(0.1)
            except Exception:
                pass

            t = getattr(self, "t", None)
            if t is not None and t.is_alive():
                # Wake the receive loop's queue so it can exit.
                try:
                    self.recv_num_q.put(None)
                except Exception:
                    pass
                # Only join if we're NOT the receive thread.
                if t is not _threading.current_thread():
                    try:
                        t.join(timeout=2.0)
                    except Exception:
                        pass

        Client.disconnect = _safe_disconnect

        _ucv._gym_env_patched = True
    except Exception as exc:
        import logging as _l
        _l.getLogger(__name__).warning(
            "could not patch unrealcv safety wrappers: %s", exc,
        )


_patch_unrealcv()
del _patch_unrealcv

# Public API surface — keep narrow and stable.
from .simworld_nav_env import SimWorldNavEnv
from .ucv_client import UCVClient
from .mcp_client import MCPClient
from .episode_builder import sample_pointnav_episode, sample_objectnav_episode
from .action_space import nav_tool_schemas, translate_action, NAV_TOOL_NAMES
from .logger import EpisodeLogger
from .runner import run_episode
from .llm import make_llm, LLMClient, LLMResponse

__all__ = [
    "SimWorldNavEnv",
    "UCVClient",
    "MCPClient",
    "sample_pointnav_episode",
    "sample_objectnav_episode",
    "nav_tool_schemas",
    "translate_action",
    "NAV_TOOL_NAMES",
    "EpisodeLogger",
    "run_episode",
    "make_llm",
    "LLMClient",
    "LLMResponse",
]
