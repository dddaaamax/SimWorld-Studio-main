# DeepSeek/Qwen Colab Implementation Plan

This document describes the runnable SimWorld Studio setup for Google Colab using
DeepSeek, Qwen/DashScope, or another OpenAI-compatible model.

## Goal

Provide a practical student-friendly workflow that can run in Colab:

1. Start the SimWorld Minimal UE package.
2. Start SimWorld Studio.
3. Use DeepSeek or Qwen for the SimCoder chat/tool loop.
4. Generate a UE scene from natural language.
5. Export four pipeline artifacts:
   - `Scene`
   - `TaskSet`
   - `TrainingRun`
   - `CurriculumRun`

The current implementation makes Scene Generation real through UE/MCP. The later
three artifacts are generated as structured research artifacts in Colab so the
full pipeline can be exercised end to end. Real NavMesh-based task sampling,
real Gym export, and real RL training still require backend extensions.

## Model Providers

The Studio backend uses `server/llm-chat.js` for non-Claude providers. It sends
OpenAI-compatible Chat Completions requests and maps model tool calls to the
existing SimWorld MCP tools.

Supported provider environment variables:

```bash
# DeepSeek
export SIMWORLD_LLM_PROVIDER=deepseek
export SIMWORLD_LLM_MODEL=deepseek-chat
export DEEPSEEK_API_KEY=...

# Qwen / DashScope
export SIMWORLD_LLM_PROVIDER=qwen
export SIMWORLD_LLM_MODEL=qwen-plus
export DASHSCOPE_API_KEY=...

# Generic OpenAI-compatible provider
export SIMWORLD_LLM_PROVIDER=openai
export SIMWORLD_LLM_BASE_URL=https://your-provider.example/v1
export SIMWORLD_LLM_MODEL=your-model
export SIMWORLD_LLM_API_KEY=...
```

Default endpoints:

```text
DeepSeek: https://api.deepseek.com/chat/completions
Qwen:     https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions
```

## Colab Workflow

Use `SimWorld_Studio.ipynb` and run the cells in order.

1. GPU check.
2. Download and extract `SimWorld-Studio-Minimal.tar.gz`.
3. Install the Studio Python package from the configured public GitHub repo.
4. Choose the LLM provider: `deepseek` or `qwen`.
5. Launch UE + Cirrus + Studio backend.
6. Create a public tunnel.
7. Verify health, assets, backend, and provider.
8. Run an end-to-end smoke test.
9. Run the full artifact pipeline cell.

The final cell writes artifacts to:

```text
/content/studio_workspace/pipeline_outputs/<timestamp>/
  scene_result.json
  taskset_pointnav.json
  training_run.json
  curriculum_run.json
  summary.json
```

## What Is Real Today

These parts are real and connected to UE when the MCP port is available:

- Natural language scene generation.
- Model tool calls.
- SimWorld MCP tools.
- Actor spawning and transforms.
- Environment setup.
- Screenshot capture.
- Scene metadata save/load.
- Pixel Streaming viewport.
- Backend health checks.

## What Is Artifact-Level Today

These parts are represented as JSON artifacts in the Colab pipeline runner:

- TaskSet generation.
- TrainingRun metrics.
- CurriculumRun adaptation decisions.

They are useful for learning the product flow and preparing research outputs,
but they are not yet a replacement for a real navigation/RL stack.

## Required Future Backend Work

To make every mode fully real, the project still needs:

1. A safe `save_map` MCP tool for writing `.umap` outputs.
2. A `query_navmesh` MCP tool for reachable point sampling.
3. A task generation service for PointNav/ObjectNav episodes.
4. A Gym/Gymnasium export format.
5. A rollout executor for RGB-D observations and agent actions.
6. Metric computation for SR, SPL, SoftSPL, and nDTW.
7. A co-evolution orchestrator that feeds agent failures back to SimCoder.

## Practical Recommendation

For a student project, start with this deliverable:

```text
DeepSeek/Qwen prompt -> UE scene -> screenshot -> scene artifact -> task/training/curriculum JSON artifacts
```

After that works reliably, extend the system one backend capability at a time,
starting with NavMesh query and `.umap` saving.
