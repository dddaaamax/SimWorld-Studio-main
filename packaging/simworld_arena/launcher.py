"""
SimWorld Studio Launcher

Single-command launcher that:
1. Checks prerequisites (Node.js, LLM auth, GPU)
2. Launches UE binary (headless)
3. Waits for MCP port to become available
4. Starts the Studio web server
5. Prints access URL (auto-detects local vs remote)
"""
import argparse
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

from . import __version__


def get_package_dir():
    """Return the directory where this package is installed."""
    return Path(__file__).parent


def find_node():
    """Find node binary."""
    node = shutil.which("node")
    if not node:
        print("[ERROR] Node.js not found. Install with: apt-get install -y nodejs")
        sys.exit(1)
    return node


def resolve_llm_provider():
    """Resolve the configured model provider from environment variables."""
    provider = (
        os.environ.get("SIMWORLD_LLM_PROVIDER")
        or os.environ.get("LLM_PROVIDER")
        or os.environ.get("AI_PROVIDER")
        or ""
    ).strip().lower()
    aliases = {
        "anthropic": "claude",
        "claude-code": "claude",
        "dashscope": "qwen",
        "aliyun": "qwen",
        "qianwen": "qwen",
    }
    provider = aliases.get(provider, provider)
    if provider:
        return provider
    if os.environ.get("DEEPSEEK_API_KEY"):
        return "deepseek"
    if os.environ.get("DASHSCOPE_API_KEY") or os.environ.get("QWEN_API_KEY"):
        return "qwen"
    if os.environ.get("SIMWORLD_LLM_API_KEY") or os.environ.get("LLM_API_KEY") or os.environ.get("OPENAI_API_KEY"):
        return "openai"
    return "claude"


def check_llm_auth():
    """Check if the configured LLM provider is authenticated."""
    provider = resolve_llm_provider()
    if provider == "deepseek":
        return "DeepSeek API key" if os.environ.get("DEEPSEEK_API_KEY") or os.environ.get("SIMWORLD_LLM_API_KEY") else None
    if provider == "qwen":
        return "Qwen/DashScope API key" if (
            os.environ.get("DASHSCOPE_API_KEY") or
            os.environ.get("QWEN_API_KEY") or
            os.environ.get("SIMWORLD_LLM_API_KEY")
        ) else None
    if provider == "openai":
        return "OpenAI-compatible API key" if (
            os.environ.get("SIMWORLD_LLM_API_KEY") or
            os.environ.get("LLM_API_KEY") or
            os.environ.get("OPENAI_API_KEY")
        ) else None

    # Claude compatibility path: API key first.
    if os.environ.get("ANTHROPIC_API_KEY"):
        return "Claude API key"

    # Check Claude Code OAuth
    claude = shutil.which("claude")
    if claude:
        try:
            result = subprocess.run(
                [claude, "auth", "status"],
                capture_output=True, text=True, timeout=10,
            )
            if result.returncode == 0 and '"loggedIn": true' in result.stdout:
                return "Claude OAuth"
        except Exception:
            pass

    return None


def detect_gpu():
    """Detect available GPUs and return count."""
    try:
        result = subprocess.run(
            ["nvidia-smi", "--query-gpu=index,name,memory.total", "--format=csv,noheader"],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode == 0:
            gpus = [line.strip() for line in result.stdout.strip().split("\n") if line.strip()]
            return gpus
    except Exception:
        pass
    return []


def get_server_ip():
    """Get the server's external IP address."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


def is_local_machine():
    """Check if we're likely on a local machine (has DISPLAY or Wayland)."""
    return bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))


def wait_for_port(port, host="127.0.0.1", timeout=120):
    """Wait for a TCP port to become available."""
    start = time.time()
    while time.time() - start < timeout:
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.settimeout(2)
            s.connect((host, port))
            s.close()
            return True
        except (ConnectionRefusedError, socket.timeout, OSError):
            time.sleep(2)
    return False


def normalize_map_path(map_path: str) -> str:
    """Normalize UE map path to include .umap suffix when omitted."""
    if not map_path:
        return "/Game/Main.umap"
    return map_path if map_path.endswith(".umap") else f"{map_path}.umap"


def setup_workspace(workspace, pkg_dir):
    """
    Create/update workspace with bundled files.
    Only copies files if they're missing or package version changed.
    """
    workspace = Path(workspace)
    pkg_dir = Path(pkg_dir)

    # Version tracking
    version_file = workspace / ".studio_version"
    current_version = version_file.read_text().strip() if version_file.exists() else ""

    needs_update = current_version != __version__

    # Create directory structure
    for d in [
        "web/server",
        "web/dist",
        "arena/skills/builtin",
        "arena/config",
        "scenes",
        "skills",
        "tmp/screens",
        "tmp/thumbnails",
        "logs",
        "arena_data",
    ]:
        (workspace / d).mkdir(parents=True, exist_ok=True)

    if needs_update:
        print(f"  Setting up workspace (v{__version__})...")

        # Copy server JS files
        server_src = pkg_dir / "server"
        server_dst = workspace / "web" / "server"
        if server_src.exists():
            for f in server_src.iterdir():
                if f.is_file():
                    shutil.copy2(f, server_dst / f.name)

        # Copy frontend dist
        dist_src = pkg_dir / "server" / "dist"
        dist_dst = workspace / "web" / "dist"
        if dist_src.exists():
            shutil.rmtree(dist_dst, ignore_errors=True)
            shutil.copytree(dist_src, dist_dst, dirs_exist_ok=True)

        # Copy skills
        skills_src = pkg_dir / "skills" / "builtin"
        skills_dst = workspace / "arena" / "skills" / "builtin"
        if skills_src.exists():
            for f in skills_src.glob("*.md"):
                shutil.copy2(f, skills_dst / f.name)

        # Copy config
        config_src = pkg_dir / "config"
        config_dst = workspace / "arena" / "config"
        if config_src.exists():
            for f in config_src.iterdir():
                if f.is_file():
                    shutil.copy2(f, config_dst / f.name)

        # Install npm deps
        server_dst = workspace / "web" / "server"
        pkg_json = server_dst / "package.json"
        if pkg_json.exists():
            node_modules = server_dst / "node_modules"
            if not node_modules.exists():
                print("  Installing Node.js dependencies...")
                subprocess.run(
                    ["npm", "install", "--production", "--no-optional", "--no-audit", "--no-fund"],
                    cwd=str(server_dst),
                    capture_output=True,
                )

        # Write version marker
        version_file.write_text(__version__)
    else:
        # Still ensure npm deps exist
        server_dst = workspace / "web" / "server"
        node_modules = server_dst / "node_modules"
        if not node_modules.exists():
            pkg_json = server_dst / "package.json"
            if pkg_json.exists():
                subprocess.run(
                    ["npm", "install", "--production", "--no-optional", "--no-audit", "--no-fund"],
                    cwd=str(server_dst),
                    capture_output=True,
                )

    return workspace


def generate_mcp_config(workspace, ue_host, ue_port):
    """Generate mcp.json pointing to the workspace MCP server."""
    mcp_server_path = str(Path(workspace) / "web" / "server" / "mcp-server.js")
    config = {
        "mcpServers": {
            "simworld": {
                "command": "node",
                "args": [mcp_server_path],
                "env": {
                    "UNREAL_HOST": ue_host,
                    "UNREAL_PORT": str(ue_port),
                },
            }
        }
    }
    config_path = Path(workspace) / "web" / "mcp.json"
    config_path.write_text(json.dumps(config, indent=2))
    return str(config_path)


def find_simworld_binary(binary_path=None):
    """Find the SimWorld binary directory or UE installation.
    
    Supports:
    1. Environment variable UE_ROOT (for local UE installation)
    2. Command line argument --binary
    3. SimWorld-Studio-Minimal in common locations
    """
    search_paths = []
    
    # Priority 1: Environment variable UE_ROOT (for local UE installation)
    ue_root = os.environ.get("UE_ROOT")
    if ue_root:
        ue_root_path = Path(ue_root)
        if (ue_root_path / "Engine" / "Binaries" / "Linux" / "UnrealEditor").exists():
            return ue_root_path
    
    # Priority 2: Command line argument
    if binary_path:
        search_paths.append(Path(binary_path))
    
    # Priority 3: Common locations for SimWorld-Studio-Minimal
    search_paths.extend([
        Path.cwd() / "SimWorld-Studio-Minimal",
        Path.cwd(),
        Path.home() / "SimWorld-Studio-Minimal",
    ])

    for p in search_paths:
        if (p / "Engine" / "Binaries" / "Linux" / "UnrealEditor").exists():
            return p
    return None


def find_ue_project(ue_root_path):
    """Find the UE project file.
    
    Supports:
    1. Environment variable UE_PROJECT_PATH (for local project)
    2. Default gym_citynav project in SimWorld-Studio-Minimal
    """
    # Priority 1: Environment variable UE_PROJECT_PATH
    ue_project_path = os.environ.get("UE_PROJECT_PATH")
    if ue_project_path:
        project_path = Path(ue_project_path)
        # If it's a directory, look for .uproject file
        if project_path.is_dir():
            uproject_files = list(project_path.glob("*.uproject"))
            if uproject_files:
                return str(uproject_files[0])
        # If it's already a .uproject file
        elif project_path.is_file() and project_path.suffix == ".uproject":
            return str(project_path)
        # If it's a path to a project directory
        elif project_path.exists():
            uproject_files = list(project_path.glob("*.uproject"))
            if uproject_files:
                return str(uproject_files[0])
    
    # Priority 2: Default gym_citynav project (for SimWorld-Studio-Minimal)
    default_project = ue_root_path / "gym_citynav" / "gym_citynav.uproject"
    if default_project.exists():
        return str(default_project)
    
    return None


def start_server(args):
    """Start everything: UE binary + Studio web server."""
    pkg_dir = get_package_dir()
    node = find_node()

    print()
    print("=" * 55)
    print("  SimWorld Studio v" + __version__)
    print("=" * 55)

    # Step 1: Check LLM auth
    print()
    auth = check_llm_auth()
    provider = resolve_llm_provider()
    if auth:
        print(f"  [OK] LLM auth: {auth} ({provider})")
    else:
        print("  [!!] LLM provider not authenticated!")
        print("       DeepSeek: set SIMWORLD_LLM_PROVIDER=deepseek and DEEPSEEK_API_KEY")
        print("       Qwen: set SIMWORLD_LLM_PROVIDER=qwen and DASHSCOPE_API_KEY")
        print("       Claude fallback: set ANTHROPIC_API_KEY or run 'claude login'")
        if not args.skip_auth_check:
            sys.exit(1)

    # ── Step 2: Detect GPU ──
    gpus = detect_gpu()
    if gpus:
        print(f"  [OK] GPU: {len(gpus)} detected")
        for g in gpus:
            print(f"       - {g}")
    else:
        print("  [!!] No NVIDIA GPU detected. UE requires a GPU.")
        if not args.skip_gpu_check:
            sys.exit(1)

    gpu_index = args.gpu
    if gpu_index is None and len(gpus) > 1:
        print()
        print(f"  Multiple GPUs detected. Which GPU to use? [0-{len(gpus)-1}]")
        try:
            gpu_index = int(input("  GPU index (default 0): ").strip() or "0")
        except (ValueError, EOFError):
            gpu_index = 0
    elif gpu_index is None:
        gpu_index = 0

    # ── Step 3: Find SimWorld binary or UE installation ──
    binary_dir = find_simworld_binary(args.binary)
    if not binary_dir:
        print("  [!!] UE binary not found!")
        print()
        print("       Option 1: Use local UE installation (recommended)")
        print("       Set environment variables:")
        print("         export UE_ROOT=/path/to/UE_5.3.2")
        print("         export UE_PROJECT_PATH=/path/to/your/project")
        print()
        print("       Option 2: Download SimWorld-Studio-Minimal")
        print("         wget -O SimWorld-Studio-Minimal.tar.gz \\")
        print("             https://huggingface.co/datasets/SimWorld-AI/SimWorld-Studio/resolve/main/SimWorld-Studio-Minimal.tar.gz")
        print("         tar xzf SimWorld-Studio-Minimal.tar.gz")
        sys.exit(1)
    print(f"  [OK] UE Root: {binary_dir}")

    # ── Step 4: Setup workspace ──
    workspace = Path(args.data_dir) if args.data_dir else Path.cwd() / "simworld_studio_workspace"
    workspace = setup_workspace(workspace, pkg_dir)
    generate_mcp_config(workspace, "127.0.0.1", str(args.mcp_port))
    print(f"  [OK] Workspace: {workspace}")

    # ── Step 5: Start Cirrus signaling server ──
    print()
    cirrus_dir = binary_dir / "Engine" / "Plugins" / "Media" / "PixelStreaming" / "Resources" / "WebServers" / "SignallingWebServer"
    cirrus_js = cirrus_dir / "cirrus.js"
    cirrus_proc = None

    # Check if Cirrus is already running on the expected ports
    cirrus_already_running = False
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(1)
        s.connect(("127.0.0.1", args.cirrus_http_port))
        s.close()
        cirrus_already_running = True
    except (ConnectionRefusedError, socket.timeout, OSError):
        pass

    if cirrus_already_running:
        print(f"  [OK] Cirrus already running (HTTP :{args.cirrus_http_port}, WS :{args.cirrus_ws_port})")
    elif cirrus_js.exists():
        # Generate cirrus config
        cirrus_config = {
            "UseFrontend": True,
            "UseMatchmaker": False,
            "HttpPort": args.cirrus_http_port,
            "StreamerPort": args.cirrus_ws_port,
            "SFUPort": args.cirrus_sfu_port,
        }
        cirrus_config_path = workspace / "cirrus-config.json"
        cirrus_config_path.write_text(json.dumps(cirrus_config, indent=2))

        cirrus_log = workspace / "logs" / "cirrus.log"
        cirrus_log_file = open(cirrus_log, "w")
        cirrus_proc = subprocess.Popen(
            [node, str(cirrus_js), f"--configFile={cirrus_config_path}"],
            cwd=str(cirrus_dir),
            stdout=cirrus_log_file,
            stderr=subprocess.STDOUT,
        )
        time.sleep(2)
        if cirrus_proc.poll() is None:
            print(f"  [OK] Cirrus signaling server (HTTP :{args.cirrus_http_port}, WS :{args.cirrus_ws_port})")
        else:
            print("  [!!] Cirrus failed to start — Pixel Streaming may not work")
            cirrus_proc = None
    else:
        print("  [!!] Cirrus not found — Pixel Streaming may not work")

    # ── Step 6: Launch UE ──
    print("  Launching Unreal Engine (headless)...")
    ue_editor = str(binary_dir / "Engine" / "Binaries" / "Linux" / "UnrealEditor")
    
    # Find project file (supports local UE_PROJECT_PATH or default gym_citynav)
    project_file = find_ue_project(binary_dir)
    if not project_file:
        print("  [!!] UE project file not found!")
        print("       Set UE_PROJECT_PATH environment variable:")
        print("         export UE_PROJECT_PATH=/path/to/your/project")
        print("       Or ensure gym_citynav/gym_citynav.uproject exists in SimWorld-Studio-Minimal")
        sys.exit(1)
    print(f"  [OK] Project: {project_file}")

    ue_env = os.environ.copy()
    ue_env["CUDA_VISIBLE_DEVICES"] = str(gpu_index)
    nvidia_icd = "/usr/share/vulkan/icd.d/nvidia_icd.json"
    if os.path.isfile(nvidia_icd):
        ue_env["VK_ICD_FILENAMES"] = nvidia_icd

    ue_log = workspace / "logs" / "ue.log"

    ue_map = normalize_map_path(args.map)

    ue_cmd = [
        ue_editor, project_file,
        ue_map,
        f"-MCPPort={args.mcp_port}",
        "-Unattended", "-NOSPLASH", "-NOSOUND", "-Messaging",
        "-ResX=1280", "-ResY=720",
        "-FPSMAX=15",
        f"-graphicsadapter={gpu_index}",
        "-RenderOffScreen",
        # Pixel Streaming via Cirrus signaling server
        "-EditorPixelStreamingRes=1280x720",
        "-EditorPixelStreamingStartOnLaunch=true",
        "-EditorPixelStreamingUseRemoteSignallingServer=true",
        f"-PixelStreamingURL=ws://127.0.0.1:{args.cirrus_ws_port}",
        "-log",
    ]

    ue_log_file = open(ue_log, "w")
    ue_proc = subprocess.Popen(
        ue_cmd,
        env=ue_env,
        stdout=ue_log_file,
        stderr=subprocess.STDOUT,
    )
    print(f"  UE PID: {ue_proc.pid} (log: {ue_log})")
    print(f"  Map: {ue_map}")

    # ── Step 6: Wait for MCP port ──
    print(f"  Waiting for MCP port {args.mcp_port}...", end="", flush=True)
    if wait_for_port(args.mcp_port, timeout=120):
        print(" ready!")
    else:
        print(" TIMEOUT!")
        print(f"  UE may have crashed. Check log: {ue_log}")
        ue_proc.terminate()
        sys.exit(1)

    # ── Step 7: Start web server ──
    env = os.environ.copy()
    env["PORT"] = str(args.port)
    env["UNREAL_HOST"] = "127.0.0.1"
    env["UNREAL_PORT"] = str(args.mcp_port)
    env["PIXEL_STREAMING_URL"] = f"http://127.0.0.1:{args.cirrus_http_port}"
    env["CIRRUS_HTTP_PORT"] = str(args.cirrus_http_port)
    env["CIRRUS_WS_PORT"] = str(args.cirrus_ws_port)
    
    # Mock mode
    if args.mock:
        env["MOCK_MODE"] = "1"
        if args.mock_file:
            # If provided, use it (could be relative or absolute)
            mock_file = args.mock_file if os.path.isabs(args.mock_file) else str(workspace / args.mock_file)
        else:
            # Default to workspace/mock_responses.txt
            mock_file = str(workspace / "mock_responses.txt")
        # Always use absolute path
        env["MOCK_FILE"] = os.path.abspath(mock_file)
        print(f"  [MOCK] Mock mode enabled, using file: {env['MOCK_FILE']}")

    entry = str(workspace / "web" / "server" / "index.js")
    
    # Patch index.js for mock mode if enabled
    if args.mock:
        patch_script = str(workspace / "web" / "server" / "patch-mock-mode.js")
        if os.path.exists(patch_script):
            print("  [MOCK] Patching index.js for mock mode...")
            subprocess.run([node, patch_script], cwd=str(workspace / "web" / "server"), check=False)

    server_proc = subprocess.Popen(
        [node, entry],
        cwd=str(workspace / "web"),
        env=env,
    )

    # ── Step 8: Print access info ──
    server_ip = get_server_ip()
    is_local = is_local_machine()

    print()
    print("=" * 55)
    print("  SimWorld Studio is running!")
    print()
    if is_local:
        print(f"  Open: http://localhost:{args.port}")
    else:
        print(f"  Local access:  http://localhost:{args.port}")
        print(f"  Remote access: http://{server_ip}:{args.port}")
        print()
        print(f"  Or use SSH tunnel from your laptop:")
        print(f"    ssh -L {args.port}:localhost:{args.port} -L {args.cirrus_http_port}:localhost:{args.cirrus_http_port} user@{server_ip}")
        print(f"    Then open: http://localhost:{args.port}")
    print()
    print(f"  GPU: {gpu_index}  |  MCP: {args.mcp_port}  |  Web: {args.port}  |  Cirrus: HTTP:{args.cirrus_http_port} WS:{args.cirrus_ws_port} SFU:{args.cirrus_sfu_port}")
    print("=" * 55)
    print()
    print('  Try: "Set up a sunset scene with 4 houses and trees"')
    print()
    print("  Press Ctrl+C to stop.")
    print()

    # ── Handle shutdown ──
    def shutdown(sig=None, frame=None):
        print("\n  Shutting down...")
        server_proc.terminate()
        ue_proc.terminate()
        if cirrus_proc:
            cirrus_proc.terminate()
        try:
            server_proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            server_proc.kill()
        try:
            ue_proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            ue_proc.kill()
        if cirrus_proc:
            try:
                cirrus_proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                cirrus_proc.kill()
        ue_log_file.close()
        print("  Done.")
        sys.exit(0)

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    # Wait for either process to exit
    try:
        while True:
            # Check if either process died
            if ue_proc.poll() is not None:
                print(f"\n  [!!] UE exited with code {ue_proc.returncode}")
                print(f"       Check log: {ue_log}")
                server_proc.terminate()
                sys.exit(1)
            if server_proc.poll() is not None:
                print(f"\n  [!!] Web server exited with code {server_proc.returncode}")
                ue_proc.terminate()
                sys.exit(1)
            time.sleep(2)
    except KeyboardInterrupt:
        shutdown()


def main():
    parser = argparse.ArgumentParser(
        prog="simworld-studio",
        description="SimWorld Studio — AI-powered 3D scene generation",
    )
    subparsers = parser.add_subparsers(dest="command")

    sp_start = subparsers.add_parser("start", help="Launch SimWorld Studio (UE + web server)")
    sp_start.add_argument("--port", type=int, default=3002, help="Web UI port (default: 3002)")
    sp_start.add_argument("--gpu", type=int, default=None, help="GPU index (auto-detected if omitted)")
    sp_start.add_argument("--mcp-port", type=int, default=55560, help="UE MCP port (default: 55560)")
    sp_start.add_argument("--cirrus-http-port", type=int, default=8585, help="Cirrus HTTP port for Pixel Streaming (default: 8585)")
    sp_start.add_argument("--cirrus-ws-port", type=int, default=8586, help="Cirrus WebSocket port for Pixel Streaming (default: 8586)")
    sp_start.add_argument("--cirrus-sfu-port", type=int, default=8889, help="Cirrus SFU port for Pixel Streaming (default: 8889)")
    sp_start.add_argument("--map", default="/Game/Main", help="UE map path to open (default: /Game/Main)")
    sp_start.add_argument("--binary", default=None, help="Path to UE installation or SimWorld-Studio-Minimal directory (overrides UE_ROOT env var)")
    sp_start.add_argument("--data-dir", default=None, help="Workspace directory")
    sp_start.add_argument("--skip-auth-check", action="store_true", help="Skip LLM auth check")
    sp_start.add_argument("--skip-gpu-check", action="store_true", help="Skip GPU check")
    sp_start.add_argument("--mock", action="store_true", help="Enable mock mode (use mock_responses.txt instead of calling the LLM)")
    sp_start.add_argument("--mock-file", default=None, help="Path to mock responses file (default: workspace/mock_responses.txt)")

    subparsers.add_parser("version", help="Show version")

    args = parser.parse_args()

    if args.command == "start":
        start_server(args)
    elif args.command == "version":
        print(f"simworld-studio v{__version__}")
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
