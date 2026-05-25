# SimWorld Studio — Static Scene Evaluation

Standalone evaluation harness for benchmarking coding agents on 3D scene generation in Unreal Engine 5. Supports multiple LLM backends (Anthropic API, open-source via vLLM), three evaluation settings, and an optional self-correction verification loop.

## Overview

This evaluation framework measures how well a coding agent can build 3D city scenes using MCP (Model Context Protocol) tools connected to Unreal Engine 5. The agent receives a text prompt, calls MCP tools to spawn buildings/trees/props, and the resulting scene is evaluated with rule-based metrics and VLM-as-Judge scoring.

### Three Evaluation Settings

| Setting | Input | Metrics |
|---------|-------|---------|
| **S1: Text-to-Scene** | Text prompt describing a scene | CNT, DIV, 1-COL, GRAV, OOB, PF, SRF, LAES |
| **S2: Image+Text-to-Scene** | Reference image + text | CNT, 1-COL, GRAV, ILC, PF, STY |
| **S3: Scene Editing** | Existing scene + edit instruction | PRES, ECNT, 1-COL, EC, SC, LQ |

Each setting has 3 difficulty levels: **easy**, **mid**, **hard** (9 scenes total per agent).

### Verification Loop

When `--verify` is enabled, the agent calls a `verify()` MCP tool after building the scene. This tool:
1. Takes a screenshot of the current viewport
2. Extracts the scene graph (all actors + positions)
3. Runs rule-based metrics: collision detection, grounding check, bounds check, object count validation
4. Returns structured feedback with specific issues

The agent (acting as its own VLM judge) reads the screenshot, reviews the metrics, fixes issues, and calls `verify()` again until all checks pass.

## Prerequisites

### 1. Unreal Engine 5.3.2 with UnrealMCP Plugin

You need a running UE Editor with the UnrealMCP TCP plugin. SimWorld Studio provides this.

```bash
# Download and extract SimWorld Studio binary
tar xzf SimWorld-Studio-Minimal.tar.gz

# Start UE (headless, offscreen rendering)
./UE_5.3.2/Engine/Binaries/Linux/UnrealEditor \
  ./gym_citynav.uproject /Game/Maps/Empty.umap \
  -MCPPort=55560 -Unattended -NOSPLASH -NOSOUND \
  -ResX=1280 -ResY=720 -FPSMAX=15 -RenderOffScreen -log &

# Wait for UE to start (~60s), then verify:
python3 -c "import socket; s=socket.socket(); s.settimeout(3); s.connect(('127.0.0.1', 55560)); print('OK'); s.close()"
```

### 2. Claude Code CLI

Install [Claude Code](https://docs.anthropic.com/en/docs/claude-code):

```bash
npm install -g @anthropic-ai/claude-code
```

For Anthropic models (Sonnet/Opus/Haiku), you need an Anthropic API key or `claude login`.

### 3. MCP Server Configuration

Copy and edit the MCP config:

```bash
cp mcp.json.example mcp.json
# Edit mcp.json: set the correct path to mcp-server.js and assets.json
```

### 4. (Optional) vLLM Server for Open-Source Models

To use open-source models (Qwen, etc.) as the coding agent's backbone:

#### Option A: Use existing shared servers

If someone is already hosting vLLM servers:

| Model | URL | Note |
|-------|-----|------|
| Qwen3.5-27B | `http://132.239.95.133:8001` | |
| Qwen3.5-9B | `http://132.239.95.133:8002` | |
| Qwen3.5-2B | `http://132.239.95.133:8003` | |

Use `--vllm-url` to point to these:

```bash
python run_eval.py --agent claude-qwen3.5-9b --vllm-url http://132.239.95.133:8002
```

#### Option B: Host your own vLLM server

```bash
# Install vLLM
pip install vllm

# Start vLLM with Anthropic API + tool support
CUDA_VISIBLE_DEVICES=0 vllm serve Qwen/Qwen3.5-9B \
  --port 30001 \
  --host 0.0.0.0 \
  --served-model-name Qwen3.5-9B \
  --enable-auto-tool-choice \
  --tool-call-parser pythonic \
  --gpu-memory-utilization 0.9 \
  --max-model-len 65536

# Verify it's running:
curl http://localhost:30001/v1/models
```

**Important vLLM flags:**
- `--enable-auto-tool-choice` — Required for the model to use MCP tools
- `--tool-call-parser pythonic` — Parses Qwen's tool call format
- `--max-model-len 65536` — Claude Code requests up to 32k output tokens; model context must be larger

The vLLM server must support the **Anthropic Messages API** (vLLM 0.6+ does this natively at `/v1/messages`).

## Quick Start

```bash
# List available agents
python run_eval.py --list-agents

# Run a single scene (S1-easy) with Claude Sonnet
python run_eval.py --agent claude-sonnet --setting s1 --difficulty easy

# Run with Qwen3.5-9B via local vLLM
python run_eval.py --agent claude-qwen3.5-9b --setting s1 --difficulty easy

# Run with background map for better visuals
python run_eval.py --agent claude-sonnet --background-map TestMap4

# Run with verification loop
python run_eval.py --agent claude-sonnet --verify

# Run full experiment (all 9 scenes)
python run_eval.py --agent claude-sonnet

# Compare multiple agents
python run_eval.py --agent claude-sonnet --agent claude-qwen3.5-9b
```

## Output Structure

Each run creates a structured output directory:

```
results/run_20260416_143000/
├── summary.json                      # Combined results for all agents
├── claude-sonnet/
│   ├── s1_easy/
│   │   ├── raw_stream.jsonl          # Raw Claude Code stream-json output
│   │   ├── chat_history.json         # Parsed: model, tool calls, text responses
│   │   ├── agent_metadata.json       # Timing, cost, tool count, model info
│   │   ├── prompt_info.json          # Setting, difficulty, prompt text
│   │   ├── result.json               # Generation + evaluation results
│   │   └── export/
│   │       ├── aerial_ne.png         # Screenshot (aerial northeast)
│   │       ├── aerial_sw.png         # Screenshot (aerial southwest)
│   │       ├── overview.png          # Screenshot (overview)
│   │       ├── street_front.png      # Screenshot (street level)
│   │       ├── street_back.png       # Screenshot (street back)
│   │       ├── street_side.png       # Screenshot (street side)
│   │       ├── scene_graph.json      # All actors + positions
│   │       └── scene.umap            # UE map file (loadable)
│   ├── s1_mid/
│   │   └── ...
│   └── s1_hard/
│       └── ...
└── claude-qwen3.5-9b/
    └── ...
```

### Chat History Format

`chat_history.json` contains the full conversation:

```json
{
  "agent": "claude-sonnet",
  "model": "sonnet",
  "display_name": "Claude Code + Sonnet 4",
  "vllm_url": "anthropic-api",
  "timestamp": "2026-04-16T02:50:28Z",
  "messages": [
    {
      "role": "assistant",
      "content": [
        {"type": "text", "text": "I'll start by setting up the environment..."},
        {"type": "tool_use", "name": "mcp__simworld__setup_environment", "input": {"background_map": "TestMap4"}}
      ]
    },
    {
      "role": "user",
      "content": [
        {"type": "tool_result", "content": "{\"status\": \"success\", ...}"}
      ]
    }
  ]
}
```

## Customizing Prompts

Edit the `PROMPTS` dict in `run_eval.py` to change scene descriptions:

```python
PROMPTS = {
    "s1": {
        "easy": "Your custom easy prompt here",
        "mid": "Your custom medium prompt here",
        "hard": "Your custom hard prompt here",
    },
    ...
}
```

## Adding New Agents

Add a new entry to the `AGENTS` dict:

```python
AGENTS["my-custom-agent"] = {
    "env": {
        "ANTHROPIC_BASE_URL": "http://my-vllm-server:8000",
        "ANTHROPIC_API_KEY": "dummy",
        "ANTHROPIC_AUTH_TOKEN": "dummy",
        "ANTHROPIC_DEFAULT_SONNET_MODEL": "my-model-name",
        "ANTHROPIC_DEFAULT_OPUS_MODEL": "my-model-name",
        "ANTHROPIC_DEFAULT_HAIKU_MODEL": "my-model-name",
    },
    "model": "sonnet",
    "extra_args": ["--bare"],
    "display": "Claude Code + My Model",
}
```

## Metrics Reference

### Rule-Based Metrics (from scene graph)

| Metric | Full Name | Description |
|--------|-----------|-------------|
| CNT | Object Count Accuracy | Fraction of requested object categories with correct counts |
| DIV | Asset Type Diversity | Distinct asset classes / total actors |
| 1-COL | Collision-Free Rate | 1 - (overlapping pairs / total pairs), AABB approximation |
| GRAV | Grounding Validity | Fraction of actors within Z=[-200, 200] of ground |
| OOB | In-Bounds Rate | Fraction of actors within X,Y=[-9500, 9500] |
| PRES | Preservation (S3) | Fraction of pre-edit actors preserved post-edit |
| ECNT | Edit Count (S3) | New actors / expected new actors from edit instruction |

### VLM-as-Judge Metrics (scored 0-10, normalized to 0-1)

| Metric | Full Name | Settings | Description |
|--------|-----------|----------|-------------|
| PF | Prompt Fidelity | S1, S2 | Does scene match the prompt? |
| SRF | Spatial Relationship Fidelity | S1 | Are spatial relationships correct? |
| LAES | Layout Aesthetics | S1 | Does it look like a real place? |
| ILC | Image Layout Correspondence | S2 | Does scene match reference image? |
| STY | Style Consistency | S2 | Do scales/density match reference? |
| EC | Edit Completeness | S3 | Was the edit fully applied? |
| SC | Scene Coherence | S3 | Does post-edit scene remain coherent? |
| LQ | Layout Quality | S3 | Are new objects logically placed? |

## Troubleshooting

### UE crashes during map loading
The `background_map` parameter loads a pre-built map. If UE crashes, restart it and try without the background map.

### vLLM returns 500 errors
- Check that `--max-model-len` is large enough (65536 recommended)
- Check that `--enable-auto-tool-choice` is set
- Check vLLM log: `tail -f /tmp/vllm_*.log`

### Model generates tool calls in text but doesn't execute them
This happens when vLLM's Anthropic API doesn't properly format tool_use blocks. Ensure `--tool-call-parser pythonic` is set. The `tool_count` in metadata still counts XML-formatted tool calls in text.

### Claude Code hangs / timeout
Usually means UE crashed or the MCP server can't connect. Check: `python3 -c "import socket; s=socket.socket(); s.settimeout(3); s.connect(('127.0.0.1', 55560)); print('OK')"`

## License

Apache 2.0
