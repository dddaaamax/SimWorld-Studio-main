#!/usr/bin/env python3
"""Anthropic-to-OpenAI API proxy for vLLM.

Translates Anthropic Messages API requests into OpenAI Chat Completions
format, forwards to vLLM, and converts the response back. This enables
proper tool calling with vLLM-hosted models via Claude Code.

The proxy is needed because vLLM's native Anthropic API endpoint doesn't
properly serialize tool_use blocks in responses, while the OpenAI endpoint
with --enable-auto-tool-choice works correctly.

Usage:
    # Start proxy on port 30010, forwarding to vLLM on port 30001
    python anthropic_proxy.py --vllm-port 30001 --proxy-port 30010

    # Then point Claude Code at the proxy:
    ANTHROPIC_BASE_URL=http://localhost:30010 claude -p "..."
"""

import argparse
import json
import sys
import uuid
from http.server import HTTPServer, BaseHTTPRequestHandler
from socketserver import ThreadingMixIn
import urllib.request
import urllib.error


class ThreadingHTTPServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True

VLLM_BASE = "http://localhost:30001"


TOOL_FORMAT_INSTRUCTION = (
    "\n\nIMPORTANT: When calling a tool, output ONLY this JSON format:\n"
    '<tool_call>\n{"name": "function_name", "arguments": {"key": "value"}}\n</tool_call>\n'
    "Do NOT use <function=> or <parameter=> XML tags. ONLY use the JSON format above.\n"
    "Call one tool at a time and wait for the result.\n"
)

# Few-shot examples injected into conversations to teach the model
# the correct tool call format. This is critical for vLLM's hermes
# parser to detect and parse tool calls properly.
FEW_SHOT_EXAMPLES = [
    {"role": "user", "content": "Set up the environment"},
    {"role": "assistant", "content": None, "tool_calls": [
        {"id": "call_example1", "type": "function",
         "function": {"name": "mcp__simworld__setup_environment",
                      "arguments": '{"time_of_day": "afternoon"}'}}
    ]},
    {"role": "tool", "tool_call_id": "call_example1",
     "content": '{"status": "success", "message": "Environment set up"}'},
    {"role": "assistant", "content": "Environment is ready. What would you like me to build?"},
]


def anthropic_to_openai(body: dict) -> dict:
    """Convert Anthropic Messages API request to OpenAI Chat Completions."""
    messages = []

    # System message (with tool format instruction appended)
    system = body.get("system", "")
    if isinstance(system, list):
        system = "\n".join(b.get("text", "") for b in system if b.get("type") == "text")
    if body.get("tools"):
        system += TOOL_FORMAT_INSTRUCTION
    if system:
        messages.append({"role": "system", "content": system})

    # Inject few-shot examples if tools are present and this is the first turn
    if body.get("tools") and len(body.get("messages", [])) <= 2:
        messages.extend(FEW_SHOT_EXAMPLES)

    # Convert messages
    for msg in body.get("messages", []):
        role = msg["role"]
        content = msg.get("content", "")

        if isinstance(content, str):
            messages.append({"role": role, "content": content})
        elif isinstance(content, list):
            # Handle mixed content (text + tool_use + tool_result)
            text_parts = []
            tool_calls = []
            tool_results = []

            for block in content:
                btype = block.get("type", "")
                if btype == "text":
                    text_parts.append(block.get("text", ""))
                elif btype == "tool_use":
                    tool_calls.append({
                        "id": block.get("id", f"call_{uuid.uuid4().hex[:8]}"),
                        "type": "function",
                        "function": {
                            "name": block.get("name", ""),
                            "arguments": json.dumps(block.get("input", {})),
                        },
                    })
                elif btype == "tool_result":
                    tool_result_content = block.get("content", "")
                    if isinstance(tool_result_content, list):
                        tool_result_content = "\n".join(
                            b.get("text", "") for b in tool_result_content if b.get("type") == "text"
                        )
                    tool_results.append({
                        "tool_call_id": block.get("tool_use_id", ""),
                        "content": str(tool_result_content),
                    })

            if role == "assistant":
                msg_out = {"role": "assistant", "content": "\n".join(text_parts) or None}
                if tool_calls:
                    msg_out["tool_calls"] = tool_calls
                messages.append(msg_out)
            elif role == "user":
                if tool_results:
                    for tr in tool_results:
                        messages.append({"role": "tool", **tr})
                if text_parts:
                    messages.append({"role": "user", "content": "\n".join(text_parts)})

    # Convert tools — limit to essential ones to avoid exceeding model context.
    # Claude Code sends ALL MCP tools (22+) but small models can't handle them all.
    # Only keep the core scene-building MCP tools (drop built-in Claude tools
    # like Bash/Read/Edit and non-essential MCP tools to fit in context)
    ESSENTIAL_TOOLS = {
        "mcp__simworld__spawn_blueprint_actor",
        "mcp__simworld__spawn_actor",
        "mcp__simworld__delete_all_spawned",
        "mcp__simworld__setup_environment",
        "mcp__simworld__take_screenshot",
        "mcp__simworld__list_assets",
        "mcp__simworld__set_actor_transform",
        "mcp__simworld__verify_scene",
    }
    tools = []
    for tool in body.get("tools", []):
        name = tool.get("name", "")
        if ESSENTIAL_TOOLS and name not in ESSENTIAL_TOOLS:
            sys.stderr.write(f"[proxy] SKIP tool: {name}\n")
            continue
        sys.stderr.write(f"[proxy] KEEP tool: {name}\n")
        tools.append({
            "type": "function",
            "function": {
                "name": name,
                "description": tool.get("description", "")[:200],  # Truncate long descriptions
                "parameters": tool.get("input_schema", {}),
            },
        })

    # Cap max_tokens to avoid exceeding model context (65k total).
    # Claude Code often requests 32000 which is too much with large tool schemas.
    max_tokens = min(body.get("max_tokens", 4096), 4096)

    req = {
        "model": body.get("model", "default"),
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": body.get("temperature", 0),
        "stream": False,
    }
    sys.stderr.write(f"[proxy] Converted: msgs={len(messages)} tools_out={len(tools)} max_tokens={max_tokens}\n")
    sys.stderr.flush()

    if tools:
        req["tools"] = tools
        # Check the ORIGINAL Anthropic messages (not few-shot examples) for
        # prior tool calls. If the model hasn't called any tools yet, force
        # tool_choice=required so small models actually invoke tools instead
        # of just planning in text.
        has_prior_tool_calls = False
        for msg in body.get("messages", []):
            if msg.get("role") == "assistant":
                content = msg.get("content", [])
                if isinstance(content, list):
                    for block in content:
                        if block.get("type") == "tool_use":
                            has_prior_tool_calls = True
                            break
        req["tool_choice"] = "auto" if has_prior_tool_calls else "required"

    return req


def openai_to_anthropic(resp: dict, model: str) -> dict:
    """Convert OpenAI Chat Completions response to Anthropic Messages API."""
    choice = resp.get("choices", [{}])[0]
    message = choice.get("message", {})
    finish = choice.get("finish_reason", "stop")

    content = []

    # Text content
    text = message.get("content")
    if text:
        # Strip <think>...</think> blocks for cleaner output
        import re
        text = re.sub(r'<think>.*?</think>\s*', '', text, flags=re.DOTALL).strip()
        if text:
            content.append({"type": "text", "text": text})

    # Tool calls
    tool_calls = message.get("tool_calls") or []
    for tc in tool_calls:
        func = tc.get("function", {})
        try:
            args = json.loads(func.get("arguments", "{}"))
        except json.JSONDecodeError:
            args = {}
        content.append({
            "type": "tool_use",
            "id": tc.get("id", f"toolu_{uuid.uuid4().hex[:12]}"),
            "name": func.get("name", ""),
            "input": args,
        })

    # Map stop reason
    if finish == "tool_calls" or tool_calls:
        stop_reason = "tool_use"
    elif finish == "length":
        stop_reason = "max_tokens"
    else:
        stop_reason = "end_turn"

    usage = resp.get("usage", {})

    return {
        "id": resp.get("id", f"msg_{uuid.uuid4().hex[:12]}"),
        "type": "message",
        "role": "assistant",
        "content": content or [{"type": "text", "text": ""}],
        "model": model,
        "stop_reason": stop_reason,
        "stop_sequence": None,
        "usage": {
            "input_tokens": usage.get("prompt_tokens", 0),
            "output_tokens": usage.get("completion_tokens", 0),
        },
    }


class ProxyHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        sys.stderr.write(f"[proxy] {args[0]} {args[1]}\n")

    def do_POST(self):
        try:
            content_len = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(content_len)) if content_len > 0 else {}

            if "/messages" in self.path:
                self._handle_messages(body)
            else:
                self.send_error(404, "Not found")
        except Exception as e:
            sys.stderr.write(f"[proxy] Error in POST handler: {e}\n")
            import traceback; traceback.print_exc(file=sys.stderr)
            try:
                self.send_response(500)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({"type": "error", "error": {"message": str(e)}}).encode())
            except Exception:
                pass

    def do_GET(self):
        if "/models" in self.path:
            # Forward to vLLM
            try:
                req = urllib.request.Request(f"{VLLM_BASE}/v1/models")
                with urllib.request.urlopen(req, timeout=10) as resp:
                    data = resp.read()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(data)
            except Exception as e:
                self.send_error(502, str(e))
        else:
            self.send_error(404, "Not found")

    def _handle_messages(self, body: dict):
        model = body.get("model", "default")
        stream = body.get("stream", False)

        incoming_tools = len(body.get('tools', []))
        sys.stderr.write(f"[proxy] Request: model={model} stream={stream} msgs={len(body.get('messages',[]))} tools_in={incoming_tools}\n")
        sys.stderr.flush()

        # Convert to OpenAI format
        try:
            openai_req = anthropic_to_openai(body)
        except Exception as e:
            sys.stderr.write(f"[proxy] Conversion error: {e}\n")
            import traceback; traceback.print_exc(file=sys.stderr)
            self.send_response(500)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"type":"error","error":{"message":str(e)}}).encode())
            return

        # Always disable streaming — we collect the full response and convert.
        # Claude Code handles non-streaming responses fine.
        openai_req["stream"] = False
        stream = False  # Force non-streaming response

        # Forward to vLLM OpenAI endpoint
        try:
            data = json.dumps(openai_req).encode()
            req = urllib.request.Request(
                f"{VLLM_BASE}/v1/chat/completions",
                data=data,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=300) as resp:
                openai_resp = json.loads(resp.read())
        except urllib.error.HTTPError as e:
            error_body = e.read().decode()
            sys.stderr.write(f"[proxy] vLLM error {e.code}: {error_body[:500]}\n")
            self.send_response(e.code)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({
                "type": "error",
                "error": {"type": "api_error", "message": error_body[:500]},
            }).encode())
            return
        except Exception as e:
            sys.stderr.write(f"[proxy] Connection error: {e}\n")
            self.send_error(502, str(e))
            return

        # Convert response
        anthropic_resp = openai_to_anthropic(openai_resp, model)

        # Log what we're sending back
        tool_names = [b["name"] for b in anthropic_resp.get("content", []) if b.get("type") == "tool_use"]
        sys.stderr.write(f"[proxy] Response: stop={anthropic_resp.get('stop_reason')} tools={tool_names}\n")

        if stream:
            # Emit SSE events that mimic Anthropic streaming
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()

            # message_start
            self._sse("message_start", {
                "type": "message_start",
                "message": {
                    "id": anthropic_resp["id"],
                    "type": "message",
                    "role": "assistant",
                    "content": [],
                    "model": model,
                    "stop_reason": None,
                    "stop_sequence": None,
                    "usage": {"input_tokens": anthropic_resp["usage"]["input_tokens"], "output_tokens": 0},
                },
            })

            # content blocks
            for i, block in enumerate(anthropic_resp["content"]):
                self._sse("content_block_start", {
                    "type": "content_block_start",
                    "index": i,
                    "content_block": block if block["type"] == "tool_use" else {"type": "text", "text": ""},
                })
                if block["type"] == "text":
                    self._sse("content_block_delta", {
                        "type": "content_block_delta",
                        "index": i,
                        "delta": {"type": "text_delta", "text": block["text"]},
                    })
                elif block["type"] == "tool_use":
                    self._sse("content_block_delta", {
                        "type": "content_block_delta",
                        "index": i,
                        "delta": {"type": "input_json_delta", "partial_json": json.dumps(block["input"])},
                    })
                self._sse("content_block_stop", {"type": "content_block_stop", "index": i})

            # message_delta + message_stop
            self._sse("message_delta", {
                "type": "message_delta",
                "delta": {"stop_reason": anthropic_resp["stop_reason"], "stop_sequence": None},
                "usage": {"output_tokens": anthropic_resp["usage"]["output_tokens"]},
            })
            self._sse("message_stop", {"type": "message_stop"})
        else:
            resp_bytes = json.dumps(anthropic_resp).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(resp_bytes)))
            self.end_headers()
            self.wfile.write(resp_bytes)
            self.wfile.flush()

    def _sse(self, event: str, data: dict):
        self.wfile.write(f"event: {event}\ndata: {json.dumps(data)}\n\n".encode())
        self.wfile.flush()


def main():
    parser = argparse.ArgumentParser(description="Anthropic-to-OpenAI proxy for vLLM")
    parser.add_argument("--vllm-port", type=int, default=30001,
                        help="vLLM server port (default: 30001)")
    parser.add_argument("--vllm-host", default="localhost",
                        help="vLLM server host (default: localhost)")
    parser.add_argument("--proxy-port", type=int, default=30010,
                        help="Proxy listen port (default: 30010)")
    args = parser.parse_args()

    global VLLM_BASE
    VLLM_BASE = f"http://{args.vllm_host}:{args.vllm_port}"

    server = ThreadingHTTPServer(("0.0.0.0", args.proxy_port), ProxyHandler)
    print(f"Anthropic proxy listening on :{args.proxy_port} -> {VLLM_BASE}")
    print(f"Set ANTHROPIC_BASE_URL=http://localhost:{args.proxy_port}")
    server.serve_forever()


if __name__ == "__main__":
    main()
