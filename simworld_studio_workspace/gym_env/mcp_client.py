"""MCP TCP client for talking to UE's editor Python interpreter.

The SimWorld JS server uses the same channel via ``mcp-server.js``.  We
do NOT touch the JS server here — this client speaks the JSON-line
protocol (port 55558 by default) directly.

Lifecycle note
--------------
The UE editor exposes a Python interpreter via this TCP channel.  It
runs the script on the **editor world** game thread.  This is fully
available **before** PIE (Play-In-Editor) is started, and is in fact
the only way to start PIE programmatically (call
``LevelEditorSubsystem.editor_play_simulate()``).  Once PIE is
running, ``execute_python_script`` calls do still get *delivered*, but
they execute against the editor world (not the PIE game world), so
they cannot interact with PIE actors.

Use this client for:

  * Pre-experiment scene setup (spawn buildings via Studio API).
  * Starting / stopping PIE.
  * Editor-world introspection (e.g. ``get_actors_in_level``).

Use UnrealCV (``ucv_client.UCVClient``) for everything that happens
inside PIE — agent spawning, control, observations, camera capture.
"""

from __future__ import annotations

import json
import logging
import socket
import time
from typing import Any, Dict, Optional

log = logging.getLogger(__name__)


class MCPError(RuntimeError):
    """Raised when an MCP command fails or times out."""


def _is_soft_timeout_success(resp: Optional[dict]) -> bool:
    """Return True for the MCP bridge's false-negative timeout reply.

    On this UE build, ``execute_python_script`` can run the script and then
    return ``status=error`` because post-exec log capture timed out while
    reading ``Saved/Logs/CodingAgent.log``.  The caller still got a structured
    response, which means the editor Python side was reachable and the script
    was dispatched.
    """
    if not isinstance(resp, dict):
        return False
    if resp.get("status") != "error":
        return False
    error_text = str(resp.get("error", ""))
    return "Script execution timed out after" in error_text


class MCPClient:
    """One-shot JSON-over-TCP client for SimWorld's MCP server.

    Each call opens a new socket and closes it.  Cheap and stateless,
    matches the design of the JS server's ``ueCommand`` helper.
    """

    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 55558,
        timeout: float = 30.0,
        name: str = "mcp",
    ) -> None:
        self.host = host
        self.port = port
        self.timeout = timeout
        self.name = name

    # ------------------------------------------------------------------

    def call(self, cmd_type: str, params: Optional[Dict[str, Any]] = None,
             *, timeout: Optional[float] = None) -> Optional[dict]:
        """Send ``{type, params}`` and return the parsed JSON reply."""
        params = params or {}
        msg = json.dumps({"type": cmd_type, "params": params}) + "\n"
        log.debug("[%s] >> %s %s", self.name, cmd_type,
                  json.dumps(params)[:200])

        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(timeout if timeout is not None else self.timeout)
        try:
            sock.connect((self.host, self.port))
            sock.sendall(msg.encode("utf-8"))
            buf = ""
            while True:
                chunk = sock.recv(4096).decode("utf-8", errors="replace")
                if not chunk:
                    break
                buf += chunk
                try:
                    result = json.loads(buf)
                    log.debug("[%s] << %s", self.name, str(result)[:200])
                    return result
                except json.JSONDecodeError:
                    continue
            if buf.strip():
                return json.loads(buf)
            return None
        except (OSError, json.JSONDecodeError) as exc:
            raise MCPError(
                f"[{self.name}] {cmd_type} failed: {exc}"
            ) from exc
        finally:
            sock.close()

    # ------------------------------------------------------------------
    # Convenience wrappers — only the ones the env actually needs
    # ------------------------------------------------------------------

    def execute_python(self, script: str, timeout: float = 30.0) -> dict:
        """Run a Python snippet inside UE's editor interpreter.

        Returns the parsed reply from UE which typically looks like
        ``{"status": "ok"|"error", "result": {"python_logs": [...]}}``.
        """
        return self.call("execute_python_script", {"script": script},
                         timeout=timeout) or {}

    def get_actors_in_level(self) -> dict:
        """Return ``{actors: [...]}``.  Editor-world actors only.

        In PIE mode this still returns the editor world's actors
        (buildings, props), not the PIE-spawned agents — those must be
        queried via UnrealCV's ``vget /objects``.
        """
        return self.call("get_actors_in_level", {}, timeout=10) or {}

    # ------------------------------------------------------------------
    # PIE lifecycle
    # ------------------------------------------------------------------

    def is_pie_active(self) -> bool:
        """Best-effort check whether PIE is currently running.

        Uses ``GEditor.is_simulate_in_editor_in_progress()`` which is
        safe to call during PostLoad (unlike ``EditorLevelLibrary.get_game_world``
        which crashes with an assertion during PostLoad).
        """
        script = (
            "import unreal\n"
            "try:\n"
            "    ew = unreal.get_editor_subsystem(unreal.LevelEditorSubsystem)\n"
            "    if ew is not None:\n"
            "        # is_playing() is PostLoad-safe and reliably detects PIE\n"
            "        if ew.is_playing():\n"
            "            print('PIE_ACTIVE')\n"
            "        else:\n"
            "            print('PIE_INACTIVE')\n"
            "    else:\n"
            "        print('PIE_INACTIVE')\n"
            "except Exception as e:\n"
            "    print('PIE_CHECK_ERROR:' + repr(e))\n"
        )
        try:
            resp = self.execute_python(script, timeout=10)
        except MCPError as exc:
            log.warning("[%s] is_pie_active probe failed: %s", self.name, exc)
            return False
        logs = _extract_python_logs(resp)
        for line in logs:
            if "PIE_ACTIVE" in line:
                return True
            if "PIE_INACTIVE" in line or "PIE_CHECK_OK" in line:
                return False
        # Empty logs on a successful probe (MCP log-capture race on secondary
        # instance) — assume PIE not active so callers proceed with start_pie.
        return False

    def _wait_until_ready(self, timeout: float = 60.0) -> bool:
        """Wait until the editor is done loading and ready for commands.

        Polls with a lightweight Python snippet that avoids PostLoad-unsafe
        APIs.  Returns True if ready, False if timed out.
        """
        poll_script = (
            "import unreal\n"
            "try:\n"
            "    ss = unreal.get_editor_subsystem(unreal.LevelEditorSubsystem)\n"
            "    if ss is not None:\n"
            "        print('EDITOR_READY')\n"
            "    else:\n"
            "        print('EDITOR_NOT_READY')\n"
            "except:\n"
            "    print('EDITOR_NOT_READY')\n"
        )
        deadline = time.time() + timeout
        attempt = 0
        while time.time() < deadline:
            attempt += 1
            try:
                remaining = max(1.0, deadline - time.time())
                resp = self.execute_python(poll_script, timeout=min(35.0, remaining))
                logs = _extract_python_logs(resp)
                if any("EDITOR_READY" in l for l in logs):
                    log.info("[%s] editor ready after %d attempts", self.name, attempt)
                    return True
                # Secondary UE instance: python_logs can come back empty even on
                # success (MCP reads wrong rotated log file). Trust success bool.
                result = resp.get("result") if isinstance(resp, dict) else None
                if isinstance(result, dict) and result.get("success") and not logs:
                    log.info("[%s] editor ready (empty logs, success=true) attempt %d",
                             self.name, attempt)
                    return True
                if _is_soft_timeout_success(resp):
                    log.info("[%s] editor ready (soft-timeout MCP reply) attempt %d",
                             self.name, attempt)
                    return True
            except MCPError:
                pass
            time.sleep(2.0)
        return False

    def start_pie(self, *, wait_seconds: float = 5.0,
                  ready_timeout: float = 240.0) -> None:
        """Start PIE if it isn't already running.

        First waits for the editor to finish loading (PostLoad safe),
        then issues the PIE start command.  Sleeps ``wait_seconds``
        after starting so UnrealCV has time to initialise.
        """
        # Wait until editor is fully loaded — avoids PostLoad assertion crash
        if not self._wait_until_ready(timeout=ready_timeout):
            log.warning(
                "[%s] editor readiness probe timed out after %.1fs; "
                "continuing with direct PIE start",
                self.name,
                ready_timeout,
            )

        if self.is_pie_active():
            log.info("[%s] PIE already active", self.name)
            return

        log.info("[%s] starting PIE...", self.name)
        script = (
            "import unreal\n"
            "le = unreal.get_editor_subsystem(unreal.LevelEditorSubsystem)\n"
            "if le is None:\n"
            "    print('NO_LEVEL_EDITOR_SUBSYSTEM')\n"
            "else:\n"
            "    try:\n"
            "        le.editor_play_simulate()\n"
            "        print('PIE_START_REQUESTED')\n"
            "    except Exception as e:\n"
            "        print('PIE_START_FAILED:' + repr(e))\n"
        )
        try:
            self.execute_python(script, timeout=90)
        except MCPError as exc:
            raise MCPError(f"[{self.name}] PIE start failed: {exc}") from exc
        time.sleep(wait_seconds)

    def stop_pie(self, *, wait_seconds: float = 2.0) -> None:
        """Request PIE to end (editor returns to edit mode).

        No-op if PIE isn't running.  Uses
        ``LevelEditorSubsystem.editor_request_end_play()``.
        """
        if not self.is_pie_active():
            log.info("[%s] PIE not active, nothing to stop", self.name)
            return
        log.info("[%s] stopping PIE...", self.name)
        script = (
            "import unreal\n"
            "le = unreal.get_editor_subsystem(unreal.LevelEditorSubsystem)\n"
            "if le is None:\n"
            "    print('NO_LEVEL_EDITOR_SUBSYSTEM')\n"
            "else:\n"
            "    try:\n"
            "        le.editor_request_end_play()\n"
            "        print('PIE_END_REQUESTED')\n"
            "    except Exception as e:\n"
            "        print('PIE_END_FAILED:' + repr(e))\n"
        )
        try:
            self.execute_python(script, timeout=90)
        except MCPError as exc:
            log.warning("[%s] PIE stop failed: %s", self.name, exc)
        time.sleep(wait_seconds)


def _extract_python_logs(resp: Optional[dict]) -> list:
    """Pull the ``python_logs`` array out of an execute_python_script reply.

    UE replies have shape ``{status, result: {python_logs: [...]}}`` but
    older builds wrap differently, so we tolerate both.
    """
    if not resp:
        return []
    result = resp.get("result")
    if isinstance(result, dict) and "python_logs" in result:
        return list(result["python_logs"])
    if isinstance(resp.get("python_logs"), list):
        return list(resp["python_logs"])
    return []
