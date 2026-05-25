"""Co-evolution loop v4: PIE cycle architecture.

Each epoch:
  1. Exit PIE → editor mode
  2. Coding agent designs scene (spawn/destroy via UCV in editor)
  3. Build NavMesh (editor mode)
  4. Start PIE
  5. Run nav agent episodes
  6. Collect results + update memory
"""
from __future__ import annotations

import json
import logging
import math
import os
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from .checkpoint import CheckpointManager
from .coding_agent import CodingAgent
from .coding_memory import CodingAgentMemory
from .config import CoEvolveConfig
from .context_manager import CoEvolveContextManager
from .difficulty import compute_coding_reward, measure_blocked_ratio, measure_float_penalty, score_task_difficulty
from .scene_manager import SceneManager, SceneSpec
from .teacher import DifficultyProposal, make_teacher

log = logging.getLogger(__name__)


class CoEvolutionRunner:
    def __init__(self, config: CoEvolveConfig, resume_dir: str = None):
        self.config = config
        self.gen_results: List[Dict[str, Any]] = []
        self._start_epoch = 0
        self._scene_obj_coords: List[tuple] = []
        # Latest BuildReport from scene_mgr.build_scene(); fed back to the
        # coding agent next epoch as real environment feedback.
        self._prev_build_report = None
        # Persisted scene graph carried across PIE restarts (full pose +
        # asset_key per object) so the next epoch can re-spawn into the
        # fresh PIE world before any in-epoch editing happens.
        self._current_scene_objects: List[Dict[str, Any]] = []
        # asset_key map kept in sync with the live scene; needed to
        # snapshot owned objects (UCV doesn't tell us the asset key).
        self._asset_key_map: Dict[str, str] = {}
        # Baseline NavMesh blocked-ratio of the persistent map (no LLM
        # spawns). Measured ONCE on the first epoch before build_scene so
        # that the static baseline (buildings/walls/water of the original
        # ~32k-actor map) is not double-counted as task difficulty.
        # Per-epoch difficulty.scene_score uses
        #   effective = max(0.0, current_blocked - baseline)
        # so only LLM-added blockage counts.
        self._baseline_blocked_ratio: Optional[float] = None

        if resume_dir:
            self.output_dir = Path(resume_dir)
        else:
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            self.output_dir = Path(config.output_dir) / f"coevolve_{ts}"
        self.output_dir.mkdir(parents=True, exist_ok=True)

        self.ckpt = CheckpointManager(self.output_dir)
        self.ctx = CoEvolveContextManager()

        if not resume_dir:
            (self.output_dir / "config.json").write_text(
                json.dumps(vars(config), indent=2, default=str), encoding="utf-8"
            )

    def run(self) -> List[Dict[str, Any]]:
        return self._run_live()

    # ------------------------------------------------------------------
    # PIE helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _exit_pie(ucv) -> None:
        """Exit PIE via UCV only (PIE world lifecycle control)."""
        try:
            resp = ucv.send("vexec /action/exit_pie", timeout=30)
            log.info("exit_pie: %s", resp)
        except Exception as exc:
            log.warning("exit_pie via UCV failed (may already be in editor): %s", exc)
        time.sleep(3)

    @staticmethod
    def _start_pie(mcp_port: int) -> None:
        """Start PIE via UnrealCV vexec /action/start_pie.

        Earlier we tried LevelEditorSubsystem.editor_play_simulate() through
        MCP, but on this UE 5.3 build that python call is a no-op: the
        function returns successfully but UE never actually enters PIE
        (no LogPlayLevel output, is_in_play_in_editor() stays False).
        UnrealCV's /action/start_pie command is what historically works.
        """
        from gym_env.mcp_client import MCPClient, _extract_python_logs

        log.info("start_pie: dispatching editor_play_simulate() via MCP port %d...",
                 mcp_port)
        dispatch_script = (
            "import unreal\n"
            "le = unreal.get_editor_subsystem(unreal.LevelEditorSubsystem)\n"
            "if le is None:\n"
            "    print('NO_LEVEL_EDITOR_SUBSYSTEM')\n"
            "else:\n"
            "    if le.is_in_play_in_editor():\n"
            "        print('PIE_ALREADY_ACTIVE')\n"
            "    else:\n"
            "        try:\n"
            "            le.editor_play_simulate()\n"
            "            print('PIE_START_REQUESTED')\n"
            "        except Exception as e:\n"
            "            print('PIE_START_FAILED:' + repr(e))\n"
        )
        # Retry dispatch up to 5 times — UE may be busy with shader compile or
        # world teardown when we first try; the underlying MCP TCP connection
        # gets closed with zero bytes in that case.
        dispatch_ok = False
        for d_attempt in range(1, 6):
            try:
                dispatch_mcp = MCPClient(port=mcp_port, timeout=120, name="loop-pie-dispatch")
                resp = dispatch_mcp.execute_python(dispatch_script, timeout=120)
                logs = "\n".join(_extract_python_logs(resp))
                log.info("start_pie: dispatch attempt %d logs: %s", d_attempt, logs[:200])
                if "PIE_START_REQUESTED" in logs or "PIE_ALREADY_ACTIVE" in logs:
                    dispatch_ok = True
                    break
                # If logs empty (soft success on busy UE), assume queued and probe
                if not logs.strip():
                    log.info("start_pie: dispatch attempt %d returned empty logs; will verify via probe", d_attempt)
                    dispatch_ok = True
                    break
            except Exception as exc:
                log.warning("start_pie: dispatch attempt %d failed: %s", d_attempt, exc)
                time.sleep(5)
        if not dispatch_ok:
            log.warning("start_pie: all 5 dispatch attempts failed; will still poll")

        # Poll PIE readiness via MCP.
        # IMPORTANT: ``LevelEditorSubsystem.is_in_play_in_editor()`` is a
        # UnrealScript function. UE 5.3 hard-asserts (and KILLS the editor
        # process) if any UnrealScript is called while the engine is
        # ``IsRoutingPostLoad`` — i.e. while it's still PostLoading the
        # assets that PIE just started streaming in (vehicles, mannequins,
        # niagara, ...). On the HwaseongHaenggung + CitySampleVehicles map
        # PostLoad takes 15-25s after ``editor_play_simulate()`` returns.
        # We therefore wait a generous ``_PIE_POSTLOAD_GRACE_S`` before the
        # very first probe so the first ``is_in_play_in_editor()`` call
        # never lands inside PostLoad. Subsequent probes happen at the
        # normal 5 s cadence.
        probe_script = (
            "import unreal\n"
            "le = unreal.get_editor_subsystem(unreal.LevelEditorSubsystem)\n"
            "ues = unreal.get_editor_subsystem(unreal.UnrealEditorSubsystem)\n"
            "is_pie = bool(le and le.is_in_play_in_editor())\n"
            "gw = None\n"
            "try:\n"
            "    gw = ues.get_game_world() if hasattr(ues, 'get_game_world') else None\n"
            "except Exception:\n"
            "    gw = None\n"
            "print('PIE_READY:' + ('1' if (is_pie and gw is not None) else '0'))\n"
        )
        _PIE_POSTLOAD_GRACE_S = 30.0
        deadline = time.time() + 300.0
        ready = False
        attempt = 0
        log.info("start_pie: waiting %.0fs for PostLoad before first probe", _PIE_POSTLOAD_GRACE_S)
        time.sleep(_PIE_POSTLOAD_GRACE_S)
        while time.time() < deadline:
            attempt += 1
            if attempt > 1:
                time.sleep(5)
            try:
                probe_mcp = MCPClient(port=mcp_port, timeout=15, name="loop-pie-probe")
                resp = probe_mcp.execute_python(probe_script, timeout=15)
<<<<<<< Updated upstream
                logs_list = _extract_python_logs(resp)
                logs = "\n".join(logs_list)
=======
                logs = "\n".join(_extract_python_logs(resp))
>>>>>>> Stashed changes
                if "PIE_READY:1" in logs:
                    log.info("start_pie: PIE world ready after %.1fs (attempt %d)",
                             5.0 * attempt, attempt)
                    ready = True
                    break
<<<<<<< Updated upstream
                # MCP log-capture race: success=true with empty python_logs
                # means the script ran but stdout was lost. After grace period
                # this is overwhelmingly the "PIE actually started" case.
                # Trust UCV liveness as the secondary signal before giving up.
                result = resp.get("result") if isinstance(resp, dict) else None
                if (isinstance(result, dict) and result.get("success")
                        and not logs_list and attempt >= 2):
                    log.info("start_pie: empty logs but execute_python succeeded "
                             "(attempt %d) — assuming PIE ready (MCP log-capture race)",
                             attempt)
                    ready = True
                    break
                else:
                    log.info("start_pie: PIE not ready yet (attempt %d, logs=%d)",
                             attempt, len(logs_list))
=======
                else:
                    log.info("start_pie: PIE not ready yet (attempt %d)", attempt)
>>>>>>> Stashed changes
            except Exception as exc:
                log.info("start_pie: probe %d failed (%s); will retry", attempt, exc)
            # Every 6 attempts (~30s) re-dispatch in case earlier dispatch was lost.
            if attempt % 6 == 0 and not ready:
                try:
                    log.info("start_pie: re-dispatching editor_play_simulate() (poll attempt %d)", attempt)
                    redisp_mcp = MCPClient(port=mcp_port, timeout=120, name="loop-pie-redispatch")
                    redisp_mcp.execute_python(dispatch_script, timeout=120)
                except Exception as exc:
                    log.info("start_pie: re-dispatch failed (%s)", exc)
        if not ready:
            log.warning("start_pie: PIE never reported ready within 300s; "
                        "proceeding anyway and hoping UCV recovers")
        time.sleep(3)
        log.info("start_pie: done waiting")

    @staticmethod
    def _ucv_port_candidates(preferred_port: int) -> List[int]:
        """Build a de-duplicated UnrealCV port probe order.

        Order policy:
        1) caller-provided preferred port
        2) 9002 (project default)
        3) 9000 (legacy fallback)
        """
        candidates: List[int] = []
        for p in [preferred_port, 9002, 9000]:
            if p not in candidates:
                candidates.append(p)
        return candidates

    @staticmethod
    def _connect_ucv_with_fallback(
        host: str,
        preferred_port: int,
        name: str,
        attempts_per_port: int,
        sleep_s: float = 2.0,
    ):
        """Connect UnrealCV by probing preferred port, then 9002/9000."""
        from gym_env.ucv_client import UCVClient

        ports = CoEvolutionRunner._ucv_port_candidates(preferred_port)
        last_exc: Optional[Exception] = None
        for port in ports:
            for attempt in range(1, attempts_per_port + 1):
                try:
                    ucv = UCVClient(host=host, port=port, name=name)
                    ucv.connect()
                    log.info("UCV connected at %s:%d (attempt %d/%d)",
                             host, port, attempt, attempts_per_port)
                    return ucv, port
                except Exception as exc:
                    last_exc = exc
                    time.sleep(sleep_s)
        if last_exc is not None:
            raise last_exc
        raise RuntimeError("UCV connect failed: no candidate ports to probe")

    @staticmethod
    def _ensure_navmesh_volume(ucv, mcp_port: int) -> None:
        """Spawn NavMeshBoundsVolume in editor mode via MCP (one-time)."""
        from gym_env.mcp_client import MCPClient
        mcp = MCPClient(port=mcp_port, timeout=20)
        volume_script = '''
import unreal
loc = unreal.Vector(0, 0, 0)
rot = unreal.Rotator(0, 0, 0)
# Check if already exists
for actor in unreal.EditorLevelLibrary.get_all_level_actors():
    if isinstance(actor, unreal.NavMeshBoundsVolume):
        print('VOLUME_EXISTS')
        break
else:
    vol = unreal.EditorLevelLibrary.spawn_actor_from_class(
        unreal.NavMeshBoundsVolume, loc, rot)
    if vol:
        vol.set_actor_scale3d(unreal.Vector(100, 100, 10))
        print('VOLUME_OK')
    else:
        print('VOLUME_FAILED')
'''
        try:
            resp = mcp.execute_python(volume_script, timeout=15)
            logs = resp.get("result", {}).get("python_logs", [])
            for l in logs:
                if l.strip():
                    log.info("[NavVolume] %s", l.strip())
        except Exception as exc:
            log.warning("NavMeshBoundsVolume spawn failed: %s", exc)

    @staticmethod
    def _build_navmesh_editor(ucv, mcp_port: int) -> None:
        """Build NavMesh in editor mode via MCP."""
        from gym_env.mcp_client import MCPClient
        mcp = MCPClient(port=mcp_port, timeout=20)
        build_script = '''
import unreal
world = unreal.get_editor_subsystem(unreal.UnrealEditorSubsystem).get_editor_world()
nav_sys = unreal.NavigationSystemV1.get_navigation_system(world)
if nav_sys:
    # UE5 Python does not expose Build(); use console command instead.
    unreal.SystemLibrary.execute_console_command(world, 'RebuildNavigation')
    print('NAVMESH_BUILT')
else:
    print('NO_NAV_SYS')
'''
        try:
            resp = mcp.execute_python(build_script, timeout=15)
            logs = resp.get("result", {}).get("python_logs", [])
            for l in logs:
                if l.strip():
                    log.info("[NavMesh] %s", l.strip())
        except Exception as exc:
            log.warning("NavMesh build failed: %s", exc)

    # ------------------------------------------------------------------
    # Main loop
    # ------------------------------------------------------------------

    def _run_live(self) -> List[Dict[str, Any]]:
        import sys
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

        from gym_env.episode_builder import sample_pointnav_episode_navmesh, sample_pointnav_episode
        from gym_env.llm import make_llm
        from gym_env.logger import EpisodeLogger
        from gym_env.memory import build_memory
        from gym_env.mcp_client import MCPClient
        from gym_env.runner import run_episode
        from gym_env.batch_runner import run_wave
        from gym_env.simworld_nav_env import SimWorldNavEnv
        from nav_task.navmesh_interface import NavmeshNavigationInterface

        cfg = self.config

        # Build LLM callables
        coding_llm = self._make_llm_call(cfg.coding_model_id, cfg.coding_base_url, cfg.coding_api_key)
        nav_reflect_llm = self._make_llm_call(cfg.nav_model_id, cfg.nav_base_url, cfg.nav_api_key)

        # Coding agent + memory
        coding_mem = CodingAgentMemory(
            path=str(self.output_dir / "coding_memory.json"),
            reflect_every_n=3, llm_call=coding_llm,
        )
        coding_agent = CodingAgent(llm_call=coding_llm, coding_memory=coding_mem)

        # Curriculum teacher — owns difficulty selection. Persists across
        # --resume via teacher_state.json beside the checkpoint.
        teacher = make_teacher(
            cfg.teacher,
            d_min=cfg.teacher_d_min,
            d_max=cfg.teacher_d_max,
            tol=cfg.difficulty_tolerance,
            p_random=cfg.teacher_p_random,
            seed=cfg.seed,
        )
        teacher_state_path = self.output_dir / "teacher_state.json"
        teacher.load(teacher_state_path)
        log.info("Curriculum teacher: %s (tol=%.2f, p_random=%.2f, max_regen=%d)",
                 teacher.name, cfg.difficulty_tolerance, cfg.teacher_p_random,
                 cfg.teacher_max_regen)
        prev_blocked_ratio: float = 0.0

        # Nav agent LLM + memory
        nav_llm = make_llm(cfg.nav_model, model=cfg.nav_model_id,
                           base_url=cfg.nav_base_url, api_key=cfg.nav_api_key)
        # Warmup memory: load distilled L3 skills (from prior training runs)
        # if a warmup file is present. These are pinned in the system prompt
        # and the agent continues to learn additional strategies on top.
        # Search order: $NAV_MEMORY_WARMUP env var, then ./l3_skills.json
        # at the workspace root (one level above output_dir's parent run dir).
        warmup_skills: list = []
        warmup_candidates = []
        env_warmup = os.environ.get("NAV_MEMORY_WARMUP")
        if env_warmup:
            warmup_candidates.append(Path(env_warmup))
        # Default: workspace root (the parent of "runs/")
        warmup_candidates.append(Path.cwd() / "l3_skills.json")
        warmup_candidates.append(Path(__file__).resolve().parent.parent.parent / "l3_skills.json")
        for cand in warmup_candidates:
            try:
                if cand.is_file():
                    data = json.loads(cand.read_text(encoding="utf-8"))
                    skills = data.get("skills") or data.get("strategies") or []
                    warmup_skills = [str(s).strip() for s in skills if isinstance(s, str) and s.strip()]
                    if warmup_skills:
                        log.info("Nav memory warmup: loaded %d L3 skills from %s",
                                 len(warmup_skills), cand)
                        break
            except Exception as exc:
                log.warning("Nav memory warmup: failed to load %s: %s", cand, exc)
        nav_memory = build_memory(cfg.nav_memory, agent_id="coevolve_nav",
                                  config={"path": str(self.output_dir / "strategy_memory.json"),
                                          "warmup": warmup_skills})
        if hasattr(nav_memory, '_llm_call') and nav_memory._llm_call is None:
            nav_memory._llm_call = nav_reflect_llm

        # Startup probe: ASK MCP whether UE is currently in PIE.
        # Old logic used "UCV reachable ⇒ PIE", which (a) wastes ~30 s on
        # internal UCV reconnect storms whenever exit_pie is called, and
        # (b) misuses UCV as a state oracle while the contract says
        # "PIE-state control goes through MCP". Use editor_subsystem.is_in_pie()
        # via MCP and only call exit_pie when actually needed.
        boot_mcp = MCPClient(host=cfg.mcp_host, port=cfg.mcp_port,
                             name="coevolve-boot-mcp", timeout=20)
        in_pie = False
        try:
            probe_script = (
                "import unreal\n"
                "is_pie = False\n"
                "try:\n"
                "    le = unreal.get_editor_subsystem(unreal.LevelEditorSubsystem)\n"
                "    is_pie = bool(le and le.is_in_play_in_editor())\n"
                "except Exception as e:\n"
                "    print('PROBE_ERR:' + repr(e))\n"
                "print('IS_PIE:' + ('1' if is_pie else '0'))\n"
            )
            resp = boot_mcp.execute_python(probe_script, timeout=15)
            logs = resp.get("result", {}).get("python_logs", [])
            for line in logs:
                if "IS_PIE:1" in line:
                    in_pie = True
                    break
            log.info("Startup probe via MCP: in_pie=%s", in_pie)
        except Exception as exc:
            log.warning("Startup probe via MCP failed (%s); assuming editor mode", exc)

        if in_pie:
            log.info("Startup probe: UE is in PIE, exiting via UCV")
            try:
                boot_ucv, chosen_port = self._connect_ucv_with_fallback(
                    host=cfg.ucv_host,
                    preferred_port=cfg.ucv_port,
                    name="coevolve-boot-ucv",
                    attempts_per_port=2,
                    sleep_s=1.0,
                )
                cfg.ucv_port = chosen_port
                self._exit_pie(boot_ucv)
                try:
                    boot_ucv.disconnect()
                except Exception:
                    pass
            except Exception as exc:
                log.warning("UCV exit_pie at boot failed (%s); continuing", exc)
        else:
            log.info("Startup probe: editor mode, skipping exit_pie")

        # One-time editor setup: no UCV assumption here.
        self._ensure_navmesh_volume(None, cfg.mcp_port)

        scene_mgr = SceneManager(None)
        mcp = MCPClient(host=cfg.mcp_host, port=cfg.mcp_port, name="coevolve-mcp")

        # Resume
        ckpt_state = self.ckpt.load()
        if ckpt_state:
            self._start_epoch = ckpt_state["last_epoch"] + 1
            self.gen_results = self.ckpt.load_gen_results()
            for r in self.gen_results:
                self.ctx.add_generation(r)
            log.info("Resuming from epoch %d", self._start_epoch)
            coding_agent._current_scene_id = ckpt_state.get("current_scene_id", "scene_000")
            coding_agent._current_difficulty = ckpt_state.get("difficulty", 0)
            # NOTE: scene-state resume is intentionally NOT supported right
            # now. Proper resume requires reloading the saved .umap into
            # the editor (via MCP python) so the static scene + coding
            # agent's spawned objects all come back together. Reusing the
            # JSON-only scene graph would only cover the spawned objects
            # and would miss any modifications to the static map. Until
            # the .umap-reload path is implemented, we warn and start the
            # next epoch from an empty in-memory scene state.
            log.warning(
                "Resume: scene-state restore is disabled. The next epoch "
                "will treat the live editor scene as the new baseline. "
                "If you need exact-state resume, reload "
                "runs/.../maps/%s.umap into the editor manually first.",
                coding_agent._current_scene_id,
            )

        # UCV is only valid in PIE mode. Keep a handle across epochs so we can
        # exit PIE via UCV at the beginning of the next epoch.
        ucv = None

        # ── Main loop ──
        for epoch in range(self._start_epoch, cfg.generations):
            log.info("=" * 60)
            log.info("EPOCH %d / %d", epoch, cfg.generations - 1)
            log.info("=" * 60)

            # ── Phase 1: If currently in PIE, exit via UCV → Editor mode ──
            if ucv is not None:
                self._exit_pie(ucv)
                try:
                    ucv.disconnect()
                except Exception:
                    pass
                ucv = None
                time.sleep(2)
            else:
                log.info("Phase 1: assuming editor mode (no active UCV session)")

            # ── Phase 2: Teacher proposes a difficulty band (no PIE needed) ──
            proposal: DifficultyProposal = teacher.propose()
            log.info("Teacher proposal: target=%.2f band=[%.2f, %.2f] (%s)",
                     proposal.target, proposal.band_lo, proposal.band_hi,
                     proposal.rationale)
            nav_ctx = self.ctx.get_nav_context_for_coding_agent(nav_memory)
            # NOTE: coding_agent.design() is now called AFTER PIE start so
            # the LLM can iteratively edit the scene with real BuildReport
            # feedback in the same epoch (multi-round editing). See the
            # loop further below.

            # ── Phase 3: Build NavMesh in editor mode ──
            self._build_navmesh_editor(None, cfg.mcp_port)
            time.sleep(2)

            # ── Phase 4: Start PIE ──
            self._start_pie(cfg.mcp_port)
            time.sleep(3)

            # Connect UCV only after PIE start (new game world)
            try:
                ucv, chosen_port = self._connect_ucv_with_fallback(
                    host=cfg.ucv_host,
                    preferred_port=cfg.ucv_port,
                    name="coevolve-ucv",
                    attempts_per_port=20,
                    sleep_s=2.0,
                )
                if chosen_port != cfg.ucv_port:
                    log.warning("UCV port fallback in effect: %d -> %d", cfg.ucv_port, chosen_port)
                    cfg.ucv_port = chosen_port
            except Exception:
                log.error("UCV connect failed after PIE start")
                continue

            # Scene updates are applied only in PIE mode via UCV.
            scene_mgr.ucv = ucv
            # Fresh PIE world ⇒ any actor names from the previous PIE are
            # dead references. Clear Python-side tracking and force a new
            # baseline snapshot on the next build_scene().
            scene_mgr.on_new_pie()

            # ── One-time baseline NavMesh blocked-ratio anchor ──
            # Measure the persistent map's intrinsic blocked area BEFORE
            # any LLM spawn so the static baseline doesn't inflate
            # per-task scene_score. NavMesh must be functional; if the
            # measurement fails we leave baseline at 0 (no subtraction).
            if self._baseline_blocked_ratio is None:
                try:
                    nav_probe = NavmeshNavigationInterface(ucv)
                    test_pts = nav_probe.get_navigable_positions(count=10)
                    if len(set(int(p.x) for p in test_pts)) > 1:
                        self._baseline_blocked_ratio = measure_blocked_ratio(
                            ucv, seed=cfg.seed
                        )
                        log.info(
                            "Baseline blocked_ratio anchor: %.3f "
                            "(will be subtracted from per-epoch measurements)",
                            self._baseline_blocked_ratio,
                        )
                    else:
                        log.warning(
                            "NavMesh not ready at baseline probe; "
                            "baseline blocked_ratio defaults to 0.0"
                        )
                        self._baseline_blocked_ratio = 0.0
                except Exception as exc:
                    log.warning(
                        "Baseline blocked_ratio probe failed (%s); defaulting to 0.0",
                        exc,
                    )
                    self._baseline_blocked_ratio = 0.0

            # Create env and pre-spawn humanoid immediately after PIE start,
            # before spawning scene clutter. This is more stable on heavy maps.
<<<<<<< Updated upstream
            # Use a unique agent name per epoch so a leftover actor from a
            # previous (failed) PIE session can never collide with the new
            # spawn (UE rejects duplicate names with 'object exsit').
            warmup_agent_name = f"CoEvolveAgent_E{epoch:03d}_{int(time.time())}"
            env = SimWorldNavEnv(
                ucv_client=ucv, mcp_client=mcp,
                agent_name=warmup_agent_name,
=======
            env = SimWorldNavEnv(
                ucv_client=ucv, mcp_client=mcp,
                agent_name="CoEvolveAgent_0",
>>>>>>> Stashed changes
                capture_rgb=cfg.capture_rgb,
                spawn_on_reset=False, ensure_pie=False,
            )
            spawn_ok = False
            for attempt in range(1, 11):
                try:
                    env._spawn_agent()
                except Exception as exc:
                    log.warning("Agent _spawn_agent attempt %d/10 raised: %s",
                                attempt, exc)
<<<<<<< Updated upstream
                    # If UE rejected because actor with same name already
                    # exists in the world (extremely unlikely now that the
                    # name is unique per-epoch, but keep as defensive net),
                    # destroy it and retry immediately on the next attempt.
                    msg = str(exc).lower()
                    if "object exsit" in msg or "object exist" in msg or "already exist" in msg:
                        try:
                            log.info("Spawn rejected (object exists); "
                                     "destroying stale %s and retrying", warmup_agent_name)
                            ucv.destroy_actor(warmup_agent_name)
                            time.sleep(2)
                        except Exception as de:
                            log.warning("destroy_actor cleanup failed: %s", de)
                    else:
                        time.sleep(5)
                    continue
                # Verify the actor actually exists in the PIE world.
                try:
                    loc = ucv.vget_location(warmup_agent_name)
=======
                    time.sleep(5)
                    continue
                # Verify the actor actually exists in the PIE world.
                try:
                    loc = ucv.vget_location("CoEvolveAgent_0")
>>>>>>> Stashed changes
                    if loc and len(loc) == 3 and all(isinstance(c, float) for c in loc):
                        log.info("Agent pre-spawned at %s for epoch %d (attempt %d)",
                                 loc, epoch, attempt)
                        spawn_ok = True
                        break
                except Exception as exc:
                    log.warning("Agent spawn verify attempt %d/10 failed: %s",
                                attempt, exc)
                    # On 1st & 5th failure dump current actor list to diagnose
                    if attempt in (1, 5):
                        try:
                            objs = ucv.send("vget /objects", timeout=10)
                            log.warning("Agent verify diagnostic: vget /objects (first 800 chars) = %s",
                                        str(objs)[:800])
                        except Exception as e2:
                            log.warning("Agent verify diagnostic failed: %s", e2)
                # Wait longer between retries — pedestrian BP compile is slow
                time.sleep(10)
            if spawn_ok:
                env._spawned = True
            else:
                log.error("Agent failed to spawn/verify after 10 attempts; "
                          "skipping epoch %d", epoch)
                try:
                    PieCycle._exit_pie(ucv)
                except Exception:
                    pass
                continue

            # ── Phase 5: Multi-round LLM scene editing with real feedback ──
            # NOTE: PIE restart does NOT wipe the editor scene in our setup,
            # so the coding agent's prior-epoch edits are still live. We do
            # NOT respawn anything from the persisted JSON — that JSON is
            # only the LLM-facing record of "what I built last time".
            MAX_EDIT_ROUNDS = 3
            spec: Optional[SceneSpec] = None
            last_report = self._prev_build_report  # carry-over from prior epoch
            for edit_round in range(MAX_EDIT_ROUNDS):
                round_spec = coding_agent.design(
                    performance_history=nav_ctx["performance_history"],
                    strategies=nav_ctx["strategies"],
                    failure_patterns=nav_ctx["failure_summary"],
                    current_scene_objects=scene_mgr.get_scene_objects(),
                    rolling_summary=nav_ctx.get("rolling_summary", "(no data yet)"),
                    current_scene_streak=nav_ctx.get("current_scene_streak", 0),
                    target_difficulty=proposal.target,
                    difficulty_band=(proposal.band_lo, proposal.band_hi),
                    prev_blocked_ratio=prev_blocked_ratio,
                    max_band_retries=cfg.teacher_max_regen,
                    prev_build_report=last_report,
                    edit_round=edit_round,
                    max_edit_rounds=MAX_EDIT_ROUNDS,
                )
                # Apply standard clamps to every round's spec.
                # FORCE max_steps to cfg value: coding agent is not allowed
                # to lower it (it was using max_steps=25 to make tasks easier).
                round_spec.max_steps = cfg.max_steps
                round_spec.max_path_cm = min(round_spec.max_path_cm, 5000.0)
                round_spec.n_episodes = max(round_spec.n_episodes, cfg.episodes_per_gen)
<<<<<<< Updated upstream
                # Per-epoch difficulty floor/ceiling (path-based, deterministic).
                # Coding agent may step path_cm by at most -300 / +800 from the
                # previous epoch. With min==max==path_cm this directly bounds
                # the difficulty contribution from path length.
                if self.gen_results:
                    # Use prev epoch midpoint as anchor; clamp the spec's
                    # CENTER (midpoint) to floor/ceiling, then preserve the
                    # ±15% sampling tolerance around that center so the
                    # navmesh sampler still has geometric slack.
                    prev_lo = float(self.gen_results[-1].get("min_path_cm", 1000.0))
                    prev_hi = float(self.gen_results[-1].get("max_path_cm", prev_lo))
                    prev_center = (prev_lo + prev_hi) / 2.0
                    floor = max(500.0, prev_center - 300.0)
                    ceiling = min(5000.0, prev_center + 800.0)
                    chosen_center = (round_spec.min_path_cm + round_spec.max_path_cm) / 2.0
                    clamped = max(floor, min(ceiling, chosen_center))
                    if abs(clamped - chosen_center) > 1.0:
                        log.info(
                            "Clamped path_cm %.0f -> %.0f (floor=%.0f ceiling=%.0f, prev=%.0f)",
                            chosen_center, clamped, floor, ceiling, prev_center,
                        )
                    round_spec.min_path_cm = max(500.0, clamped * 0.85)
                    round_spec.max_path_cm = min(5000.0, clamped * 1.15)
=======
                if self.gen_results:
                    prev = self.gen_results[-1]
                    floor_min = max(500.0, prev.get("min_path_cm", 500.0) - 500.0)
                    floor_max = max(1000.0, prev.get("max_path_cm", 1000.0) - 500.0)
                    if round_spec.min_path_cm < floor_min:
                        round_spec.min_path_cm = floor_min
                    if round_spec.max_path_cm < floor_max:
                        round_spec.max_path_cm = floor_max
                    if round_spec.min_path_cm >= round_spec.max_path_cm:
                        round_spec.max_path_cm = round_spec.min_path_cm + 500.0
>>>>>>> Stashed changes

                is_new_scene_r = getattr(round_spec, '_is_new_scene', False)
                is_modify_r = getattr(round_spec, '_is_modify', False)
                remove_names_r = getattr(round_spec, '_remove_names', []) or []
                n_changes = (
                    (1 if is_new_scene_r else 0)
                    + len(remove_names_r)
                    + len(round_spec.objects)
                )
                action_label = (
                    "NEW_SCENE" if is_new_scene_r
                    else ("MODIFY" if is_modify_r else "KEEP")
                )
                log.info(
                    "EditRound %d/%d: %s changes=%d path=[%.0f,%.0f] | %s",
                    edit_round + 1, MAX_EDIT_ROUNDS, action_label, n_changes,
                    round_spec.min_path_cm, round_spec.max_path_cm,
                    round_spec.reasoning[:60],
                )

                # Done signals: KEEP, or MODIFY with no add/remove.
                if n_changes == 0:
                    if spec is None:
                        spec = round_spec  # use first round for episode config
                    else:
                        # Keep the latest spec's path/episode params (LLM may
                        # have tweaked them) but preserve the scene state.
                        spec.min_path_cm = round_spec.min_path_cm
                        spec.max_path_cm = round_spec.max_path_cm
                        spec.max_steps = round_spec.max_steps
                        spec.n_episodes = round_spec.n_episodes
                        spec.task_type = round_spec.task_type
                        spec.reasoning = round_spec.reasoning
                    log.info("EditRound %d: LLM finalised scene (no edits)",
                             edit_round + 1)
                    break

                # Apply the requested changes in PIE.
                if is_new_scene_r:
                    try:
                        scene_mgr.clear_scene()
                    except Exception as exc:
                        log.warning("clear_scene failed: %s", exc)
                    self._scene_obj_coords = []
                    self._asset_key_map.clear()
                    time.sleep(2)
                if remove_names_r:
                    try:
                        n_removed = scene_mgr.remove_objects(remove_names_r)
                        log.info("  Removed %d objects: %s", n_removed, remove_names_r)
                    except Exception as exc:
                        log.warning("remove_objects failed: %s", exc)
                    for n in remove_names_r:
                        self._asset_key_map.pop(n, None)
                    # Drop the corresponding coords by name lookup is not
                    # available; safest to rebuild from the asset_key_map
                    # after the build_scene below also updates it.
                    time.sleep(1)
                if round_spec.objects:
                    try:
                        last_report = scene_mgr.build_scene(round_spec)
                    except Exception as exc:
                        log.warning("build_scene failed: %s", exc)
                        last_report = None
                    for obj in round_spec.objects:
                        self._scene_obj_coords.append((obj.x, obj.y))
                        self._asset_key_map[obj.actor_name] = obj.asset_key
                    time.sleep(2)

                spec = round_spec  # carry latest spec forward

                # Early exit: scene is clean and we've done >= 1 successful build.
                if last_report is not None and last_report.is_clean():
                    log.info("EditRound %d: BuildReport clean, ending early",
                             edit_round + 1)
                    break

            if spec is None:
                # Defensive fallback — every round failed before producing a
                # usable spec. Reuse last successful spec from coding_agent.
                spec = coding_agent._last_successful_spec
                if spec is None:
                    log.error("No usable spec after multi-round; skipping epoch")
                    try:
                        PieCycle._exit_pie(ucv)
                    except Exception:
                        pass
                    continue

            self._prev_build_report = last_report

            # ── Persist the finalised scene every epoch (modify or new) ──
            try:
                final_objects = scene_mgr.snapshot_owned_objects(self._asset_key_map)
                self._current_scene_objects = final_objects
                if final_objects:
                    self.ckpt.save_scene(spec.scene_id, final_objects, spec.description)
            except Exception as exc:
                log.warning("snapshot/persist scene failed: %s", exc)
                final_objects = self._current_scene_objects

            # ── Phase 5: Generate episodes ──
            nav_interface = None
            blocked_ratio = 0.0
            use_navmesh = False

            try:
                nav_interface = NavmeshNavigationInterface(ucv)
                test_pts = nav_interface.get_navigable_positions(count=10)
                if len(set(int(p.x) for p in test_pts)) > 1:
                    use_navmesh = True
                    log.info("NavMesh OK: %d navigable points", len(test_pts))
                    try:
                        raw_blocked = measure_blocked_ratio(ucv, seed=cfg.seed + epoch)
                        # Subtract baseline so only LLM-added blockage counts.
                        baseline_anchor = float(self._baseline_blocked_ratio or 0.0)
                        blocked_ratio = max(0.0, raw_blocked - baseline_anchor)
                        log.info(
                            "blocked_ratio raw=%.3f baseline=%.3f effective=%.3f",
                            raw_blocked, baseline_anchor, blocked_ratio,
                        )
                    except Exception:
                        blocked_ratio = 0.0
                else:
                    log.warning("NavMesh not working, using legacy episode gen")
            except Exception as exc:
                log.warning("NavMesh init failed: %s", exc)

            # Compute episode bounds from scene objects
            PLAY_HALF = 5000.0
            scene_obj_coords = list(self._scene_obj_coords)
            if scene_obj_coords:
                obj_xs = [c[0] for c in scene_obj_coords]
                obj_ys = [c[1] for c in scene_obj_coords]
                cx = (min(obj_xs) + max(obj_xs)) / 2
                cy = (min(obj_ys) + max(obj_ys)) / 2
                spread = max(max(obj_xs) - min(obj_xs), max(obj_ys) - min(obj_ys), 2000.0)
                half = max(spread * 0.75, 1500.0) + spec.max_path_cm * 0.5
                half = max(half, 3000.0)
                half = min(half, PLAY_HALF)
                ep_bounds = (cx - half, cy - half, cx + half, cy + half)
                log.info("Episode bounds: center=(%.0f,%.0f) half=%.0f", cx, cy, half)
            else:
                ep_bounds = (-PLAY_HALF, -PLAY_HALF, PLAY_HALF, PLAY_HALF)

            episodes = []
            episodes_data = []
            task_difficulties = []
            for i in range(spec.n_episodes):
                ep_seed = cfg.seed + epoch * 100 + i
                try:
                    if use_navmesh:
                        try:
                            r = sample_pointnav_episode_navmesh(
                                ucv, seed=ep_seed, idx=i,
                                min_geodesic_cm=spec.min_path_cm,
                                max_geodesic_cm=spec.max_path_cm,
                                max_steps=spec.max_steps,
                                build_navmesh=False, nav_interface=nav_interface,
                                bounds=ep_bounds,
                            )
                        except Exception as bounded_exc:
                            msg = str(bounded_exc)
                            if "After bounds filtering" in msg or "bounds" in msg.lower():
                                log.warning(
                                    "Episode %d: bounds sampling failed (%s); retrying without bounds",
                                    i, bounded_exc,
                                )
                                r = sample_pointnav_episode_navmesh(
                                    ucv, seed=ep_seed, idx=i,
                                    min_geodesic_cm=spec.min_path_cm,
                                    max_geodesic_cm=spec.max_path_cm,
                                    max_steps=spec.max_steps,
                                    build_navmesh=False, nav_interface=nav_interface,
                                    bounds=None,
                                )
                            else:
                                raise
                        ep = r["episode"]
                        geo = r["difficulty"]["distance_m"] * 100
                        eucl = math.sqrt(
                            (ep.start_position.x - ep.goal_position.x)**2 +
                            (ep.start_position.y - ep.goal_position.y)**2
                        )
                        detour = r["difficulty"]["detour_ratio"]
                        heading_off = r["difficulty"].get("heading_offset_deg", 0)
                    else:
                        dist = (spec.min_path_cm + spec.max_path_cm) / 2
                        ep = sample_pointnav_episode(
                            ucv, seed=ep_seed, idx=i,
                            target_distance_cm=dist,
                            distance_jitter_cm=(spec.max_path_cm - spec.min_path_cm) / 2,
                            max_steps=spec.max_steps,
                        )
                        eucl = math.sqrt(
                            (ep.start_position.x - ep.goal_position.x)**2 +
                            (ep.start_position.y - ep.goal_position.y)**2
                        )
                        geo = eucl
                        detour = 1.0
                        heading_off = 0.0

                    episodes.append(ep)
                    task_diff = score_task_difficulty(
                        geodesic_cm=geo, euclidean_cm=eucl,
                        heading_offset_deg=heading_off,
                        task_type=spec.task_type,
                        blocked_ratio=blocked_ratio,
                    )
                    task_difficulties.append(task_diff)
                    episodes_data.append({
                        "episode_id": ep.episode_id,
                        "start": {"x": ep.start_position.x, "y": ep.start_position.y},
                        "goal": {"x": ep.goal_position.x, "y": ep.goal_position.y},
                        "geodesic_cm": geo, "euclidean_cm": eucl,
                        "detour_ratio": detour,
                        "difficulty": task_diff,
                    })
                    log.info("  Task %d: geo=%.0fcm detour=%.2f diff=%.1f/10",
                             i, geo, detour, task_diff["total"])
                except Exception as exc:
                    log.warning("Episode %d gen failed: %s", i, exc)

            avg_task_diff = (sum(d["total"] for d in task_difficulties) / len(task_difficulties)
                            if task_difficulties else 0.0)
            # Use the SPEC-based predicted difficulty (deterministic from
            # coding agent's path_cm + n_objects) as the official score, so
            # the curriculum reflects the agent's deliberate choice rather
            # than per-episode geodesic sampling noise. The per-task
            # rubric scores are kept for logging only.
            from .teacher import predict_spec_difficulty as _predict_diff
            spec_difficulty = _predict_diff(spec, blocked_ratio)
            log.info(
                "Difficulty: spec_based=%.2f (deterministic) vs sampled_avg=%.2f",
                spec_difficulty, avg_task_diff,
            )
            avg_task_diff = spec_difficulty
            coding_agent._current_difficulty = avg_task_diff
            # Cache for next epoch's predicted-difficulty pre-validation.
            prev_blocked_ratio = blocked_ratio
            in_band = proposal.band_lo <= avg_task_diff <= proposal.band_hi
            log.info("Realised difficulty: %.2f vs band [%.2f, %.2f] -> %s",
                     avg_task_diff, proposal.band_lo, proposal.band_hi,
                     "IN-BAND" if in_band else "OUT-OF-BAND")

            if not episodes:
                log.error("No episodes for epoch %d", epoch)
                continue

            # ── Phase 6: Run nav agent episodes (PARALLEL via ghost-mode waves) ──
            # Destroy the warmup agent before spawning ghost agents — it has
            # default collision config and would block the navmesh / ghosts.
            try:
<<<<<<< Updated upstream
                ucv.send(f"vset /object/{warmup_agent_name}/destroy")
                log.info("Phase 6: destroyed warmup %s prior to ghost wave", warmup_agent_name)
            except Exception as exc:
                log.warning("Phase 6: warmup agent destroy failed (%s); continuing", exc)
            time.sleep(1)
            # Force UE GC so the destroyed warmup actor releases its FName
            # ("CoEvolveAgent_0") before UnrealCV spawns a new BP. Without
            # this, UnrealCV's spawn-then-rename flow can hit a fatal
            # UObject::Rename assert ("Renaming ... on top of an existing
            # object ... is not allowed") and crash the editor. Observed
            # in production at E5/Phase 6 on 2026-04-25.
            try:
                gc_script = (
                    "import unreal\n"
                    "try:\n"
                    "    unreal.SystemLibrary.collect_garbage()\n"
                    "    unreal.log('coevolve: forced GC after warmup destroy')\n"
                    "except Exception as _e:\n"
                    "    unreal.log_warning('coevolve: collect_garbage failed: ' + repr(_e))\n"
                )
                mcp.execute_python(gc_script, timeout=30)
                log.info("Phase 6: forced UE GC after warmup destroy")
            except Exception as exc:
                log.warning("Phase 6: forced GC failed (%s); continuing", exc)
            time.sleep(2)
=======
                ucv.send("vset /object/CoEvolveAgent_0/destroy")
                log.info("Phase 6: destroyed warmup CoEvolveAgent_0 prior to ghost wave")
            except Exception as exc:
                log.warning("Phase 6: warmup agent destroy failed (%s); continuing", exc)
            time.sleep(1)
>>>>>>> Stashed changes

            epoch_dir = self.output_dir / f"epoch_{epoch:03d}"
            epoch_dir.mkdir(parents=True, exist_ok=True)

            wave_size = max(1, getattr(cfg, "wave_size", 10))
            wave_results: List[Dict[str, Any]] = []
            all_trajectories: List[List[str]] = []
            global_step = 0
            n_waves = (len(episodes) + wave_size - 1) // wave_size
            log.info("Phase 6: running %d episodes as %d wave(s) of up to %d ghost agents",
                     len(episodes), n_waves, wave_size)

            for wave_idx in range(n_waves):
                wave_eps = episodes[wave_idx * wave_size:(wave_idx + 1) * wave_size]
                log.info("Wave %d/%d: %d sequential episodes", wave_idx + 1, n_waves, len(wave_eps))
                try:
                    # Unique per-wave prefix avoids UObject FName collisions
                    # with the previously-destroyed agent (UE GC may not have
                    # reclaimed the FName yet by the next wave).
                    wave_prefix = f"SeqE{epoch:03d}W{wave_idx}"
                    summaries, global_step = run_wave(
                        ucv, mcp, nav_llm, wave_eps,
                        max_steps=spec.max_steps,
                        vision_depth=cfg.vision_depth,
                        memory=nav_memory,
                        global_step=global_step,
                        batch_dir=epoch_dir,
                        save_frames=False,
                        capture_rgb=cfg.capture_rgb,
                        image_kind="rgb" if cfg.capture_rgb else "none",
                        name_prefix=wave_prefix,
                    )
                except Exception as exc:
                    log.error("Wave %d FAILED: %s: %s", wave_idx + 1,
                              type(exc).__name__, exc, exc_info=True)
                    # Mark all episodes in the wave as failures so downstream
                    # SR / coding-reward computation still has data points.
                    for ep in wave_eps:
                        wave_results.append({
                            "episode_id": ep.episode_id,
                            "SR": 0, "SPL": 0, "steps": 0,
                            "path_length_cm": 0.0,
                            "ended_reason": "wave_exception",
                        })
                    try:
                        ucv.hard_reconnect()
                    except Exception:
                        pass
                    continue

                for s in summaries:
                    wave_results.append({
                        "episode_id": s["episode_id"],
                        "SR": float(s.get("SR", 0) or 0),
                        "SPL": float(s.get("SPL", 0) or 0),
                        "steps": int(s.get("steps", 0) or 0),
                        "path_length_cm": float(s.get("path_length_cm", 0) or 0),
                        "ended_reason": s.get("ended_reason", "max_steps"),
                    })
                    log.info("  ep %s: SR=%.0f SPL=%.3f steps=%d %s",
                             s["episode_id"], s.get("SR", 0),
                             s.get("SPL", 0), s.get("steps", 0),
                             s.get("ended_reason", ""))
                all_trajectories.append([])  # per-step trajectories live in epoch_dir/ep_*

            # ── Phase 7: Aggregate + save ──
            n = len(wave_results)
            n_success = sum(1 for r in wave_results if r.get("SR", 0) > 0)
            sr = n_success / n if n else 0
            spl = sum(r.get("SPL", 0) for r in wave_results) / n if n else 0
            avg_steps = sum(r.get("steps", 0) for r in wave_results) / n if n else 0
            # Measure float penalty in PIE — check if objects are grounded.
            float_penalty = 0.0
            spawned = scene_mgr.get_scene_objects()
            if spawned:
                try:
                    float_penalty = measure_float_penalty(ucv, spawned)
                except Exception as fp_exc:
                    log.warning("Float penalty check failed: %s", fp_exc)

            coding_reward = compute_coding_reward(
                sr, difficulty=avg_task_diff,
                best_difficulty=coding_agent._best_difficulty,
                float_penalty=float_penalty,
            )

            # ── Update curriculum teacher with this epoch's outcome ──
            try:
                teacher.update(
                    target=proposal.target,
                    observed_difficulty=avg_task_diff,
                    sr_per_episode=[float(r.get("SR", 0) or 0) for r in wave_results],
                )
                teacher.save(teacher_state_path)
            except Exception as t_exc:
                log.warning("Teacher update/save failed: %s", t_exc)

            coding_mem.record(epoch, spec, sr, avg_task_diff)
            coding_mem.maybe_reflect(epoch)

            nav_strategies = nav_memory.query("", k=10) if hasattr(nav_memory, 'query') else []

            gen_record = {
                "generation": epoch,
                "sr": sr, "spl": spl, "avg_steps": avg_steps,
                "n_episodes": n, "n_success": n_success,
                "difficulty_score": avg_task_diff,
                "task_difficulties": task_difficulties,
                "blocked_ratio": blocked_ratio,
                "coding_reward": coding_reward,
                "scene_id": spec.scene_id,
                "scene_description": spec.description,
                "task_type": spec.task_type,
                "min_path_cm": spec.min_path_cm,
                "max_path_cm": spec.max_path_cm,
                "task_reasoning": spec.reasoning,
                "nav_strategies": list(nav_strategies),
                "coding_principles": list(coding_mem.principles),
                "episode_results": wave_results,
                # ── curriculum teacher diagnostics ──
                "teacher_name": teacher.name,
                "teacher_target": proposal.target,
                "teacher_band": [proposal.band_lo, proposal.band_hi],
                "teacher_rationale": proposal.rationale,
                "predicted_difficulty": float(getattr(spec, "_predicted_difficulty", -1.0)),
                "in_band": bool(in_band),
            }
            self.gen_results.append(gen_record)
            self.ctx.add_generation(gen_record)

            self.ckpt.save_epoch_data(epoch, spec.to_dict(), episodes_data,
                                       all_trajectories, gen_record)

            scene_objs = list(self._current_scene_objects) if self._current_scene_objects else [
                {"actor_name": n, "asset_key": self._asset_key_map.get(n, ""),
                 "x": 0, "y": 0, "z": 0, "yaw": 0}
                for n in scene_mgr.get_scene_objects()
            ]
            ckpt_dict = vars(cfg).copy()
            ckpt_dict["difficulty"] = avg_task_diff
            self.ckpt.save(epoch, self.gen_results, spec.scene_id, scene_objs, ckpt_dict)

            summary = (
                f"Epoch {epoch}: SR={sr:.0%} SPL={spl:.3f} "
                f"diff={avg_task_diff:.1f}/10 reward={coding_reward:.2f} "
                f"scene={spec.scene_id}({len(scene_mgr.get_scene_objects())}obj) "
                f"| {spec.reasoning[:50]}"
            )
            log.info(summary)
            print(f"\n  >>> {summary}\n")

        # Final: if still in PIE, exit via UCV.
        if ucv is not None:
            self._exit_pie(ucv)
            try:
                ucv.disconnect()
            except Exception:
                pass
        self._save_final()
        try:
            scene_mgr.clear_scene()
        except Exception:
            pass
        return self.gen_results

    def _save_final(self):
        path = self.output_dir / "all_results.json"
        path.write_text(json.dumps(self.gen_results, indent=2, default=str), encoding="utf-8")
        log.info("Results saved: %s", path)

    @staticmethod
    def _make_llm_call(model_id, base_url, api_key):
        """Build a per-call LLM closure with fresh OpenAI client each call.

        Fresh client = fresh httpx pool. Reusing a single long-lived client
        across ucv.connect() on Windows reproduced WinError 10061 on every
        subsequent outbound request. Per-call clients work. Connect timeout
        is short so transient SYN rejections fail fast and retry.
        """
        from openai import OpenAI
        import httpx as _httpx
        import time as _time
        _timeout = _httpx.Timeout(connect=5.0, read=90.0, write=30.0, pool=5.0)

        def call(prompt: str) -> str:
            client = OpenAI(
                api_key=api_key, base_url=base_url,
                timeout=_timeout, max_retries=1,
            )
            try:
                extra_body = None
                if "qwen3" in str(model_id).lower():
                    # Keep coding turns short and deterministic for JSON output.
                    extra_body = {"chat_template_kwargs": {"enable_thinking": False}}
                t0 = _time.time()
                resp = client.chat.completions.create(
                    model=model_id,
                    messages=[{"role": "user", "content": prompt}],
                    # Coding agent needs enough tokens for complete JSON output.
                    max_tokens=2048, temperature=0.1,
                    extra_body=extra_body,
                )
                dt = _time.time() - t0
                log.info("coding_llm response in %.2fs (model=%s)", dt, model_id)
                return resp.choices[0].message.content or ""
            finally:
                try:
                    client.close()
                except Exception:
                    pass
        return call
