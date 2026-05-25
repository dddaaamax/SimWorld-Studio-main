---
name: simworld-mcp
description: Use when implementing SimWorld Studio MCP tools, UE worker adapter, verifier integration, tool safety gates, or any backend code in simworld_studio_workspace/web/server/.
---

# SimWorld MCP Skill

## Before starting any backend/MCP task:

1. Read `AGENTS.md` (repo root)
2. Read `docs/product/architecture.md`
3. Read `docs/product/api_contract.md`

## Key files:

```
simworld_studio_workspace/web/server/
  index.js          — Express API + SSE poller
  mcp-server.js     — MCP protocol server (TCP 55557)
  unreal-bridge.js  — UE TCP client
  agents.js         — agent session management
  skills.js         — skill store
  scenes.js         — scene persistence
```

## Safety rules:

1. **Never let the frontend call UE tools directly** — all tool calls go through `/api/` → server → MCP
2. **Dangerous tools require explicit approval** (user must confirm before execution):
   - `execute_python_script` — arbitrary Python in UE context
   - `delete_all_spawned` — clears entire scene
   - Any filesystem write beyond the `simworld_studio_workspace/` directory
   - Any external network call beyond the configured UE host

3. **Every tool call must produce an audit event** — log tool name, parameters (sanitized), result status, timestamp
4. **Every tool response must be structured JSON** — no raw string responses from tools

## Safe tools (always available):

```
list_assets               — catalog UE assets
take_screenshot           — capture viewport as PNG
get_actors_in_level       — list all actors
find_actors_by_name       — search actors by pattern
set_actor_transform       — move/rotate/scale actor
spawn_actor               — spawn static mesh
spawn_blueprint_actor     — spawn Blueprint class
delete_actor              — delete single actor by name
setup_environment         — set up lighting/sky/ground
verify_scene              — VLM + rule verification
get_agent_state           — query embodied agent state
agent_action              — send action to agent
agent_rotate              — rotate agent
agent_stop                — stop agent
```

## Restricted tools (require approval):

```
execute_python_script     — RESTRICTED: explicit approval + audit log required
delete_all_spawned        — RESTRICTED: explicit approval required
```

## Adding a new tool:

1. Add tool schema to the MCP server (`mcp-server.js`) following existing patterns
2. Add tool handler with input validation and error handling
3. Add tool to the safe or restricted list above
4. Add tool to `.codex/config.toml` `enabled_tools` or `disabled_tools`
5. Emit an audit event on every call:
   ```js
   emitEvent('tool_call', { tool: name, params: sanitized, timestamp: Date.now() })
   ```

## Event stream requirements:

When adding new backend features, emit appropriate SSE events:
```
run.started          → { runId, type, params }
run.status_changed   → { runId, status }
tool_call.started    → { toolName, params }
tool_call.completed  → { toolName, result }
tool_call.failed     → { toolName, error }
scene.updated        → { actorCount, environment }
verifier.completed   → { report }
```

## Adding a new API endpoint:

1. Add route to `server/index.js` following existing REST patterns
2. Add schema to `docs/product/api_contract.md`
3. Return structured JSON always:
   ```js
   res.json({ success: true, data: {...} })
   // or on error:
   res.status(400).json({ error: "message", code: "ERROR_CODE" })
   ```
4. Add input validation — never trust raw request body

## Before finishing any backend task:

```bash
# Start the server and verify no crash on startup
node simworld_studio_workspace/web/server/index.js
# Ctrl+C after it prints "Server running on port..."

# If you added new API endpoints, test them:
curl http://localhost:9001/api/health
```

## Local UE worker integration:

The UE worker runs locally on Windows (not in cloud).  
Do not attempt to auto-start UE from code.  
All UE communication is via TCP socket on port 55557.  
If UE is not running, `health.ueConnected` will be `false` — this is normal.

For testing without UE, use mock responses in the MCP server bridge:
```js
// In unreal-bridge.js, add a MOCK_MODE env flag:
if (process.env.MOCK_UE === "1") {
  return { success: true, data: MOCK_RESPONSES[toolName] || {} };
}
```
