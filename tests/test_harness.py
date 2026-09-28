"""
Harness conformance + behavior tests (stdlib unittest, no network, no Ollama needed).
Run:  python3 -m unittest discover -s tests -v
"""
import asyncio
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from harness import tools as T  # noqa: E402
from harness.agent import Agent  # noqa: E402
from harness.protocol import message_text  # noqa: E402
from harness.providers import GeminiProvider, OllamaProvider, OpenAIProvider, ScriptedProvider  # noqa: E402
from harness.session import load_session  # noqa: E402


def run(coro):
    return asyncio.run(coro)


async def noop(_):
    pass


class Script:
    """Deterministic provider script: list of steps consumed per model call."""

    def __init__(self, steps):
        self.steps = list(steps)
        self.calls = 0

    def __call__(self, messages, system=""):
        self.calls += 1
        if not self.steps:
            return {"text": "done"}
        s = self.steps.pop(0)
        return s(messages) if callable(s) else s


def call(name, **args):
    return {"text": "", "calls": [{"name": name, "arguments": args}]}


class WS(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ws = Path(self.tmp.name)
        self.ctx = T.ToolContext(cwd=self.ws)

    def tearDown(self):
        self.tmp.cleanup()

    def tool(self, name, **args):
        return run(T.run_tool(name, args, self.ctx, noop))


# ---------------------------------------------------------------------------- tools
class TestTools(WS):
    def test_read_offset_and_truncation_notice(self):
        (self.ws / "big.txt").write_text("\n".join(f"line{i}" for i in range(1, 3001)))
        res, err = self.tool("read", path="big.txt")
        self.assertFalse(err)
        txt = res["content"][0]["text"]
        self.assertIn("line2000", txt)
        self.assertNotIn("line2001\n", txt)
        self.assertIn("offset=2001", txt)
        res, _ = self.tool("read", path="big.txt", offset=2999, limit=5)
        self.assertTrue(res["content"][0]["text"].startswith("line2999\nline3000"))

    def test_edit_unique_ambiguous_missing(self):
        f = self.ws / "a.py"
        f.write_text("x = 1\ny = 1\nx = 1\n")
        _, err = self.tool("edit", path="a.py", oldText="x = 1", newText="x = 2")
        self.assertTrue(err)  # ambiguous
        _, err = self.tool("edit", path="a.py", oldText="nope", newText="q")
        self.assertTrue(err)
        res, err = self.tool("edit", path="a.py", oldText="y = 1", newText="y = 9")
        self.assertFalse(err)
        self.assertEqual(f.read_text(), "x = 1\ny = 9\nx = 1\n")
        self.assertIn("+y = 9", res["details"]["diff"])

    def test_hashline_edit_and_stale_anchor(self):
        f = self.ws / "h.txt"
        f.write_text("alpha\nbeta\ngamma\ndelta")
        res, _ = self.tool("read", path="h.txt", hashline=True)
        lines = res["content"][0]["text"].splitlines()
        a2 = lines[1].split("|")[0]
        a3 = lines[2].split("|")[0]
        a4 = lines[3].split("|")[0]
        _, err = self.tool("hashline_edit", path="h.txt", edits=[
            {"op": "replace", "anchor": a2, "end": a3, "content": "BETA\nGAMMA"},
            {"op": "insert_after", "anchor": a4, "content": "epsilon"}])
        self.assertFalse(err)
        self.assertEqual(f.read_text(), "alpha\nBETA\nGAMMA\ndelta\nepsilon")
        # the old anchor for line 2 no longer matches -> rejected, file untouched
        res, err = self.tool("hashline_edit", path="h.txt", edits=[{"op": "delete", "anchor": a2}])
        self.assertTrue(err)
        self.assertIn("stale anchor", res["content"][0]["text"])
        self.assertEqual(f.read_text(), "alpha\nBETA\nGAMMA\ndelta\nepsilon")

    def test_jail_blocks_escape(self):
        res, err = self.tool("read", path="../../etc/passwd")
        self.assertTrue(err)
        self.assertIn("escapes workspace", res["content"][0]["text"])
        _, err = self.tool("write", path="/tmp/evil_outside.txt", content="x")
        self.assertTrue(err)

    def test_bash_exit_code_timeout_and_policy(self):
        res, err = self.tool("bash", command="echo ok")
        self.assertFalse(err)
        self.assertEqual(res["content"][0]["text"].strip(), "ok")
        res, err = self.tool("bash", command="echo bad; exit 4")
        self.assertTrue(err)
        self.assertIn("code 4", res["content"][0]["text"])
        t0 = time.time()
        res, err = self.tool("bash", command="sleep 30", timeout=1)
        self.assertTrue(err)
        self.assertLess(time.time() - t0, 5)
        self.assertIn("timed out", res["content"][0]["text"])
        self.ctx.bash_policy = __import__("harness.agent", fromlist=["x"]).default_bash_policy
        res, err = self.tool("bash", command="rm -rf / --no-preserve-root")
        self.assertTrue(err)
        self.assertIn("blocked", res["content"][0]["text"])

    def test_grep_find_ls_and_bad_args(self):
        (self.ws / "src").mkdir()
        (self.ws / "src" / "m.py").write_text("def route():\n    pass\n")
        (self.ws / "node_modules").mkdir()
        (self.ws / "node_modules" / "x.py").write_text("def route(): pass")
        res, _ = self.tool("grep", pattern="def route")
        self.assertIn("src/m.py", res["content"][0]["text"])
        self.assertNotIn("node_modules", res["content"][0]["text"])
        res, _ = self.tool("find", pattern="*.py")
        self.assertEqual(res["content"][0]["text"], "src/m.py")
        res, _ = self.tool("ls")
        self.assertIn("src/", res["content"][0]["text"])
        _, err = self.tool("edit", path="src/m.py")
        self.assertTrue(err)  # missing required args -> error result, not exception
        _, err = run(T.run_tool("nope", {}, self.ctx, noop))
        self.assertTrue(err)


# ---------------------------------------------------------------------------- agent loop
class TestAgent(WS):
    def agent(self, script, **kw):
        self.events = []

        async def emit(ev):
            self.events.append(ev)
        return Agent(ScriptedProvider(script, delay=0), "m", self.ws, emit=emit, persist=False, **kw)

    def types(self):
        return [e["type"] for e in self.events if e["type"] != "message_update"]

    def test_tool_roundtrip_and_event_order(self):
        s = Script([call("write", path="hi.py", content="print('hi')\n"),
                    call("bash", command="python3 hi.py"), {"text": "It prints hi."}])
        a = self.agent(s)
        out = run(a.run_to_completion("make hi.py"))
        self.assertEqual(out, "It prints hi.")
        self.assertEqual((self.ws / "hi.py").read_text(), "print('hi')\n")
        t = self.types()
        self.assertEqual(t[0], "agent_start")
        self.assertEqual(t[-2:], ["agent_end", "agent_settled"])
        self.assertEqual(t.count("turn_start"), 3)
        self.assertEqual(t.count("tool_execution_end"), 2)
        bash_end = [e for e in self.events if e["type"] == "tool_execution_end" and e["toolName"] == "bash"][0]
        self.assertFalse(bash_end["isError"])
        self.assertEqual(bash_end["result"]["content"][0]["text"].strip(), "hi")
        roles = [m["role"] for m in a.messages]
        self.assertEqual(roles, ["user", "assistant", "toolResult", "assistant", "toolResult", "assistant"])
        deltas = [e for e in self.events if e["type"] == "message_update"]
        kinds = {e["assistantMessageEvent"]["type"] for e in deltas}
        self.assertTrue({"start", "toolcall_start", "toolcall_end", "text_delta", "done"} <= kinds)

    def test_steer_skips_remaining_tools(self):
        async def go():
            state = {}

            def slow_then_more(msgs):
                return {"text": "", "calls": [{"name": "bash", "arguments": {"command": "sleep 0.5"}},
                                              {"name": "write", "arguments": {"path": "x", "content": "x"}}]}
            s = Script([slow_then_more, lambda m: {"text": "steered: " + message_text(m[-1])}])
            a = self.agent(s)
            await a.prompt("start")
            await asyncio.sleep(0.2)
            disp = await a.prompt("change of plan", "steer")
            state["disp"] = disp
            await a.wait_idle()
            return a, state
        a, state = run(go())
        self.assertEqual(state["disp"], "queued_steer")
        self.assertFalse((self.ws / "x").exists())
        skipped = [e for e in self.events if e["type"] == "tool_execution_end" and e["toolName"] == "write"][0]
        self.assertTrue(skipped["isError"])
        self.assertIn("Skipped due to queued user message", skipped["result"]["content"][0]["text"])
        self.assertEqual(a.last_output, "steered: change of plan")
        self.assertIn("queue_update", self.types())

    def test_follow_up_runs_after_settle_and_busy_prompt_rejected(self):
        async def go():
            s = Script([call("bash", command="sleep 0.3"), {"text": "first"}, {"text": "second"}])
            a = self.agent(s)
            await a.prompt("one")
            await asyncio.sleep(0.05)
            with self.assertRaises(RuntimeError):
                await a.prompt("no behavior")
            await a.follow_up("two")
            await a.wait_idle()
            return a
        a = run(go())
        texts = [message_text(m) for m in a.messages if m["role"] == "assistant"]
        self.assertEqual(texts[-2:], ["first", "second"])
        self.assertEqual(self.types().count("agent_start"), 1)

    def test_abort_kills_bash(self):
        async def go():
            a = self.agent(Script([call("bash", command="sleep 30")]))
            await a.prompt("go")
            await asyncio.sleep(0.4)
            t0 = time.time()
            await a.abort()
            return a, time.time() - t0
        a, dt = run(go())
        self.assertLess(dt, 3)
        self.assertFalse(a.is_streaming)
        end = [e for e in self.events if e["type"] == "tool_execution_end"][0]
        self.assertTrue(end["isError"])

    def test_compaction_keeps_tool_pairs(self):
        steps = []
        for i in range(6):
            steps += [call("ls"), {"text": f"answer {i} " + "x" * 400}]
        a = self.agent(Script(steps + [{"text": "summary: did six things"}]), context_limit=100000)
        for i in range(6):
            run(a.run_to_completion(f"q{i}"))
        n_before = len(a.messages)
        info = run(a.compact())
        self.assertTrue(info["compacted"])
        msgs = a.messages
        self.assertLess(len(msgs), n_before)
        self.assertIn("[Conversation summary]", message_text(msgs[0]))
        ids = {b["id"] for m in msgs if m["role"] == "assistant" for b in m["content"] if b["type"] == "toolCall"}
        for m in msgs:
            if m["role"] == "toolResult":
                self.assertIn(m["toolCallId"], ids)  # no orphaned tool results

    def test_checkpoint_rewind_keeps_lesson(self):
        s = Script([call("checkpoint", label="pre"), call("ls"),
                    call("rewind", label="pre", lesson="ls is useless here; read main.py instead"),
                    {"text": "ok after rewind"}])
        a = self.agent(s)
        out = run(a.run_to_completion("explore"))
        self.assertEqual(out, "ok after rewind")
        texts = [message_text(m) for m in a.messages]
        self.assertTrue(any("Lesson from the discarded branch" in t for t in texts))
        self.assertFalse(any(m["role"] == "toolResult" and m["toolName"] == "ls" for m in a.messages))

    def test_task_subagents_parallel(self):
        def brain(messages, system=""):
            last = messages[-1]
            if "ROOT" in system and last["role"] == "user":
                return {"text": "", "calls": [{"name": "task", "arguments": {"tasks": [
                    {"prompt": "count py files", "role": "researcher"}, {"prompt": "say hi", "role": "builder"}]}}]}
            if last["role"] == "toolResult" and last["toolName"] == "task":
                return {"text": "merged: " + message_text(last)[:40]}
            return {"text": "sub done: " + message_text(last)}
        a = self.agent(brain, role_prompt="ROOT", role="ROOT")
        run(a.run_to_completion("fan out"))
        res = [m for m in a.messages if m["role"] == "toolResult" and m["toolName"] == "task"][0]
        results = res["details"]["results"]
        self.assertEqual([r["role"] for r in results], ["RESEARCHER", "BUILDER"])
        self.assertTrue(all(r["ok"] for r in results))
        self.assertTrue(any(e["type"] == "subagent_event" for e in self.events))

    def test_json_text_toolcall_salvage(self):
        s = Script([{"text": '{"name": "ls", "parameters": {"path": "."}}'}, {"text": "listed"}])
        a = self.agent(s)
        run(a.run_to_completion("ls please"))
        self.assertTrue(any(m["role"] == "toolResult" and m["toolName"] == "ls" for m in a.messages))

    def test_session_persist_and_reload(self):
        d = self.ws / "sessions"
        a = Agent(ScriptedProvider(Script([call("ls"), {"text": "fin"}]), delay=0), "m", self.ws,
                  session_dir=d, persist=True)
        run(a.run_to_completion("hello"))
        f = a.session.file
        head = json.loads(f.read_text().splitlines()[0])
        self.assertEqual(head["type"], "session")
        s2 = load_session(f)
        self.assertEqual([m["role"] for m in s2.messages()], [m["role"] for m in a.messages])

    def test_agents_md_loaded(self):
        (self.ws / "AGENTS.md").write_text("Always use tabs.")
        a = self.agent(Script([]))
        self.assertIn("Always use tabs.", a.system_prompt())


# ---------------------------------------------------------------------------- providers vs fake servers
class FakeLLM(BaseHTTPRequestHandler):
    requests = []

    def log_message(self, *a):
        pass

    def do_GET(self):
        body = json.dumps({"models": [{"name": "llama3.1:8b"}]}).encode()
        self.send_response(200)
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        req = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        FakeLLM.requests.append((self.path, req))
        self.send_response(200)
        self.end_headers()
        if "streamGenerateContent" in self.path:  # Gemini SSE
            has_tool_result = any("functionResponse" in p for c in req.get("contents", [])
                                  for p in c.get("parts", []))
            if has_tool_result:
                chunks = [{"candidates": [{"content": {"parts": [{"text": "Done."}]},
                                          "finishReason": "STOP"}]},
                          {"usageMetadata": {"promptTokenCount": 20, "candidatesTokenCount": 2}}]
            else:
                chunks = [{"candidates": [{"content": {"parts": [
                    {"functionCall": {"name": "ls", "args": {"path": "."}}}]}}]},
                          {"usageMetadata": {"promptTokenCount": 15, "candidatesTokenCount": 5}}]
            for c in chunks:
                self.wfile.write(f"data: {json.dumps(c)}\n\n".encode())
            return
        has_tool_result = any(m["role"] == "tool" for m in req["messages"])
        if self.path == "/api/chat":  # Ollama NDJSON
            if has_tool_result:
                chunks = [{"message": {"content": "Found "}}, {"message": {"content": "it."}},
                          {"done": True, "done_reason": "stop", "prompt_eval_count": 50, "eval_count": 3}]
            else:
                chunks = [{"message": {"content": "", "tool_calls": [
                    {"function": {"name": "ls", "arguments": {"path": "."}}}]}},
                    {"done": True, "prompt_eval_count": 40, "eval_count": 9}]
            for c in chunks:
                self.wfile.write((json.dumps(c) + "\n").encode())
                self.wfile.flush()
        else:  # OpenAI SSE, tool args split across chunks
            if has_tool_result:
                deltas = [{"choices": [{"delta": {"content": "Done."}, "finish_reason": "stop"}]},
                          {"choices": [], "usage": {"prompt_tokens": 30, "completion_tokens": 2}}]
            else:
                deltas = [
                    {"choices": [{"delta": {"tool_calls": [{"index": 0, "id": "call_1",
                                                            "function": {"name": "ls", "arguments": '{"pa'}}]}}]},
                    {"choices": [{"delta": {"tool_calls": [{"index": 0, "function": {"arguments": 'th": "."}'}}]},
                                  "finish_reason": "tool_calls"}]}]
            for d in deltas:
                self.wfile.write(f"data: {json.dumps(d)}\n\n".encode())
            self.wfile.write(b"data: [DONE]\n\n")


class TestProviders(WS):
    @classmethod
    def setUpClass(cls):
        cls.srv = ThreadingHTTPServer(("127.0.0.1", 0), FakeLLM)
        cls.port = cls.srv.server_address[1]
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()

    def _roundtrip(self, provider):
        FakeLLM.requests.clear()
        a = Agent(provider, "llama3.1:8b", self.ws, persist=False)
        out = run(a.run_to_completion("what's here?"))
        return a, out

    def test_ollama_native_tools(self):
        a, out = self._roundtrip(OllamaProvider(f"http://127.0.0.1:{self.port}"))
        self.assertEqual(out, "Found it.")
        path, req = FakeLLM.requests[0]
        self.assertEqual(path, "/api/chat")
        self.assertIn("ls", [t["function"]["name"] for t in req["tools"]])
        second = FakeLLM.requests[1][1]["messages"]
        self.assertEqual(second[-1]["role"], "tool")
        self.assertEqual(second[-2]["tool_calls"][0]["function"]["name"], "ls")
        self.assertEqual(a.total_usage["totalTokens"], 40 + 9 + 50 + 3)

    def test_openai_sse_split_args(self):
        a, out = self._roundtrip(OpenAIProvider(f"http://127.0.0.1:{self.port}/v1", "k"))
        self.assertEqual(out, "Done.")
        tr = [m for m in a.messages if m["role"] == "toolResult"][0]
        self.assertFalse(tr["isError"])
        second = FakeLLM.requests[1][1]["messages"]
        self.assertEqual(second[-1], {"role": "tool", "tool_call_id": "call_1",
                                      "content": message_text(tr)})

    def test_gemini_sse_function_call(self):
        p = GeminiProvider(api_key="k")
        p.base = f"http://127.0.0.1:{self.port}"
        a, out = self._roundtrip(p)
        self.assertEqual(out, "Done.")
        path, req = FakeLLM.requests[0]
        self.assertIn("streamGenerateContent", path)
        self.assertIn("alt=sse", path)
        first_call_msg = [m for m in a.messages if m["role"] == "assistant"][0]
        self.assertEqual(first_call_msg["stopReason"], "toolUse")
        second = FakeLLM.requests[1][1]["contents"]
        self.assertEqual(second[-1]["role"], "user")
        self.assertEqual(second[-1]["parts"][0]["functionResponse"]["name"], "ls")

    def test_provider_down_is_error_not_crash(self):
        a = Agent(OllamaProvider("http://127.0.0.1:9"), "m", self.ws, persist=False)
        run(a.run_to_completion("hi"))
        last = a.messages[-1]
        self.assertEqual(last["stopReason"], "error")
        self.assertFalse(a.is_streaming)


# ---------------------------------------------------------------------------- RPC over stdio
class TestRPC(WS):
    def test_pi_rpc_protocol(self):
        cmds = [{"id": "s", "type": "get_state"}, {"id": "p", "type": "prompt", "message": "hi"},
                {"id": "f", "type": "prompt", "message": "@file.txt"}]
        p = subprocess.Popen([sys.executable, "-m", "harness", "--mode", "rpc", "--provider", "mock",
                              "--no-session", "--cwd", str(self.ws)], cwd=ROOT,
                             stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
        for c in cmds:
            p.stdin.write(json.dumps(c) + "\n")
        p.stdin.write("{not json\n")
        p.stdin.flush()
        lines = []
        deadline = time.time() + 20
        while time.time() < deadline:
            ln = p.stdout.readline()
            if not ln:
                break
            lines.append(json.loads(ln))
            if lines[-1].get("type") == "agent_settled":
                break
        p.stdin.write(json.dumps({"id": "m", "type": "get_messages"}) + "\n")
        p.stdin.close()
        lines += [json.loads(l) for l in p.stdout.read().splitlines() if l.strip()]
        p.wait(10)
        p.stdout.close()
        resp = {l.get("id", "parse"): l for l in lines if l["type"] == "response"}
        self.assertTrue(resp["s"]["success"])
        self.assertIn("sessionId", resp["s"]["data"])
        self.assertEqual(resp["p"]["data"]["disposition"], "started")
        self.assertFalse(resp["f"]["success"])
        self.assertEqual(resp["parse"]["command"], "parse")
        self.assertFalse(resp["parse"]["success"])
        self.assertGreaterEqual(len(resp["m"]["data"]["messages"]), 4)
        ev = [l["type"] for l in lines if l["type"] != "response"]
        self.assertEqual(ev[0], "agent_start")
        self.assertIn("tool_execution_start", ev)
        self.assertLess(ev.index("agent_end"), ev.index("agent_settled"))


if __name__ == "__main__":
    unittest.main()
