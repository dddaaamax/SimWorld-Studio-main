"""UnrealCV TCP client.

PIE-mode safe wrapper around the third-party ``unrealcv`` package.

Why this file exists rather than using ``unrealcv`` directly:

  1. Adds auto-reconnect with logged retries (UE drops the socket on
     spawn-heavy operations).
  2. Adds binary-payload helpers (``vget_camera``) — the upstream
     ``Client.request`` returns ``bytes`` for image commands and ``str``
     for everything else, which is easy to get wrong without a typed API.
  3. Centralises command logging so the runner / env can introspect every
     UE command from one place (essential for debugging).

Logging level discipline:

    DEBUG  — every command + truncated response
    INFO   — connect / reconnect / spawn lifecycle events
    WARN   — recovered transient errors (reconnects)
    ERROR  — final failures returned to caller

The class is intentionally not asyncio — the env step loop is sequential
and the UnrealCV protocol itself is request/response.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Optional

log = logging.getLogger(__name__)

_RESPONSE_PREVIEW_LEN = 200
_RECONNECT_DELAY_S = 1.5
_RECONNECT_MAX_ATTEMPTS = 10


def _force_unrealcv_threads_daemon() -> None:
    """Make unrealcv's receive threads daemon so Python can exit cleanly.

    The upstream unrealcv library creates its receive_loop_queue thread
    without ``daemon=True`` (see unrealcv/__init__.py, BaseClient.connect).
    Combined with :meth:`UCVClient.hard_reconnect` — which intentionally
    orphans stale clients to dodge a different unrealcv deadlock — every
    reconnect leaks a non-daemon thread blocked on ``socket.recv``.  At
    end-of-process those threads keep the interpreter alive forever, so
    ``python -m gym_env.batch_runner`` prints "results saved" and hangs.

    We patch ``threading.Thread`` inside the unrealcv module namespace so
    any thread unrealcv spawns inherits ``daemon=True``.  Idempotent; has
    no effect on threads created outside that module.
    """
    import unrealcv
    ucv_threading = getattr(unrealcv, "threading", None)
    if ucv_threading is None or getattr(ucv_threading.Thread, "_gymenv_daemon_patched", False):
        return
    _OrigThread = ucv_threading.Thread

    class _DaemonThread(_OrigThread):
        _gymenv_daemon_patched = True

        def __init__(self, *args, **kwargs):
            kwargs.setdefault("daemon", True)
            super().__init__(*args, **kwargs)

    ucv_threading.Thread = _DaemonThread


class UCVError(RuntimeError):
    """Raised when UnrealCV cannot be reached or returned an error."""


class UCVClient:
    """UnrealCV client with auto-reconnect.

    Parameters
    ----------
    host : str
    port : int
    name : str
        Tag included in log messages so multi-instance batches can tell
        connections apart (e.g. ``"env-0"``).
    """

    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 9000,
        name: str = "ucv",
    ) -> None:
        self.host = host
        self.port = port
        self.name = name
        self._client = None  # lazy unrealcv.Client

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def connect(self) -> None:
        import unrealcv  # local — keeps import cost out of module load
        _force_unrealcv_threads_daemon()
        self._client = unrealcv.Client((self.host, self.port))
        self._client.connect()
        if not self._client.isconnected():
            raise UCVError(
                f"[{self.name}] cannot connect to UnrealCV at "
                f"{self.host}:{self.port}"
            )
        log.info("[%s] connected to UnrealCV at %s:%d",
                 self.name, self.host, self.port)

    def disconnect(self) -> None:
        if self._client is not None:
            try:
                self._client.disconnect()
            except Exception:
                pass
            self._client = None

    def is_connected(self) -> bool:
        return self._client is not None and self._client.isconnected()

    def _ensure_connected(self) -> None:
        if self.is_connected():
            return
        log.warning("[%s] reconnecting to UnrealCV...", self.name)
        for attempt in range(1, _RECONNECT_MAX_ATTEMPTS + 1):
            time.sleep(_RECONNECT_DELAY_S)
            try:
                import unrealcv
                _force_unrealcv_threads_daemon()
                self._client = unrealcv.Client((self.host, self.port))
                self._client.connect()
                if self.is_connected():
                    log.info("[%s] reconnected (attempt %d)", self.name, attempt)
                    return
            except Exception as exc:
                log.debug("[%s] reconnect attempt %d failed: %s",
                          self.name, attempt, exc)
        raise UCVError(
            f"[{self.name}] failed to reconnect after "
            f"{_RECONNECT_MAX_ATTEMPTS} attempts"
        )

    # ------------------------------------------------------------------
    # Commands
    # ------------------------------------------------------------------

    def _request_with_timeout(self, cmd: str, timeout: float = 10.0):
        """Run self._client.request(cmd) with a real wall-clock timeout.

        The unrealcv library's ``request(timeout=N)`` parameter is silently
        ignored — the underlying ``recv_data_q.get()`` call has no timeout
        and blocks forever if UE never replies or drops the connection.
        We work around this by running the blocking call in a daemon thread
        and joining with our own deadline.  The abandoned thread dies when
        its socket is closed (by a subsequent disconnect or process exit).
        """
        result: list = [None]
        exc_holder: list = [None]

        def _run() -> None:
            try:
                result[0] = self._client.request(cmd)
            except Exception as exc:  # noqa: BLE001
                exc_holder[0] = exc

        t = threading.Thread(target=_run, daemon=True)
        t.start()
        t.join(timeout)
        if t.is_alive():
            raise UCVError(
                f"[{self.name}] command timed out after {timeout:.0f}s: {cmd!r}"
            )
        if exc_holder[0] is not None:
            raise UCVError(
                f"[{self.name}] command failed: {cmd!r} -> {exc_holder[0]}"
            ) from exc_holder[0]
        if result[0] is None:
            raise UCVError(
                f"[{self.name}] no response (disconnection) for: {cmd!r}"
            )
        return result[0]

    def send(self, cmd: str, timeout: float = 10.0) -> str:
        """Send a text command and return its string response.

        Auto-recovers once on transient socket failures.  Raises
        :class:`UCVError` if the command cannot be delivered.

        On retry we call :meth:`hard_reconnect` (not the cheap
        ``_ensure_connected``) because unrealcv's internal self-healing
        path can silently kill the receive thread while leaving
        ``isconnected()`` True.  Without a fresh client we'd send on a
        live socket that has no reader and hang forever.
        """
        self._ensure_connected()
        log.debug("[%s] >> %s", self.name, cmd)
        try:
            resp = self._request_with_timeout(cmd, timeout)
        except Exception as exc:
            log.warning("[%s] command failed (%s); hard_reconnect + retry",
                        self.name, exc)
            self.hard_reconnect()
            try:
                resp = self._request_with_timeout(cmd, timeout)
            except Exception as exc2:
                raise UCVError(
                    f"[{self.name}] command failed: {cmd!r} -> {exc2}"
                ) from exc2
        text = "" if resp is None else str(resp)
        if log.isEnabledFor(logging.DEBUG):
            preview = text if len(text) <= _RESPONSE_PREVIEW_LEN else (
                text[:_RESPONSE_PREVIEW_LEN] + "..."
            )
            log.debug("[%s] << %s", self.name, preview)
        return text

    def send_bytes(self, cmd: str, *, timeout: float = 10.0) -> bytes:
        """Send a command expected to return a binary payload (PNG, npy)."""
        self._ensure_connected()
        log.debug("[%s] >> %s (binary, timeout=%.0fs)", self.name, cmd, timeout)
        try:
            resp = self._request_with_timeout(cmd, timeout)
        except Exception as exc:
            log.warning("[%s] binary cmd failed (%s); hard_reconnect + retry",
                        self.name, exc)
            self.hard_reconnect()
            try:
                resp = self._request_with_timeout(cmd, timeout)
            except Exception as exc2:
                raise UCVError(
                    f"[{self.name}] binary command failed: {cmd!r} -> {exc2}"
                ) from exc2
        if isinstance(resp, str):
            # Some unrealcv versions return a path string for "png" instead
            # of inline bytes when the image is large.  Caller can handle
            # this case via vget_camera_png's fallback path.
            return resp.encode("latin-1")
        return resp or b""

    # ------------------------------------------------------------------
    # Convenience: typed wrappers used by the env
    # ------------------------------------------------------------------

    def vget_location(self, actor: str) -> tuple:
        """Return ``(x, y, z)`` floats in cm."""
        resp = self.send(f"vget /object/{actor}/location")
        parts = resp.strip().split()
        return tuple(float(p) for p in parts[:3])

    def vget_rotation(self, actor: str) -> tuple:
        """Return ``(pitch, yaw, roll)`` floats in degrees."""
        resp = self.send(f"vget /object/{actor}/rotation")
        parts = resp.strip().split()
        return tuple(float(p) for p in parts[:3])

    def vset_location(self, actor: str, x: float, y: float, z: float) -> None:
        self.send(f"vset /object/{actor}/location {x} {y} {z}")

    def vset_rotation(self, actor: str, pitch: float, yaw: float, roll: float) -> None:
        self.send(f"vset /object/{actor}/rotation {pitch} {yaw} {roll}")

    def vget_objects(self) -> list:
        """Return the list of all visible UE actor names."""
        resp = self.send("vget /objects")
        return [name for name in resp.strip().split() if name]

    def vget_bounds(self, actor: str):
        """Return world-space AABB ``(xmin, ymin, zmin, xmax, ymax, zmax)``
        in cm, or ``None`` if the engine did not return 6 parseable floats.

        Calls UnrealCV's ``vget /object/<name>/bounds``. The standard plugin
        returns 6 space-separated floats; some forks use commas. Both are
        accepted. ``None`` means "unknown" — callers should treat that as
        "no information" (do NOT assume zero size or empty box).
        """
        try:
            resp = self.send(f"vget /object/{actor}/bounds").strip()
        except Exception:
            return None
        if not resp or resp.lower().startswith("error"):
            return None
        # Accept both " " and "," separators.
        parts = resp.replace(",", " ").split()
        if len(parts) < 6:
            return None
        try:
            xmin, ymin, zmin, xmax, ymax, zmax = (float(p) for p in parts[:6])
        except ValueError:
            return None
        # Guard against degenerate boxes from buggy responses.
        if xmin > xmax or ymin > ymax or zmin > zmax:
            return None
        return (xmin, ymin, zmin, xmax, ymax, zmax)

    def vget_camera_png(self, camera_id: int = 1, mode: str = "lit") -> bytes:
        """Capture an RGB frame as PNG bytes from a humanoid first-person camera.

        ``camera_id`` defaults to **1** because that matches SimWorld's
        ``Humanoid`` class convention: index 0 is the player pawn camera,
        and the first spawned humanoid registers its FusionCamSensor at
        index 1 (see ``SimWorld/simworld/agent/humanoid.py:14-30``).
        Hitting index 0 for an agent we just spawned via UCV resolves to
        the editor's default pawn sensor, which has historically crashed
        UE on the first ``CaptureScene`` call in this build.

        Timing note: a 640x480 lit frame is a few hundred KB and
        round-trips in 100-300 ms on a healthy connection.
        """
        cmd = f"vget /camera/{camera_id}/{mode} png"
        t0 = time.time()
        try:
            payload = self.send_bytes(cmd, timeout=30)
        except UCVError:
            payload = b""
        elapsed = time.time() - t0
        if payload[:8] == b"\x89PNG\r\n\x1a\n":
            log.debug("[%s] camera %d %s: %d bytes in %.2fs",
                      self.name, camera_id, mode, len(payload), elapsed)
            return payload
        log.debug("[%s] camera %d %s: inline response was not PNG (%d bytes); "
                  "trying file-path fallback", self.name, camera_id, mode, len(payload))
        try:
            text_path = self.send(f"vget /camera/{camera_id}/{mode}")
            text_path = text_path.strip().strip('"')
            if text_path:
                with open(text_path, "rb") as f:
                    return f.read()
        except Exception as exc:
            raise UCVError(f"could not retrieve camera frame: {exc}") from exc
        return b""


    def spawn_bp_asset(
        self,
        blueprint_path: str,
        name: str,
        location: Optional[tuple] = None,
        rotation: Optional[tuple] = None,
        auto_repair_collision: bool = True,
        collision_mode: int = None,
    ) -> None:
        """Spawn a blueprint actor, optionally at a specific transform.

        Spawn-heavy commands can drop the underlying TCP socket.  We
        therefore (a) ignore any error raised by the request, (b)
        sleep so UE finishes loading the BP, and (c) **hard-reset**
        the unrealcv Client so we don't carry a stale receive-queue
        into subsequent requests.

        Args:
            auto_repair_collision: Legacy bool flag (0 or 1).
            collision_mode: If set, overrides auto_repair_collision.
                0 = no collision repair
                1 = full repair (XY collision + ground trace)
                2 = XY-only repair (no ground trace, keeps Z position)
        """
        if location is not None:
            x, y, z = location
            pitch, yaw, roll = rotation if rotation is not None else (0.0, 0.0, 0.0)
            if collision_mode is not None:
                flag = collision_mode
            else:
                flag = 1 if auto_repair_collision else 0
            cmd = (
                f"vset /objects/spawn_bp_asset {blueprint_path} {name} "
                f"{x} {y} {z} {pitch} {yaw} {roll} {flag}"
            )
        else:
            cmd = f"vset /objects/spawn_bp_asset {blueprint_path} {name}"

        # Synchronous spawn with bounded timeout — we MUST see UE's response
        # to detect "error" (e.g. duplicate name, BP not found, world rejection).
        # The fire-and-forget path silently swallowed all errors.
        log.info("[%s] spawn dispatching (sync, 60s timeout): %s",
                 self.name, cmd)
        self._ensure_connected()
        resp = None
        try:
            resp = self._request_with_timeout(cmd, timeout=60.0)
        except UCVError as exc:
            # Likely UE dropped socket during skinned-mesh compile.
            # Reconnect and verify post-hoc by checking object list.
            log.warning("[%s] spawn request raised %s — reconnecting to verify",
                        self.name, exc)
            try:
                self._client.disconnect()
            except Exception:
                pass
            time.sleep(5.0)
            for attempt in range(60):
                try:
                    self._client.connect()
                    if self.is_connected():
                        log.info("[%s] reconnected after spawn drop (attempt %d, ~%ds)",
                                 self.name, attempt + 1, (attempt + 1) * 5 + 5)
                        break
                except Exception:
                    pass
                time.sleep(5.0)
            else:
                raise UCVError(f"[{self.name}] spawn reconnect failed after 300s")
        else:
            resp_text = "" if resp is None else str(resp).strip()
            log.info("[%s] spawn UE response: %r", self.name, resp_text[:200])
            if resp_text.lower().startswith("error"):
                raise UCVError(
                    f"[{self.name}] spawn rejected by UE: {resp_text!r} "
                    f"(cmd={cmd!r})"
                )
        log.info("[%s] spawned BP %s as %s at %s",
                 self.name, blueprint_path, name, location)

    def spawn_static_mesh(
        self,
        mesh_path: str,
        name: str,
        location: tuple,
        rotation: tuple = (0.0, 0.0, 0.0),
        auto_repair_collision: bool = True,
    ) -> None:
        """Spawn a StaticMeshActor pointing at a StaticMesh asset.

        Requires the UnrealCV plugin with the ``spawn_static_mesh``
        command registered (added alongside the Linux listener fix
        in mid-April 2026).
        """
        x, y, z = location
        pitch, yaw, roll = rotation
        flag = 1 if auto_repair_collision else 0
        cmd = (
            f"vset /objects/spawn_static_mesh {mesh_path} {name} "
            f"{x} {y} {z} {pitch} {yaw} {roll} {flag}"
        )
        try:
            self.send(cmd)
        except UCVError as exc:
            log.debug("[%s] spawn_static_mesh raised %s", self.name, exc)
        time.sleep(1.0)
        self.hard_reconnect()
        log.info("[%s] spawned SM %s as %s at %s",
                 self.name, mesh_path, name, location)

    def destroy_actor(self, name: str) -> str:
        """Destroy an actor by name.  No-op if it doesn't exist."""
        try:
            return self.send(f"vset /object/{name}/destroy")
        except UCVError as exc:
            log.debug("[%s] destroy %s: %s", self.name, name, exc)
            return ""

    def hard_reconnect(self) -> None:
        """Drop the unrealcv Client and create a fresh one.

        Use after operations that may have left the receive queue in a
        stale state (spawn, PIE start, etc.).  Distinct from
        ``_ensure_connected`` which only reconnects if the socket is
        already known to be down.

        Important: we do **NOT** call ``old_client.disconnect()`` here.
        The unrealcv library has a bug where the receive_loop_queue
        thread, after seeing EOF on its socket, calls
        ``self.disconnect()`` from inside its own thread, which then
        does ``self.t.join()`` on itself and deadlocks with
        ``RuntimeError: cannot join current thread``.  If we *also*
        call disconnect from the main thread we race that path and
        leave the receive thread holding a stale socket forever — any
        subsequent ``request()`` we make on the new client may get a
        response that's intercepted by the dead old thread, hanging
        the new request indefinitely.

        However, just dropping our reference to the old Client (the
        previous behaviour) leaks the underlying TCP socket: the old
        receive thread is still blocked on ``socket.recv`` so the
        Client object cannot be garbage-collected, and the OS-level
        socket stays in ESTABLISHED. After a long run this piles up to
        hundreds of connections on port 9001, observed empirically
        on the 2026-04-24 prod30 run (~200 ESTABLISHED).

        Solution: explicitly ``shutdown`` + ``close`` the OLD raw
        socket from the main thread BEFORE we drop the reference.
        Closing the socket unblocks the receive thread (recv returns
        b'' and the thread exits naturally), so no join is needed and
        the OS releases the connection immediately.
        """
        # Shut down the old socket so its receive thread exits and the
        # OS releases the TCP connection. ``sock`` is the raw
        # ``socket.socket`` exposed by ``unrealcv.Client``.
        old = self._client
        if old is not None:
            try:
                sock = getattr(old, "sock", None)
                if sock is not None:
                    import socket as _socket
                    try:
                        sock.shutdown(_socket.SHUT_RDWR)
                    except Exception:
                        pass
                    try:
                        sock.close()
                    except Exception:
                        pass
            except Exception as exc:
                log.debug("[%s] hard_reconnect: socket close failed: %s",
                          self.name, exc)
        self._client = None
        time.sleep(0.5)
        import unrealcv
        _force_unrealcv_threads_daemon()
        for attempt in range(1, _RECONNECT_MAX_ATTEMPTS + 1):
            try:
                self._client = unrealcv.Client((self.host, self.port))
                self._client.connect()
                if self.is_connected():
                    log.info("[%s] hard-reconnected on attempt %d",
                             self.name, attempt)
                    return
            except Exception as exc:
                log.debug("[%s] hard-reconnect attempt %d failed: %s",
                          self.name, attempt, exc)
            time.sleep(_RECONNECT_DELAY_S)
        raise UCVError(f"[{self.name}] hard_reconnect failed")

    def vbp(self, actor: str, command: str) -> str:
        """Send a Blueprint function call to a named actor."""
        return self.send(f"vbp {actor} {command}")

    # ------------------------------------------------------------------
    # Navigation commands (requires NavigationHandler in UnrealCV plugin)
    # ------------------------------------------------------------------

    def nav_build(self, min_x: float, min_y: float, min_z: float,
                  max_x: float, max_y: float, max_z: float) -> str:
        """Build navmesh over a bounding box."""
        return self.send(
            f"vset /nav/build {min_x} {min_y} {min_z} {max_x} {max_y} {max_z}"
        )

    def nav_build_from_actor(self, actor: str, padding: float = 0.0) -> str:
        """Build navmesh from an actor's bounds (e.g. floor mesh)."""
        if padding > 0:
            return self.send(f"vset /nav/build_from_actor {actor} {padding}")
        return self.send(f"vset /nav/build_from_actor {actor}")

    def nav_status(self) -> str:
        """Check if navmesh is built and ready (returns JSON string)."""
        return self.send("vget /nav/status")

    def nav_reachable(self, x1: float, y1: float, z1: float,
                      x2: float, y2: float, z2: float) -> bool:
        """Lightweight reachability test (no full path computed)."""
        resp = self.send(
            f"vget /nav/reachable {x1} {y1} {z1} {x2} {y2} {z2}"
        )
        return resp.strip() == "true"

    def nav_path(self, x1: float, y1: float, z1: float,
                 x2: float, y2: float, z2: float) -> str:
        """Query navmesh path. Returns 'length|x,y,z|...' or '-1'."""
        return self.send(
            f"vget /nav/path {x1} {y1} {z1} {x2} {y2} {z2}"
        )
