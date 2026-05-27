"use strict";

const fs = require("fs");
const path = require("path");
const { spawn } = require("child_process");

const DEFAULTS = {
  deepseek: {
    baseUrl: "https://api.deepseek.com",
    model: "deepseek-chat",
    apiKeyEnv: "DEEPSEEK_API_KEY",
  },
  qwen: {
    baseUrl: "https://dashscope.aliyuncs.com/compatible-mode/v1",
    model: "qwen-plus",
    apiKeyEnv: "DASHSCOPE_API_KEY",
  },
  openai: {
    baseUrl: "https://api.openai.com/v1",
    model: "gpt-4o-mini",
    apiKeyEnv: "OPENAI_API_KEY",
  },
};

const SESSION_MESSAGES = new Map();
const MAX_SESSION_MESSAGES = 24;
const MAX_TOOL_RESULT_CHARS = 20000;

function normalizeProvider(raw) {
  const provider = String(raw || "").trim().toLowerCase();
  if (provider === "anthropic" || provider === "claude-code") return "claude";
  if (provider === "dashscope" || provider === "aliyun" || provider === "qianwen") return "qwen";
  if (provider === "custom") return "openai";
  return provider;
}

function resolveLlmProvider(env = process.env) {
  let provider = normalizeProvider(
    env.SIMWORLD_LLM_PROVIDER ||
    env.LLM_PROVIDER ||
    env.AI_PROVIDER ||
    ""
  );

  if (!provider) {
    if (env.DEEPSEEK_API_KEY) provider = "deepseek";
    else if (env.DASHSCOPE_API_KEY || env.QWEN_API_KEY) provider = "qwen";
    else if (env.SIMWORLD_LLM_API_KEY || env.LLM_API_KEY || env.OPENAI_API_KEY) provider = "openai";
    else provider = "claude";
  }

  if (provider === "claude") return { provider, isClaude: true };

  const defaults = DEFAULTS[provider] || DEFAULTS.openai;
  const baseUrl = String(
    env.SIMWORLD_LLM_BASE_URL ||
    env.LLM_BASE_URL ||
    env.OPENAI_BASE_URL ||
    defaults.baseUrl
  ).replace(/\/+$/, "");

  const model = String(
    env.SIMWORLD_LLM_MODEL ||
    env.LLM_MODEL ||
    env.OPENAI_MODEL ||
    (provider === "deepseek" ? env.DEEPSEEK_MODEL : "") ||
    (provider === "qwen" ? (env.QWEN_MODEL || env.DASHSCOPE_MODEL) : "") ||
    defaults.model
  );

  const apiKey = String(
    env.SIMWORLD_LLM_API_KEY ||
    env.LLM_API_KEY ||
    (provider === "deepseek" ? env.DEEPSEEK_API_KEY : "") ||
    (provider === "qwen" ? (env.DASHSCOPE_API_KEY || env.QWEN_API_KEY) : "") ||
    env.OPENAI_API_KEY ||
    ""
  );

  return { provider, baseUrl, model, apiKey, isClaude: false };
}

function shouldUseOpenAiCompatible(env = process.env) {
  return !resolveLlmProvider(env).isClaude;
}

function chatCompletionsUrl(baseUrl) {
  return `${baseUrl.replace(/\/+$/, "")}/chat/completions`;
}

async function postChatCompletion(config, payload) {
  if (typeof fetch !== "function") {
    throw new Error("Node.js global fetch is unavailable. Use Node.js 18+ for OpenAI-compatible providers.");
  }
  if (!config.apiKey) {
    throw new Error(
      `Missing API key for provider '${config.provider}'. Set SIMWORLD_LLM_API_KEY, ` +
      (config.provider === "deepseek" ? "DEEPSEEK_API_KEY" :
        config.provider === "qwen" ? "DASHSCOPE_API_KEY or QWEN_API_KEY" : "OPENAI_API_KEY") +
      "."
    );
  }

  const res = await fetch(chatCompletionsUrl(config.baseUrl), {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      Authorization: `Bearer ${config.apiKey}`,
    },
    body: JSON.stringify(payload),
  });

  const text = await res.text();
  let data = null;
  try { data = JSON.parse(text); } catch {}
  if (!res.ok) {
    const detail = data?.error?.message || data?.message || text.slice(0, 800);
    throw new Error(`${config.provider} chat completion failed (${res.status}): ${detail}`);
  }
  if (!data) throw new Error(`${config.provider} returned non-JSON response: ${text.slice(0, 200)}`);
  return data;
}

class McpClient {
  constructor(options = {}) {
    this.serverPath = options.serverPath || path.join(__dirname, "mcp-server.js");
    this.cwd = options.cwd || path.dirname(this.serverPath);
    this.env = options.env || process.env;
    this.log = options.log || (() => {});
    this.proc = null;
    this.nextId = 1;
    this.pending = new Map();
    this.stdoutBuffer = "";
  }

  async start() {
    this.proc = spawn(process.execPath, [this.serverPath], {
      cwd: this.cwd,
      env: this.env,
      stdio: ["pipe", "pipe", "pipe"],
    });

    this.proc.stdout.on("data", (chunk) => this._onStdout(chunk));
    this.proc.stderr.on("data", (chunk) => {
      const msg = chunk.toString().trim();
      if (msg) this.log("mcp", msg.slice(0, 500));
    });
    this.proc.on("close", (code) => {
      for (const { reject, timer } of this.pending.values()) {
        clearTimeout(timer);
        reject(new Error(`MCP server exited with code ${code}`));
      }
      this.pending.clear();
    });

    await this.request("initialize", {
      protocolVersion: "2024-11-05",
      capabilities: {},
      clientInfo: { name: "simworld-openai-compatible", version: "1.0.0" },
    });
    this.notify("notifications/initialized", {});
  }

  _onStdout(chunk) {
    this.stdoutBuffer += chunk.toString();
    const lines = this.stdoutBuffer.split("\n");
    this.stdoutBuffer = lines.pop() || "";
    for (const line of lines) {
      if (!line.trim()) continue;
      let msg;
      try { msg = JSON.parse(line); } catch {
        this.log("mcp", `Invalid JSON from MCP: ${line.slice(0, 200)}`);
        continue;
      }
      if (msg.id === undefined) continue;
      const pending = this.pending.get(msg.id);
      if (!pending) continue;
      clearTimeout(pending.timer);
      this.pending.delete(msg.id);
      if (msg.error) pending.reject(new Error(msg.error.message || "MCP error"));
      else pending.resolve(msg.result);
    }
  }

  notify(method, params) {
    this._write({ jsonrpc: "2.0", method, params });
  }

  request(method, params, timeoutMs = 120000) {
    const id = this.nextId++;
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => {
        this.pending.delete(id);
        reject(new Error(`MCP request timed out: ${method}`));
      }, timeoutMs);
      this.pending.set(id, { resolve, reject, timer });
      this._write({ jsonrpc: "2.0", id, method, params });
    });
  }

  _write(message) {
    if (!this.proc || !this.proc.stdin.writable) {
      throw new Error("MCP server is not running");
    }
    this.proc.stdin.write(`${JSON.stringify(message)}\n`);
  }

  async listTools() {
    const result = await this.request("tools/list", {});
    return Array.isArray(result?.tools) ? result.tools : [];
  }

  callTool(name, args) {
    return this.request("tools/call", { name, arguments: args || {} }, 180000);
  }

  close() {
    try { this.proc?.kill("SIGTERM"); } catch {}
  }
}

function toolAllowedForMessage(toolName, message) {
  if (toolName === "execute_python_script") return false;
  if (toolName === "verify_scene" && process.env.SIMWORLD_ENABLE_VLM_VERIFY !== "1") return false;
  if (toolName !== "delete_all_spawned") return true;
  return /clear|reset|rebuild|start over|from scratch|清空|重置|重新生成|重建|从头|删除.*全部|删除.*所有/i.test(message || "");
}

function toOpenAiTool(tool) {
  return {
    type: "function",
    function: {
      name: tool.name,
      description: tool.description || "",
      parameters: tool.inputSchema || { type: "object", properties: {} },
    },
  };
}

function normalizeContent(content) {
  if (!content) return "";
  if (typeof content === "string") return content;
  if (Array.isArray(content)) {
    return content.map((part) => {
      if (typeof part === "string") return part;
      return part.text || part.content || "";
    }).join("");
  }
  return String(content);
}

function parseToolArgs(raw) {
  if (!raw) return {};
  if (typeof raw === "object") return raw;
  try { return JSON.parse(raw); } catch {
    return {};
  }
}

function stringifyToolResult(result) {
  const textParts = [];
  if (Array.isArray(result?.content)) {
    for (const part of result.content) {
      if (part?.type === "text") textParts.push(part.text || "");
      else if (part) textParts.push(JSON.stringify(part));
    }
  }
  const text = textParts.length ? textParts.join("\n") : JSON.stringify(result || {});
  return text.length > MAX_TOOL_RESULT_CHARS ? `${text.slice(0, MAX_TOOL_RESULT_CHARS)}\n...[truncated]` : text;
}

function collectPngPaths(value, out = []) {
  if (!value) return out;
  if (typeof value === "string") {
    const matches = value.match(/[A-Za-z]:\\[^"\n\r]+?\.png|\/[^"\n\r\s]+?\.png/g);
    if (matches) out.push(...matches);
    return out;
  }
  if (Array.isArray(value)) {
    for (const item of value) collectPngPaths(item, out);
    return out;
  }
  if (typeof value === "object") {
    for (const item of Object.values(value)) collectPngPaths(item, out);
  }
  return out;
}

function screenshotUrlFromToolResult(resultText) {
  const candidates = [];
  try { collectPngPaths(JSON.parse(resultText), candidates); } catch {
    collectPngPaths(resultText, candidates);
  }
  for (const candidate of candidates) {
    const cleaned = candidate.replace(/\\\\/g, "\\");
    if (fs.existsSync(cleaned)) {
      return `/api/screenshot/file?path=${encodeURIComponent(cleaned)}`;
    }
  }
  return null;
}

function latestScreenshotUrl(screenshotDir) {
  if (!screenshotDir || !fs.existsSync(screenshotDir)) return null;
  try {
    const latest = fs.readdirSync(screenshotDir)
      .filter((name) => name.endsWith(".png"))
      .map((name) => {
        const fp = path.join(screenshotDir, name);
        return { fp, time: fs.statSync(fp).mtimeMs };
      })
      .filter(({ time }) => Date.now() - time < 1800000)
      .sort((a, b) => b.time - a.time)[0];
    return latest ? `/api/screenshot/file?path=${encodeURIComponent(latest.fp)}` : null;
  } catch {
    return null;
  }
}

function trimSessionMessages(messages) {
  if (messages.length <= MAX_SESSION_MESSAGES) return messages;
  return messages.slice(messages.length - MAX_SESSION_MESSAGES);
}

function storeTextSessionTurn(sessionId, previous, userContent, assistantContent) {
  const safePrevious = previous.filter((msg) => {
    return msg.role === "user" || (msg.role === "assistant" && !msg.tool_calls);
  });
  SESSION_MESSAGES.set(sessionId, trimSessionMessages([
    ...safePrevious,
    { role: "user", content: userContent },
    { role: "assistant", content: assistantContent || "" },
  ]));
}

async function runOpenAiCompatibleChat(options) {
  const {
    message,
    sessionId,
    systemPrompt,
    emit,
    res,
    keepAlive,
    logToFile = () => {},
    screenshotDir,
    mcpServerPath = path.join(__dirname, "mcp-server.js"),
    mcpCwd = path.dirname(mcpServerPath),
    env = process.env,
  } = options;

  const config = resolveLlmProvider(env);
  if (config.isClaude) throw new Error("runOpenAiCompatibleChat called for Claude provider");

  const activeSessionId = sessionId || `${config.provider}_${Date.now()}`;
  const previous = SESSION_MESSAGES.get(activeSessionId) || [];
  const guardedSystemPrompt = `${systemPrompt}

## Tool safety
- Do not call execute_python_script.
- Call delete_all_spawned only when the user explicitly asks to clear/reset/rebuild the whole scene.
- Use setup_environment before placing new objects if the environment is not ready.
- Prefer spawn_blueprint_actor for buildings, trees, vehicles, and street furniture.`;

  const messages = [
    { role: "system", content: guardedSystemPrompt },
    ...previous.filter((item) => item.role !== "system"),
    { role: "user", content: message },
  ];

  let mcp = null;
  let closed = false;
  const closeHandler = () => {
    closed = true;
    if (mcp) mcp.close();
  };
  res?.on?.("close", closeHandler);

  try {
    emit("system", {
      sessionId: activeSessionId,
      provider: config.provider,
      model: config.model,
      mcpServers: [{ name: "simworld", status: "connected" }],
    });
    logToFile("llm", `Provider=${config.provider} model=${config.model} session=${activeSessionId}`);

    mcp = new McpClient({
      serverPath: mcpServerPath,
      cwd: mcpCwd,
      env,
      log: logToFile,
    });
    await mcp.start();

    const mcpTools = await mcp.listTools();
    const tools = mcpTools
      .filter((tool) => toolAllowedForMessage(tool.name, message))
      .map(toOpenAiTool);

    let latestShot = null;
    const maxTurns = Number(env.SIMWORLD_LLM_MAX_TOOL_TURNS || 12);
    for (let turn = 0; turn < maxTurns; turn++) {
      if (closed || res?.writableEnded) return;
      const data = await postChatCompletion(config, {
        model: config.model,
        messages,
        tools,
        tool_choice: tools.length ? "auto" : undefined,
        temperature: Number(env.SIMWORLD_LLM_TEMPERATURE || 0.2),
      });

      const choice = data.choices?.[0] || {};
      const assistantMessage = choice.message || {};
      const text = normalizeContent(assistantMessage.content);
      const toolCalls = Array.isArray(assistantMessage.tool_calls) ? assistantMessage.tool_calls : [];

      if (text && toolCalls.length === 0) emit("text", { delta: `${text}\n` });
      messages.push({
        role: "assistant",
        content: assistantMessage.content || "",
        tool_calls: toolCalls.length ? toolCalls : undefined,
      });

      if (toolCalls.length === 0) {
        storeTextSessionTurn(activeSessionId, previous, message, text);
        emit("done", {
          sessionId: activeSessionId,
          isError: false,
          latestScreenshot: latestShot || latestScreenshotUrl(screenshotDir),
          provider: config.provider,
          model: config.model,
        });
        return;
      }

      if (text) emit("text", { delta: text });

      for (const call of toolCalls) {
        const callId = call.id || `call_${Date.now()}`;
        const toolName = call.function?.name || call.name;
        const args = parseToolArgs(call.function?.arguments || call.arguments);
        emit("tool_start", { id: callId, name: toolName, displayName: toolName });
        emit("tool_details", { id: callId, name: toolName, displayName: toolName, input: args });
        logToFile("tool", `Starting: ${toolName}`);

        let resultText;
        let isError = false;
        try {
          const result = await mcp.callTool(toolName, args);
          isError = Boolean(result?.isError);
          resultText = stringifyToolResult(result);
        } catch (err) {
          isError = true;
          resultText = JSON.stringify({ error: err.message });
        }

        const shot = screenshotUrlFromToolResult(resultText);
        if (shot) {
          latestShot = shot;
          emit("screenshot", { toolUseId: callId, filepath: shot });
        }
        emit("tool_result", {
          toolUseId: callId,
          result: resultText.slice(0, 2000),
          isError,
        });
        logToFile("tool_result", `${String(callId).slice(0, 8)} -> ${resultText.slice(0, 300)}`);
        messages.push({
          role: "tool",
          tool_call_id: callId,
          content: resultText,
        });
      }
    }

    emit("text", { delta: "\nTool loop reached the configured turn limit before the model finished.\n" });
    emit("done", {
      sessionId: activeSessionId,
      isError: true,
      latestScreenshot: latestShot || latestScreenshotUrl(screenshotDir),
      provider: config.provider,
      model: config.model,
    });
  } catch (err) {
    logToFile("llm_error", err.stack || err.message);
    emit("text", { delta: `\n\nLLM provider error: ${err.message}\n` });
    emit("done", {
      sessionId: activeSessionId,
      isError: true,
      latestScreenshot: latestScreenshotUrl(screenshotDir),
      provider: config.provider,
      model: config.model,
    });
  } finally {
    if (keepAlive) clearInterval(keepAlive);
    if (mcp) mcp.close();
    res?.off?.("close", closeHandler);
    if (res && !res.writableEnded) res.end();
  }
}

module.exports = {
  resolveLlmProvider,
  shouldUseOpenAiCompatible,
  runOpenAiCompatibleChat,
};
