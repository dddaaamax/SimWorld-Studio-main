#!/usr/bin/env python3
"""SimWorld Studio — Static Scene Evaluation Runner.

Runs coding agent (Claude Code) experiments with full chat history logging,
optional verification loop, and background map support.

Usage:
    # Single test (S1-easy with Qwen3.5-9B)
    python run_eval.py --agent claude-qwen3.5-9b --setting s1 --difficulty easy

    # With verification loop
    python run_eval.py --agent claude-qwen3.5-9b --verify

    # With background map
    python run_eval.py --agent claude-sonnet --background-map TestMap4

    # Custom vLLM endpoint
    python run_eval.py --agent claude-qwen3.5-9b --vllm-url http://132.239.95.133:8002

    # Full experiment (all settings × difficulties)
    python run_eval.py --agent claude-sonnet

    # List agents
    python run_eval.py --list-agents
"""

import argparse
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from collections import Counter

# ─── Paths ────────────────────────────────────────────────────────────────────
BASE_DIR = Path(__file__).parent.resolve()
MCP_CONFIG = str(BASE_DIR / "mcp.json")
RESULTS_DIR = BASE_DIR / "results"

# ─── Agent definitions ────────────────────────────────────────────────────────
# Each agent maps to a Claude Code CLI configuration.
# For open-source models, we override ANTHROPIC_BASE_URL to point to a
# vLLM server that speaks the Anthropic Messages API.
#
# Default vLLM endpoints (edit these or use --vllm-url to override):
#   Qwen3.5-27B: http://132.239.95.133:8001/v1  (or localhost:30001)
#   Qwen3.5-9B:  http://132.239.95.133:8002/v1  (or localhost:30002)
#   Qwen3.5-2B:  http://132.239.95.133:8003/v1  (or localhost:30003)

AGENTS = {
    # ── Anthropic models (direct API, no vLLM needed) ──
    "claude-sonnet": {
        "env": {},
        "model": "sonnet",
        "extra_args": [],
        "display": "Claude Code + Sonnet 4",
    },
    "claude-opus": {
        "env": {},
        "model": "opus",
        "extra_args": [],
        "display": "Claude Code + Opus 4",
    },
    "claude-haiku": {
        "env": {},
        "model": "haiku",
        "extra_args": [],
        "display": "Claude Code + Haiku",
    },
    # ── Open-source models via vLLM ──
    "claude-qwen3.5-9b": {
        "env": {
            "ANTHROPIC_BASE_URL": "http://localhost:30015",  # proxy -> vLLM
            "ANTHROPIC_API_KEY": "dummy",
            "ANTHROPIC_AUTH_TOKEN": "dummy",
            "ANTHROPIC_DEFAULT_SONNET_MODEL": "Qwen3.5-9B",
            "ANTHROPIC_DEFAULT_OPUS_MODEL": "Qwen3.5-9B",
            "ANTHROPIC_DEFAULT_HAIKU_MODEL": "Qwen3.5-9B",
        },
        "model": "sonnet",
        "extra_args": ["--bare"],
        "display": "Claude Code + Qwen3.5-9B (vLLM)",
    },
    "claude-qwen3.5-27b": {
        "env": {
            "ANTHROPIC_BASE_URL": "http://localhost:30002",
            "ANTHROPIC_API_KEY": "dummy",
            "ANTHROPIC_AUTH_TOKEN": "dummy",
            "ANTHROPIC_DEFAULT_SONNET_MODEL": "Qwen3.5-27B",
            "ANTHROPIC_DEFAULT_OPUS_MODEL": "Qwen3.5-27B",
            "ANTHROPIC_DEFAULT_HAIKU_MODEL": "Qwen3.5-27B",
        },
        "model": "sonnet",
        "extra_args": ["--bare"],
        "display": "Claude Code + Qwen3.5-27B (vLLM)",
    },
    "claude-qwen3.5-2b": {
        "env": {
            "ANTHROPIC_BASE_URL": "http://localhost:30003",
            "ANTHROPIC_API_KEY": "dummy",
            "ANTHROPIC_AUTH_TOKEN": "dummy",
            "ANTHROPIC_DEFAULT_SONNET_MODEL": "Qwen3.5-2B",
            "ANTHROPIC_DEFAULT_OPUS_MODEL": "Qwen3.5-2B",
            "ANTHROPIC_DEFAULT_HAIKU_MODEL": "Qwen3.5-2B",
        },
        "model": "sonnet",
        "extra_args": ["--bare"],
        "display": "Claude Code + Qwen3.5-2B (vLLM)",
    },
}

# ─── Prompts ──────────────────────────────────────────────────────────────────
# Setting 1: Text-to-Scene (3 difficulty levels)
# Setting 2: Image+Text-to-Scene (3 difficulty levels)
# Setting 3: Scene Editing (3 difficulty levels)

PROMPTS = {
    "s1": {
        "easy": "Build a street with 3 houses in a row and a tree next to each house.",
        "mid": (
            "Create a town plaza: place 4 different buildings forming a square around "
            "a central open area. Put tables and a couch for outdoor seating in the center. "
            "Add 6 trees around the perimeter. Place a road along the south edge with "
            "2 scooters parked nearby. Add trash bins at each corner of the plaza."
        ),
        "hard": (
            "Design a complete residential neighborhood with two parallel streets. "
            "Place 3 varied buildings on each side of both streets (12 buildings total, "
            "use at least 5 different building types). Connect the streets with a cross road. "
            "Line all roads with trees on both sides (at least 16 trees). "
            "Create a central park between the streets with 3 tables, 2 couches, and 4 trash bins. "
            "Add fire hydrants at every intersection. Park scooters and carts along the main road. "
            "Place road cones and blockers to mark a construction zone at one end."
        ),
    },
    "s2": {
        "easy": (
            "ref_images/S2-E-01.png",
            "Build a scene matching this sketch: a row of small houses along a street with trees."
        ),
        "mid": (
            "ref_images/S2-E-02.png",
            "Build a scene matching this sketch: a square open area with trees at the four corners, "
            "street furniture in the center, and buildings nearby."
        ),
        "hard": (
            "ref_images/city_4.jpg",
            "Build a dense city block matching this aerial photo: multiple buildings in a grid, "
            "streets between them, trees lining the roads, vehicles and street furniture throughout."
        ),
    },
    "s3": {
        "base_umap": "base_scenes/plaza_base.umap",
        "easy": "Add 2 trash bins near the center and 1 fire hydrant near a building.",
        "mid": (
            "Add a road along one edge connecting two buildings. "
            "Place 2 scooters near the road. Add 3 trees to fill gaps. Add 2 tables."
        ),
        "hard": (
            "Expand into a larger district: add 2 new buildings on the north side. "
            "Connect them with roads to existing buildings. Plant 6 trees along new roads. "
            "Create a marketplace with 3 tables and 2 carts in the center. "
            "Add road cones and blockers for a construction zone. "
            "Place hydrants at intersections and trash bins along sidewalks."
        ),
    },
}


def build_system_prompt(background_map=None, verify_enabled=False, is_vllm=False):
    """Build the system prompt for the coding agent."""
    prompt = (
        "You build 3D city scenes in Unreal Engine 5 using MCP tools.\n"
        "Call list_assets() to see available assets.\n"
        "Workflow: delete_all_spawned -> setup_environment -> spawn objects -> take_screenshot.\n"
        "Buildings: BP_Building_01-99 (01-09 small, 10-30 medium, 31+ tall). Trees: BP_Tree1-6.\n"
        "Furniture: BP_Table, BP_Couch, BP_Hydrant, BP_Trash_bin_a/b, BP_RoadCone, etc.\n"
        "Roads: spawn_actor with static_mesh=/Game/CityDatabase/meshes/SM_Road.SM_Road\n"
        "Units: 1m=100. Space buildings 3000-8000 apart.\n"
    )
    if is_vllm:
        # Instruct the model to use Hermes JSON format for tool calls.
        # This is required for vLLM's hermes tool-call parser to detect and
        # parse tool invocations into structured tool_use blocks.
        prompt += (
            "\nTOOL CALL FORMAT — you MUST call tools using ONLY this JSON format:\n"
            "<tool_call>\n"
            '{"name": "tool_name", "arguments": {"param1": "value1"}}\n'
            "</tool_call>\n"
            "NEVER use <function=> or <parameter=> XML tags. ONLY use the JSON format above.\n"
            "\nExample — spawning a building:\n"
            "<tool_call>\n"
            '{"name": "mcp__simworld__spawn_blueprint_actor", '
            '"arguments": {"actor_name": "House_1", "blueprint_id": "BP_Building_01", "location": [0, 0, 0]}}\n'
            "</tool_call>\n"
            "\nExample — setting up the environment:\n"
            "<tool_call>\n"
            '{"name": "mcp__simworld__setup_environment", '
            '"arguments": {"time_of_day": "afternoon"}}\n'
            "</tool_call>\n"
            "\nCall tools one at a time. After each tool call, wait for the result before calling the next tool.\n"
        )
    if background_map:
        prompt += (
            f"\nIMPORTANT: When calling setup_environment, pass background_map=\"{background_map}\" "
            "to load a pre-built terrain as the scene backdrop.\n"
        )
    if verify_enabled:
        prompt += (
            "\nVERIFICATION LOOP: After building the scene and taking a screenshot, "
            "call verify(prompt=<your original task prompt>) to check scene quality. "
            "The verify tool runs rule-based metrics (collision, grounding, bounds, counts) "
            "and returns structured feedback. If issues are found, fix them and verify again. "
            "Repeat until all checks pass. Also visually inspect the screenshot.\n"
        )
    return prompt


def check_ue(port=55560):
    """Check if UE is reachable on TCP port."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(3)
        s.connect(("127.0.0.1", port))
        s.close()
        return True
    except Exception:
        return False


def run_agent(agent_name, prompt, output_dir, timeout=300, run_label="run"):
    """Run a Claude Code agent and save full chat history.

    Saves to output_dir:
      - raw_stream.jsonl      : raw stream-json output from Claude Code
      - chat_history.json     : parsed structured chat history
      - agent_metadata.json   : agent config, timing, cost, model info
    """
    cfg = AGENTS[agent_name]
    os.makedirs(output_dir, exist_ok=True)

    cmd = [
        "claude", "-p", prompt,
        "--model", cfg["model"],
        "--mcp-config", MCP_CONFIG,
        "--output-format", "stream-json",
        "--verbose",
        "--dangerously-skip-permissions",
    ]
    for arg in cfg.get("extra_args", []):
        cmd.append(arg)

    env = os.environ.copy()
    env.update(cfg.get("env", {}))
    if cfg.get("env", {}).get("ANTHROPIC_BASE_URL"):
        for k in list(env.keys()):
            if k.startswith("CLAUDE") and k != "CLAUDECODE":
                env.pop(k, None)

    start = time.time()
    timestamp = datetime.now(timezone.utc).isoformat()

    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout,
            cwd="/tmp/experiment_sandbox", env=env,
        )
        duration_ms = int((time.time() - start) * 1000)

        # Save raw output
        with open(os.path.join(output_dir, "raw_stream.jsonl"), "w") as f:
            f.write(proc.stdout)
        if proc.stderr.strip():
            with open(os.path.join(output_dir, "stderr.log"), "w") as f:
                f.write(proc.stderr)

        # Parse stream-json
        tool_calls = []
        text_blocks = []
        tool_count = 0
        is_error = False
        cost = 0.0
        messages = []

        for line in proc.stdout.strip().split("\n"):
            if not line.strip():
                continue
            try:
                ev = json.loads(line)
                ev_type = ev.get("type", "")

                if ev_type == "assistant":
                    msg_content = ev.get("message", {}).get("content", [])
                    msg_entry = {"role": "assistant", "content": []}
                    for b in msg_content:
                        btype = b.get("type", "")
                        if btype == "tool_use":
                            tool_count += 1
                            entry = {
                                "type": "tool_use", "id": b.get("id", ""),
                                "name": b.get("name", ""), "input": b.get("input", {}),
                            }
                            tool_calls.append(entry)
                            msg_entry["content"].append(entry)
                        elif btype == "text":
                            text = b.get("text", "")
                            # Also count XML tool calls in text
                            xml_tools = re.findall(r'<function=(\w+)>', text)
                            tool_count += len(xml_tools)
                            text_blocks.append(text)
                            msg_entry["content"].append({"type": "text", "text": text})
                    if msg_entry["content"]:
                        messages.append(msg_entry)

                elif ev_type == "user":
                    msg_content = ev.get("message", {}).get("content", [])
                    msg_entry = {"role": "user", "content": []}
                    for b in msg_content:
                        if b.get("type") == "tool_result":
                            msg_entry["content"].append({
                                "type": "tool_result",
                                "tool_use_id": b.get("tool_use_id", ""),
                                "content": str(b.get("content", ""))[:2000],
                            })
                    if msg_entry["content"]:
                        messages.append(msg_entry)

                elif ev_type == "result":
                    is_error = ev.get("is_error", False)
                    cost = ev.get("total_cost_usd", 0) or 0

            except json.JSONDecodeError:
                pass

        # Save chat history
        chat_history = {
            "agent": agent_name,
            "model": cfg["model"],
            "display_name": cfg.get("display", agent_name),
            "vllm_url": cfg.get("env", {}).get("ANTHROPIC_BASE_URL", "anthropic-api"),
            "timestamp": timestamp,
            "run_label": run_label,
            "messages": messages,
            "tool_calls_summary": [
                {"name": t["name"], "input_keys": list(t["input"].keys())}
                for t in tool_calls
            ],
        }
        with open(os.path.join(output_dir, "chat_history.json"), "w") as f:
            json.dump(chat_history, f, indent=2, default=str)

        # Save metadata
        metadata = {
            "agent": agent_name,
            "model": cfg["model"],
            "display_name": cfg.get("display", agent_name),
            "vllm_url": cfg.get("env", {}).get("ANTHROPIC_BASE_URL", "anthropic-api"),
            "timestamp": timestamp,
            "duration_ms": duration_ms,
            "tool_count": tool_count,
            "cost_usd": cost,
            "success": proc.returncode == 0 and not is_error,
            "run_label": run_label,
        }
        with open(os.path.join(output_dir, "agent_metadata.json"), "w") as f:
            json.dump(metadata, f, indent=2)

        return {
            "success": proc.returncode == 0 and not is_error,
            "tool_count": tool_count,
            "duration_ms": duration_ms,
            "cost_usd": cost,
        }

    except subprocess.TimeoutExpired:
        duration_ms = int((time.time() - start) * 1000)
        metadata = {
            "agent": agent_name, "model": cfg["model"], "timestamp": timestamp,
            "duration_ms": duration_ms, "success": False, "error": f"Timeout after {timeout}s",
        }
        with open(os.path.join(output_dir, "agent_metadata.json"), "w") as f:
            json.dump(metadata, f, indent=2)
        return {"success": False, "tool_count": 0, "duration_ms": duration_ms, "cost_usd": 0}

    except Exception as e:
        metadata = {
            "agent": agent_name, "model": cfg["model"], "timestamp": timestamp,
            "success": False, "error": str(e),
        }
        with open(os.path.join(output_dir, "agent_metadata.json"), "w") as f:
            json.dump(metadata, f, indent=2)
        return {"success": False, "tool_count": 0, "duration_ms": 0, "cost_usd": 0, "error": str(e)}


def export_scene(export_dir, server_port=3002):
    """Export scene via web server. Returns screenshot count."""
    import requests
    os.makedirs(export_dir, exist_ok=True)
    try:
        exp = requests.post(
            f"http://localhost:{server_port}/api/export-evaluation",
            json={"prompt": "export", "chatHistory": []},
            timeout=300
        ).json()
        folder = exp.get("folder", "")
        ss = exp.get("manifest", {}).get("screenshotCount", 0)
        if folder and ss > 0:
            for f in Path(folder).iterdir():
                shutil.copy2(str(f), os.path.join(export_dir, f.name))
        return ss
    except Exception as e:
        print(f"    Export failed: {e}")
        return 0


def run_single(agent_name, setting, difficulty, results_dir,
               background_map=None, verify_enabled=False, timeout=300,
               skip_eval=False):
    """Run a single agent x setting x difficulty combination."""
    os.makedirs("/tmp/experiment_sandbox", exist_ok=True)
    run_id = f"{setting}_{difficulty}"
    run_dir = str(results_dir / agent_name / run_id)

    # Detect if this agent uses vLLM (needs tool-call format instructions)
    is_vllm = bool(AGENTS[agent_name].get("env", {}).get("ANTHROPIC_BASE_URL"))
    sys_prompt = build_system_prompt(
        background_map=background_map, verify_enabled=verify_enabled, is_vllm=is_vllm
    )

    if setting == "s1":
        task_prompt = PROMPTS["s1"][difficulty]
        full_prompt = f"{sys_prompt}\n\n{task_prompt}"
    elif setting == "s2":
        ref_image, text = PROMPTS["s2"][difficulty]
        ref_path = str(BASE_DIR / ref_image) if not os.path.isabs(ref_image) else ref_image
        full_prompt = (
            f"{sys_prompt}\n\nReference image: {ref_path}\n"
            f"Read it first, then build a scene that matches.\nDescription: {text}"
        )
    elif setting == "s3":
        base_path = str(BASE_DIR / PROMPTS["s3"]["base_umap"])
        edit_inst = PROMPTS["s3"][difficulty]
        full_prompt = (
            f"{sys_prompt}\n\nLoad base scene: {base_path}\n"
            f"Then apply edit: {edit_inst}\nDo NOT call delete_all_spawned or setup_environment."
        )
    else:
        raise ValueError(f"Unknown setting: {setting}")

    print(f"\n  [{run_id}] Generating with {agent_name}...")
    gen = run_agent(agent_name, full_prompt, run_dir, timeout=timeout, run_label=run_id)
    print(f"    tools={gen['tool_count']} ok={gen['success']} time={gen['duration_ms']//1000}s cost=${gen.get('cost_usd',0):.4f}")
    time.sleep(5)

    # Export scene (screenshots + scene graph)
    export_dir = os.path.join(run_dir, "export")
    ss = 0
    if check_ue():
        ss = export_scene(export_dir)
    print(f"    screenshots={ss}")

    # Save prompt info
    with open(os.path.join(run_dir, "prompt_info.json"), "w") as f:
        json.dump({
            "setting": setting, "difficulty": difficulty,
            "prompt": PROMPTS.get(setting, {}).get(difficulty, ""),
            "background_map": background_map, "verify_enabled": verify_enabled,
        }, f, indent=2, default=str)

    result = {
        "setting": setting, "difficulty": difficulty, "agent": agent_name,
        "generation": gen, "screenshots": ss,
    }
    with open(os.path.join(run_dir, "result.json"), "w") as f:
        json.dump(result, f, indent=2, default=str)

    return result


def main():
    parser = argparse.ArgumentParser(
        description="SimWorld Studio — Static Scene Evaluation",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--agent", action="append", dest="agents", default=[],
                        help="Agent(s) to run (repeatable)")
    parser.add_argument("--setting", choices=["s1", "s2", "s3", "all"], default="all")
    parser.add_argument("--difficulty", choices=["easy", "mid", "hard", "all"], default="all")
    parser.add_argument("--output-dir", default=None)
    parser.add_argument("--background-map", default=None,
                        help="UE map name for backdrop (e.g., 'TestMap4')")
    parser.add_argument("--verify", action="store_true",
                        help="Enable verification loop (agent self-evaluates)")
    parser.add_argument("--timeout", type=int, default=300)
    parser.add_argument("--skip-eval", action="store_true")
    parser.add_argument("--vllm-url", default=None,
                        help="Override vLLM base URL (e.g., http://132.239.95.133:8002)")
    parser.add_argument("--list-agents", action="store_true")
    args = parser.parse_args()

    if args.list_agents:
        print("Available agents:")
        for name, cfg in AGENTS.items():
            vllm = cfg.get("env", {}).get("ANTHROPIC_BASE_URL", "Anthropic API")
            print(f"  {name:25s}  {cfg.get('display', name):35s}  {vllm}")
        return

    if not args.agents:
        args.agents = ["claude-qwen3.5-9b"]

    if args.vllm_url:
        for name in args.agents:
            if name in AGENTS and AGENTS[name].get("env", {}).get("ANTHROPIC_BASE_URL"):
                AGENTS[name]["env"]["ANTHROPIC_BASE_URL"] = args.vllm_url
                print(f"  Override {name} vLLM -> {args.vllm_url}")

    settings = ["s1", "s2", "s3"] if args.setting == "all" else [args.setting]
    difficulties = ["easy", "mid", "hard"] if args.difficulty == "all" else [args.difficulty]

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    results_dir = Path(args.output_dir) if args.output_dir else RESULTS_DIR / f"run_{ts}"
    results_dir.mkdir(parents=True, exist_ok=True)

    if not check_ue():
        print("WARNING: UE not reachable on port 55560.")

    if not Path(MCP_CONFIG).exists():
        print(f"ERROR: MCP config not found at {MCP_CONFIG}")
        print("Copy mcp.json.example to mcp.json and edit paths.")
        sys.exit(1)

    all_results = []
    for agent_name in args.agents:
        if agent_name not in AGENTS:
            print(f"ERROR: Unknown agent '{agent_name}'. Use --list-agents.")
            continue
        print(f"\n{'='*60}")
        print(f"AGENT: {agent_name} ({AGENTS[agent_name].get('display', '')})")
        print(f"{'='*60}")

        for setting in settings:
            for diff in difficulties:
                result = run_single(
                    agent_name, setting, diff, results_dir,
                    background_map=args.background_map,
                    verify_enabled=args.verify,
                    timeout=args.timeout,
                    skip_eval=args.skip_eval,
                )
                all_results.append(result)
                time.sleep(3)

    summary = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "agents": args.agents,
        "settings": settings,
        "difficulties": difficulties,
        "background_map": args.background_map,
        "verify_enabled": args.verify,
        "results": all_results,
    }
    (results_dir / "summary.json").write_text(json.dumps(summary, indent=2, default=str))
    print(f"\nResults saved to {results_dir}/")


if __name__ == "__main__":
    main()
