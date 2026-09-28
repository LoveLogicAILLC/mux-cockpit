"""MUX host + bridge integration tests (mock provider, real tools, real git, real socket)."""
import asyncio
import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ["MUX_PROVIDER"] = "mock"

from cockpit_integration import CockpitBridge  # noqa: E402
from harness.providers import ScriptedProvider  # noqa: E402
from host_orchestrator import HostOrchestrator  # noqa: E402
from mux_router import Channel, Task  # noqa: E402


class HostTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ws = Path(self.tmp.name)
        subprocess.run("git init -q && git config user.email t@t && git config user.name t && "
                       "echo x > a.txt && git add -A && git commit -qm init", shell=True, cwd=self.ws, check=True)

    def tearDown(self):
        self.tmp.cleanup()

    def host(self, **kw):
        return HostOrchestrator(self.ws / ".mux", self.ws, provider=ScriptedProvider(delay=0), **kw)

    def test_swarm_plan_fanout_review_commit(self):
        os.environ["MUX_GIT"] = "1"

        async def go():
            h = self.host()
            await h.start()
            tid = await h.submit("add a health endpoint")
            await h.wait_quiet(timeout=60)
            await h.stop()
            return h, tid
        try:
            h, tid = asyncio.run(go())
        finally:
            os.environ.pop("MUX_GIT")
        rec = h.results[tid]
        self.assertEqual(rec["status"], "planned")
        self.assertEqual([s["action"] for s in rec["steps"]], ["research", "implement"])
        self.assertEqual(rec["steps"][0]["worker"], "researcher")  # medium-priority research -> researcher
        self.assertEqual(rec["steps"][1]["worker"], "builder")
        self.assertIsNotNone(rec["steps"][1].get("score"))  # TOOL output was critic-reviewed
        log = subprocess.run(["git", "log", "--oneline"], cwd=self.ws, capture_output=True, text=True).stdout
        archived = (self.ws / ".mux" / "archived.jsonl")
        score = rec["steps"][1]["score"]
        if score >= 6:
            self.assertIn("mux:", log)
        else:
            self.assertTrue(archived.exists())
        st = h.status()
        self.assertEqual(len(st["workers"]), 7)
        self.assertEqual({c["channel"] for c in st["channels"]}, {"INPUT", "CONTEXT", "TOOL"})

    def test_park_spills_and_unpark_restores_without_loss(self):
        async def go():
            h = self.host()  # loops NOT started -> tasks stay queued
            for i in range(5):
                await h.router.route(Task(id=f"t{i}", goal=f"g{i}", action="implement"), "builder", Channel.TOOL)
            self.assertEqual(h.router.workers["builder"].depth(), 5)
            flushed = await h.park("builder")
            st = await h.router.route(Task(id="late", goal="late"), "builder", Channel.TOOL)
            spilled = h.router.spilled_count("builder")
            restored = await h.unpark("builder")
            return h, flushed, st, spilled, restored
        h, flushed, st, spilled, restored = asyncio.run(go())
        self.assertEqual(flushed, 5)
        self.assertEqual(st, "spilled")  # routing to a parked worker spills, never drops
        self.assertEqual(spilled, 6)
        self.assertEqual(restored, 6)
        self.assertEqual(h.router.workers["builder"].depth(), 6)
        self.assertTrue(list((self.ws / ".mux" / "checkpoints").glob("builder_*.json")))

    def test_unpark_skips_corrupted_line_without_losing_others(self):
        """A truncated/corrupted line in parked/<id>.jsonl (e.g. a write cut short by a crash
        mid-append) must not poison the whole restore -- every other valid spilled task must
        still come back, and the worker must still leave the parked state."""
        async def go():
            h = self.host()
            await h.router.route(Task(id="good1", goal="g1", action="implement"), "builder", Channel.TOOL)
            await h.router.route(Task(id="good2", goal="g2", action="implement"), "builder", Channel.TOOL)
            await h.park("builder")
            p = self.ws / ".mux" / "parked" / "builder.jsonl"
            with open(p, "a") as f:
                f.write('{"channel": 2, "task": {"id": "trunc"')  # cut off mid-write, invalid JSON
            restored = await h.unpark("builder")
            return h, restored
        h, restored = asyncio.run(go())
        self.assertEqual(restored, 2)  # both well-formed tasks survive the corrupted neighbor
        self.assertEqual(h.router.workers["builder"].depth(), 2)
        self.assertFalse(h.router.workers["builder"].parked)



    def test_worker_exception_is_archived_not_counted_as_success(self):
        def boom(messages, system=""):
            raise RuntimeError("simulated provider outage")

        async def go():
            h = HostOrchestrator(self.ws / ".mux", self.ws, provider=ScriptedProvider(script=boom, delay=0))
            await h.start()
            await h.router.route(Task(id="t1", goal="do it", action="implement"), "builder", Channel.TOOL)
            await h.wait_quiet(timeout=15)
            await h.stop()
            return h
        h = asyncio.run(go())
        w = h.workers["builder"]
        self.assertEqual(w.status, "error")
        self.assertEqual(w.completed, 0)  # a swallowed agent-level error must not count as done
        self.assertTrue((self.ws / ".mux" / "archived.jsonl").exists())
        # never reached the critic: no review/score noise for output that was never real
        self.assertFalse(any("review" in e for e in h.events))

    def test_worker_loop_survives_unexpected_exception(self):
        """A stray exception anywhere in per-task processing -- not just the two spots that
        already have their own try/except (run_to_completion, _after) -- must not permanently
        kill that worker's asyncio loop task, and must not leave the workspace lock stuck held
        forever (which would deadlock the whole TOOL channel across every worker)."""
        async def go():
            h = HostOrchestrator(self.ws / ".mux", self.ws, provider=ScriptedProvider(delay=0))
            await h.start()
            w = h.workers["builder"]
            bucket = h.router.token_bucket["builder"]
            orig_consume = bucket.consume
            calls = {"n": 0}

            def flaky_consume(n):
                calls["n"] += 1
                if calls["n"] == 1:
                    raise RuntimeError("simulated bug outside the known try/except blocks")
                return orig_consume(n)
            bucket.consume = flaky_consume
            await h.router.route(Task(id="t1", goal="do it", action="implement"), "builder", Channel.TOOL)
            await h.wait_quiet(timeout=15)  # first task's exception must not hang the swarm
            await h.router.route(Task(id="t2", goal="do it again", action="implement"), "builder", Channel.TOOL)
            await h.wait_quiet(timeout=15)  # second task needs the SAME lock t1 held -> proves it was released
            await h.stop()
            return h, w
        h, w = asyncio.run(go())
        self.assertTrue(any("loop error on t1" in e for e in h.events))
        self.assertGreaterEqual(w.completed, 1)  # t2 (and its downstream review) actually ran



    def test_review_rejection_retries_then_archives(self):
        """Critic feedback must actually loop back to the worker (recursive self-correction),
        not dead-end straight into archived.jsonl on the first rejection."""
        import re as _re

        def script(messages, system=""):
            role = (_re.search(r"# Role: (\w+)", system) or [None, ""])[1]
            last = messages[-1]
            if last["role"] == "user":
                return {"text": f"[{role}] working", "calls": [{"name": "ls", "arguments": {"path": "."}}]}
            if role == "PLANNER":
                return {"text": "Plan.\n" + json.dumps({"steps": [
                    {"id": 1, "action": "implement", "desc": "do the thing"}]})}
            if role == "CRITIC":
                return {"text": "Reviewed.\n" + json.dumps(
                    {"score": 3.0, "fails": ["always fails"], "fix": "be more thorough"})}
            return {"text": "done\nSTATUS: done"}

        async def go():
            h = HostOrchestrator(self.ws / ".mux", self.ws,
                                  provider=ScriptedProvider(script=script, delay=0), max_retries=2)
            await h.start()
            tid = await h.submit("add a widget")
            await h.wait_quiet(timeout=60)
            await h.stop()
            return h, tid

        h, tid = asyncio.run(go())
        step = h.results[tid]["steps"][0]
        self.assertEqual(step["action"], "implement")
        self.assertEqual(step["retries"], 2)  # MAX_RETRIES exhausted, not dead-ended on attempt 1
        self.assertEqual(step["score"], 3.0)
        self.assertTrue((self.ws / ".mux" / "archived.jsonl").exists())
        self.assertTrue(any("retry 1/2" in e for e in h.events))
        self.assertTrue(any("retry 2/2" in e for e in h.events))
        self.assertTrue(any("retries exhausted" in e for e in h.events))

    def test_concurrent_tool_tasks_never_mix_commits(self):
        """Two TOOL tasks on different workers race to mutate the SAME shared workspace.
        The serialization lock must (a) prevent their execution windows from overlapping
        and (b) guarantee each one's own file lands in its own commit only \u2014 never swept
        into the other's `git add -A`."""
        os.environ["MUX_GIT"] = "1"
        peak = {"active": 0, "max_seen": 0}

        def make_script(fname, delay):
            def script(messages, system=""):
                import re, time as _t
                role = (re.search(r"# Role: (\w+)", system) or [None, ""])[1]
                if role == "CRITIC":
                    return {"text": "Reviewed.\n" + json.dumps({"score": 8.0})}
                peak["active"] += 1
                peak["max_seen"] = max(peak["max_seen"], peak["active"])
                _t.sleep(delay)
                (self.ws / fname).write_text(f"from {fname}\n")
                peak["active"] -= 1
                return {"text": "done\nSTATUS: done"}
            return script

        async def go():
            h = HostOrchestrator(self.ws / ".mux", self.ws,
                                  provider=ScriptedProvider(script=make_script("a.txt", 0.2), delay=0))
            h.workers["security"].agent.provider = ScriptedProvider(script=make_script("b.txt", 0.2), delay=0)
            await h.start()
            await h.router.route(Task(id="taskA", goal="write a.txt", action="implement"), "builder", Channel.TOOL)
            await h.router.route(Task(id="taskB", goal="write b.txt", action="implement"), "security", Channel.TOOL)
            await h.wait_quiet(timeout=30)
            await h.stop()

        try:
            asyncio.run(go())
        finally:
            os.environ.pop("MUX_GIT")

        self.assertEqual(peak["max_seen"], 1)  # never two TOOL tasks mutating files at once
        commits = subprocess.run(["git", "log", "--format=%H"], cwd=self.ws,
                                  capture_output=True, text=True).stdout.split()
        for sha in commits[:-1]:  # skip setUp's initial commit
            files = subprocess.run(["git", "show", "--name-only", "--format=", sha], cwd=self.ws,
                                    capture_output=True, text=True).stdout.split()
            self.assertFalse("a.txt" in files and "b.txt" in files, f"commit {sha} mixed both tasks' files")

    def test_morph_keeps_context(self):
        async def go():
            h = self.host()
            w = h.workers["xr"]
            await w.agent.run_to_completion("remember: the port is 7777")
            n = len(w.agent.messages)
            await h.morph_worker("xr", "SECURITY_ENGINEER")
            return w, n
        w, n = asyncio.run(go())
        self.assertEqual(w.role, "SECURITY_ENGINEER")
        self.assertEqual(len(w.agent.messages), n)  # nothing lost
        self.assertIn("Role: SECURITY_ENGINEER", w.agent.system_prompt())
        with self.assertRaises(KeyError):
            asyncio.run(self.host().morph_worker("nope", "PLANNER"))

    def test_socket_perms_actions_and_pi_passthrough(self):
        sock = str(self.ws / "t.sock")

        async def rpc(obj):
            r, w = await asyncio.open_unix_connection(sock)
            w.write((json.dumps(obj) + "\n").encode())
            await w.drain()
            line = await r.readline()
            w.close()
            return json.loads(line)

        async def go():
            h = self.host()
            b = CockpitBridge(h, sock)
            srv = asyncio.create_task(b.start_server())
            await h.start()
            for _ in range(50):
                if os.path.exists(sock):
                    break
                await asyncio.sleep(0.05)
            mode = stat.S_IMODE(os.stat(sock).st_mode)
            out = {"mode": mode}
            out["status"] = await rpc({"action": "status"})
            out["bad"] = await rpc({"action": "morph", "worker_id": "builder", "new_role": "WIZARD"})
            out["morph"] = await rpc({"action": "morph", "worker_id": "builder", "new_role": "critic"})
            out["prompt"] = await rpc({"type": "prompt", "worker": "devops", "message": "check disk"})
            await h.workers["devops"].agent.wait_idle()
            out["state"] = await rpc({"type": "get_state", "worker": "devops"})
            out["garbage"] = await rpc("x")
            await h.stop()
            srv.cancel()
            return out
        o = asyncio.run(go())
        self.assertEqual(o["mode"], 0o600)
        self.assertIn("workers", o["status"])
        self.assertEqual(o["bad"]["status"], "error")
        self.assertEqual(o["morph"]["role"], "CRITIC")
        self.assertEqual(o["prompt"]["data"]["disposition"], "started")
        self.assertGreaterEqual(o["state"]["data"]["messageCount"], 3)
        self.assertFalse(o["garbage"]["success"])


if __name__ == "__main__":
    unittest.main()
