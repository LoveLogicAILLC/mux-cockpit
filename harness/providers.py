"""
Streaming model providers with native tool calling. Stdlib HTTP only.

  ollama   — POST /api/chat (NDJSON stream, tools=[...])
  openai   — POST {base}/chat/completions (SSE, tool_calls deltas) — LM Studio, vLLM,
             llama.cpp server, OpenRouter, OpenAI, etc.
  mock     — deterministic offline brain that exercises real tools (tests/demo)

Every provider yields normalized events:
  ("text_delta", str) | ("thinking_delta", str) | ("toolcall", {id,name,arguments})
  ("usage", (input, output)) | ("stop", "stop"|"length"|"toolUse"|"error"|"aborted") | ("error", msg)
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import threading
import urllib.error
import urllib.request
from typing import AsyncIterator, Callable, List, Optional, Tuple

from .protocol import message_text, new_id, tool_calls

Event = Tuple[str, object]


# --------------------------------------------------------------------------- #
# thread -> asyncio bridge for blocking streaming HTTP
# --------------------------------------------------------------------------- #
async def _stream_lines(url: str, payload: dict, headers: dict, abort: asyncio.Event,
                        timeout: float) -> AsyncIterator[bytes]:
    loop = asyncio.get_running_loop()
    q: asyncio.Queue = asyncio.Queue()
    stop = threading.Event()
    END = object()

    def post(item):
        try:
            loop.call_soon_threadsafe(q.put_nowait, item)
        except RuntimeError:  # loop already closed (abort/shutdown) -> drop
            stop.set()

    def worker():
        try:
            req = urllib.request.Request(url, data=json.dumps(payload).encode(),
                                         headers={"Content-Type": "application/json", **headers})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                for line in r:
                    if stop.is_set():
                        break
                    post(line)
        except urllib.error.HTTPError as e:
            body = e.read().decode(errors="replace")[:1000]
            post(RuntimeError(f"HTTP {e.code}: {body}"))
        except Exception as e:
            post(e)
        finally:
            post(END)

    threading.Thread(target=worker, daemon=True).start()
    abort_wait = asyncio.create_task(abort.wait())
    try:
        while True:
            get = asyncio.create_task(q.get())
            done, _ = await asyncio.wait({get, abort_wait}, return_when=asyncio.FIRST_COMPLETED)
            if get not in done:
                get.cancel()
                stop.set()
                return
            item = get.result()
            if item is END:
                return
            if isinstance(item, Exception):
                raise item
            yield item
    finally:
        stop.set()
        abort_wait.cancel()


def salvage_text_toolcall(txt: str, tool_names: List[str]) -> Optional[dict]:
    """Local models (llama3.1 et al.) often print a tool call as JSON text instead of
    emitting a native call. Accept {"name":..,"parameters"|"arguments":{..}} when the
    whole message is that object."""
    s = re.sub(r"^```(?:json)?|```$", "", txt.strip(), flags=re.M).strip()
    s = re.sub(r"^<\|python_tag\|>", "", s).strip()
    if not (s.startswith("{") and s.endswith("}")):
        return None
    try:
        obj = json.loads(s)
    except json.JSONDecodeError:
        return None
    name = obj.get("name") or obj.get("tool")
    args = obj.get("arguments", obj.get("parameters", obj.get("args")))
    if name in tool_names and isinstance(args, dict):
        return {"id": new_id("call_"), "name": name, "arguments": args}
    return None


# --------------------------------------------------------------------------- #
# Ollama
# --------------------------------------------------------------------------- #
class OllamaProvider:
    name = "ollama"

    def __init__(self, host: str = None, timeout: float = 600, num_ctx: int = 16384):
        self.host = (host or os.environ.get("OLLAMA_HOST", "http://127.0.0.1:11434")).rstrip("/")
        if not self.host.startswith("http"):
            self.host = "http://" + self.host
        self.timeout = timeout
        self.num_ctx = num_ctx

    def list_models(self) -> List[str]:
        with urllib.request.urlopen(self.host + "/api/tags", timeout=2) as r:
            return [m["name"] for m in json.loads(r.read()).get("models", [])]

    @staticmethod
    def to_wire(system: str, messages: List[dict]) -> List[dict]:
        out = [{"role": "system", "content": system}]
        for m in messages:
            if m["role"] == "user":
                out.append({"role": "user", "content": message_text(m)})
            elif m["role"] == "assistant":
                w = {"role": "assistant", "content": message_text(m)}
                tcs = tool_calls(m)
                if tcs:
                    w["tool_calls"] = [{"function": {"name": t["name"], "arguments": t["arguments"]}} for t in tcs]
                out.append(w)
            elif m["role"] == "toolResult":
                out.append({"role": "tool", "tool_name": m["toolName"], "content": message_text(m)})
        return out

    async def stream(self, model: str, system: str, messages: List[dict], tools: List[dict],
                     abort: asyncio.Event, thinking: str = "off") -> AsyncIterator[Event]:
        payload = {"model": model, "messages": self.to_wire(system, messages), "stream": True,
                   "options": {"num_ctx": self.num_ctx}}
        if tools:
            payload["tools"] = [{"type": "function", "function": t} for t in tools]
        if thinking != "off":
            payload["think"] = True
        got_call, reason = False, "stop"
        try:
            async for line in _stream_lines(self.host + "/api/chat", payload, {}, abort, self.timeout):
                if not line.strip():
                    continue
                d = json.loads(line)
                if d.get("error"):
                    yield ("error", d["error"])
                    return
                msg = d.get("message", {})
                if msg.get("thinking"):
                    yield ("thinking_delta", msg["thinking"])
                if msg.get("content"):
                    yield ("text_delta", msg["content"])
                for tc in msg.get("tool_calls") or []:
                    f = tc.get("function", {})
                    args = f.get("arguments", {})
                    if isinstance(args, str):
                        try:
                            args = json.loads(args)
                        except json.JSONDecodeError:
                            args = {"_raw": args}
                    got_call = True
                    yield ("toolcall", {"id": tc.get("id") or new_id("call_"), "name": f.get("name"), "arguments": args})
                if d.get("done"):
                    yield ("usage", (int(d.get("prompt_eval_count", 0)), int(d.get("eval_count", 0))))
                    reason = "length" if d.get("done_reason") == "length" else "stop"
        except Exception as e:
            yield ("error", f"{type(e).__name__}: {e}")
            return
        if abort.is_set():
            yield ("stop", "aborted")
            return
        yield ("stop", "toolUse" if got_call else reason)


# --------------------------------------------------------------------------- #
# OpenAI-compatible
# --------------------------------------------------------------------------- #
class OpenAIProvider:
    name = "openai"

    def __init__(self, base_url: str = None, api_key: str = None, timeout: float = 600):
        self.base = (base_url or os.environ.get("OPENAI_BASE_URL", "http://127.0.0.1:1234/v1")).rstrip("/")
        self.key = api_key or os.environ.get("OPENAI_API_KEY", "")
        self.timeout = timeout

    @staticmethod
    def to_wire(system: str, messages: List[dict]) -> List[dict]:
        out = [{"role": "system", "content": system}]
        for m in messages:
            if m["role"] == "user":
                out.append({"role": "user", "content": message_text(m)})
            elif m["role"] == "assistant":
                w = {"role": "assistant", "content": message_text(m) or None}
                tcs = tool_calls(m)
                if tcs:
                    w["tool_calls"] = [{"id": t["id"], "type": "function",
                                        "function": {"name": t["name"], "arguments": json.dumps(t["arguments"])}}
                                       for t in tcs]
                out.append(w)
            elif m["role"] == "toolResult":
                out.append({"role": "tool", "tool_call_id": m["toolCallId"], "content": message_text(m)})
        return out

    async def stream(self, model, system, messages, tools, abort, thinking="off"):
        payload = {"model": model, "messages": self.to_wire(system, messages), "stream": True,
                   "stream_options": {"include_usage": True}}
        if tools:
            payload["tools"] = [{"type": "function", "function": t} for t in tools]
        headers = {"Authorization": f"Bearer {self.key}"} if self.key else {}
        calls: dict = {}
        reason = "stop"
        try:
            async for raw in _stream_lines(self.base + "/chat/completions", payload, headers, abort, self.timeout):
                line = raw.decode(errors="replace").strip()
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    break
                d = json.loads(data)
                if d.get("usage"):
                    u = d["usage"]
                    yield ("usage", (int(u.get("prompt_tokens", 0)), int(u.get("completion_tokens", 0))))
                for ch in d.get("choices", []):
                    delta = ch.get("delta", {})
                    if delta.get("reasoning_content") or delta.get("reasoning"):
                        yield ("thinking_delta", delta.get("reasoning_content") or delta.get("reasoning"))
                    if delta.get("content"):
                        yield ("text_delta", delta["content"])
                    for tc in delta.get("tool_calls") or []:
                        slot = calls.setdefault(tc.get("index", 0), {"id": None, "name": "", "args": ""})
                        slot["id"] = tc.get("id") or slot["id"]
                        fn = tc.get("function", {})
                        slot["name"] += fn.get("name") or ""
                        slot["args"] += fn.get("arguments") or ""
                    if ch.get("finish_reason") == "length":
                        reason = "length"
        except Exception as e:
            yield ("error", f"{type(e).__name__}: {e}")
            return
        if abort.is_set():
            yield ("stop", "aborted")
            return
        for _, s in sorted(calls.items()):
            try:
                args = json.loads(s["args"] or "{}")
            except json.JSONDecodeError:
                args = {"_raw": s["args"]}
            yield ("toolcall", {"id": s["id"] or new_id("call_"), "name": s["name"], "arguments": args})
        yield ("stop", "toolUse" if calls else reason)

# --------------------------------------------------------------------------- #
# Google Gemini
# --------------------------------------------------------------------------- #
class GeminiProvider:
    name = "gemini"

    def __init__(self, api_key: str = None, timeout: float = 600):
        self.key = api_key or os.environ.get("GEMINI_API_KEY", "")
        self.base = "https://generativelanguage.googleapis.com/v1beta"
        self.timeout = timeout

    @staticmethod
    def to_wire(system: str, messages: List[dict]) -> dict:
        contents = []
        for m in messages:
            if m["role"] == "user":
                contents.append({"role": "user", "parts": [{"text": message_text(m)}]})
            elif m["role"] == "assistant":
                txt = message_text(m)
                parts = [{"text": txt}] if txt else []
                for tc in tool_calls(m):
                    parts.append({"functionCall": {"id": tc["id"], "name": tc["name"], "args": tc["arguments"]}})
                contents.append({"role": "model", "parts": parts})
            elif m["role"] == "toolResult":
                # Gemini expects functionResponse parts under role "user" (not "function") per
                # current API docs, and the response name must be the actual tool name.
                contents.append({"role": "user", "parts": [{"functionResponse": {
                    "id": m.get("toolCallId"), "name": m["toolName"],
                    "response": {"name": m["toolName"], "content": message_text(m)}}}]})
        return {"system_instruction": {"parts": [{"text": system}]} if system else None, "contents": contents}

    async def stream(self, model, system, messages, tools, abort, thinking="off"):
        payload = {"model": f"models/{model}", "generationConfig": {"temperature": 0.2}}
        wire = self.to_wire(system, messages)
        if wire.get("system_instruction"):
            payload["systemInstruction"] = wire["system_instruction"]
        payload["contents"] = wire["contents"]
        if tools:
            payload["tools"] = [{"functionDeclarations": tools}]
        url = f"{self.base}/models/{model}:streamGenerateContent?alt=sse&key={self.key}"
        headers = {"Content-Type": "application/json"}
        calls: List[dict] = []
        reason = "stop"
        try:
            async for raw in _stream_lines(url, payload, headers, abort, self.timeout):
                line = raw.decode(errors="replace").strip()
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if not data or data == "[DONE]":
                    continue
                d = json.loads(data)
                if "usageMetadata" in d:
                    u = d["usageMetadata"]
                    yield ("usage", (int(u.get("promptTokenCount", 0)), int(u.get("candidatesTokenCount", 0))))
                for c in d.get("candidates", []):
                    for p in c.get("content", {}).get("parts", []):
                        if "text" in p:
                            yield ("text_delta", p["text"])
                        if "functionCall" in p:
                            fc = p["functionCall"]
                            call = {"id": fc.get("id") or new_id("call_"), "name": fc["name"], "arguments": fc.get("args", {})}
                            calls.append(call)
                            yield ("toolcall", call)
                    if c.get("finishReason") == "MAX_TOKENS":
                        reason = "length"
        except Exception as e:
            yield ("error", f"{type(e).__name__}: {e}")
            return
        if abort.is_set():
            yield ("stop", "aborted")
            return
        yield ("stop", "toolUse" if calls else reason)



# --------------------------------------------------------------------------- #
# Mock / scripted
# --------------------------------------------------------------------------- #
class ScriptedProvider:
    """script: callable(messages) -> {"text": str, "calls": [{"name","arguments"}]}"""
    name = "mock"

    def __init__(self, script: Optional[Callable[[List[dict]], dict]] = None, delay: float = 0.02):
        self.script = script or mock_brain
        self.delay = delay

    async def stream(self, model, system, messages, tools, abort, thinking="off"):
        try:
            step = self.script(messages, system)
        except TypeError:
            step = self.script(messages)
        txt = step.get("text", "")
        for i in range(0, len(txt), 12):
            if abort.is_set():
                yield ("stop", "aborted")
                return
            await asyncio.sleep(self.delay)
            yield ("text_delta", txt[i:i + 12])
        for c in step.get("calls", []):
            yield ("toolcall", {"id": new_id("call_"), "name": c["name"], "arguments": c["arguments"]})
        yield ("usage", (sum(len(message_text(m)) for m in messages) // 4 + 50, len(txt) // 4 + 5))
        yield ("stop", "toolUse" if step.get("calls") else "stop")


_MOCK_SCORES = [7.4, 5.1, 8.2, 6.6]
_mock_n = [0]


def mock_brain(messages: List[dict], system: str = "") -> dict:
    """Role-aware offline brain: inspect with real tools, then answer in the role's contract
    (planner -> plan JSON, critic -> score JSON). Exercises the full MUX pipeline."""
    role = (re.search(r"# Role: (\w+)", system) or [None, "GENERAL"])[1]
    last = messages[-1]
    if last["role"] == "user":
        goal = message_text(last)[:80]
        return {"text": f"[{role}] on it: {goal}",
                "calls": [{"name": "todo", "arguments": {"todos": [
                              {"content": "inspect workspace", "status": "in_progress"},
                              {"content": f"deliver: {goal[:50]}", "status": "pending"}]}},
                          {"name": "ls", "arguments": {"path": "."}}]}
    listing = next((message_text(m) for m in reversed(messages)
                    if m["role"] == "toolResult" and m["toolName"] == "ls"), "")
    n = len([l for l in listing.splitlines() if l.strip()])
    goal = next((message_text(m) for m in reversed(messages) if m["role"] == "user"), "")
    if role == "PLANNER":
        return {"text": "Plan ready.\n" + json.dumps({"steps": [
            {"id": 1, "action": "research", "desc": "map existing modules"},
            {"id": 2, "action": "implement", "desc": "apply the change with tests"}]})}
    if role == "CRITIC":
        _mock_n[0] += 1
        sc = _MOCK_SCORES[_mock_n[0] % len(_MOCK_SCORES)]
        return {"text": "Reviewed.\n" + json.dumps({"score": sc, "fails": [] if sc >= 6 else ["weak tests"],
                                                      "fix": "add a failing-case test"})}
    return {"text": f"Workspace has {n} entries. Done: {goal[:60]} (mock provider — "
                    "set MUX_PROVIDER=ollama for real work).\nSTATUS: done"}


def make_provider(kind: Optional[str] = None):
    kind = (kind or os.environ.get("MUX_PROVIDER") or "auto").lower()
    if kind == "mock":
        return ScriptedProvider()
    if kind == "openai":
        return OpenAIProvider()
    if kind == "gemini":
        return GeminiProvider()
    if kind == "ollama":
        return OllamaProvider()
    # auto: Ollama if reachable, else Gemini/OpenAI-compat if configured, else mock
    if os.environ.get("GEMINI_API_KEY"):
        return GeminiProvider()
    op = OllamaProvider()
    try:
        op.list_models()
        return op
    except Exception:
        pass
    if os.environ.get("OPENAI_BASE_URL") or os.environ.get("OPENAI_API_KEY"):
        return OpenAIProvider()
    return ScriptedProvider()
