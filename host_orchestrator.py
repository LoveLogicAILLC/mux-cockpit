"""
host_orchestrator.py
Mac Mini M4 • Ollama (or any OpenAI-compatible server) • MUX host-driven swarm

Every worker is a Pi-compatible harness Agent (harness/) with a persistent session, real
tools and a role overlay. The host owns scheduling:

  AgentsRoom NEEDS INPUT -> submit(goal) -> PLANNER (CH0 INPUT) -> steps fanned out on MUX
  implement/design steps -> BUILDER (CH2 TOOL)        research steps -> RESEARCHER/PLANNER (CH0)
  every TOOL result      -> CRITIC  (CH1 CONTEXT)     score >= 6 -> git commit, else archive
  AgentsRoom TO REVIEW   == CH1 CONTEXT               SEEN == parked + checkpointed to NVMe

Run:  python3 host_orchestrator.py serve            (host + cockpit socket)
      python3 host_orchestrator.py submit "goal"    (one-shot, prints results)
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Deque, Dict, List, Optional

from harness.agent import Agent
from harness.providers import make_provider
from harness.roles import ROLE_PROMPTS
from morph_engine import RoleMorphEngine, lora_for, parse_fix, parse_plan, parse_score
from mux_router import WORKER_IDS, Channel, MuxRouter, Task

log = logging.getLogger("mux.host")

DEFAULT_ROLES = {
    "planner": "PLANNER", "builder": "BUILDER", "critic": "CRITIC", "security": "SECURITY_ENGINEER",
    "devops": "DEVOPS_ENGINEER", "researcher": "RESEARCHER", "xr": "XR_COCKPIT",
}


@dataclass
class AgentWorker:
    id: str
    role: str
    agent: Agent
    lora: Optional[str] = None
    status: str = "idle"  # idle, running, parked, checkpointed
    current: Optional[str] = None
    completed: int = 0
    last_score: Optional[float] = None
    last_output: str = ""
    last_error: Optional[str] = None

    @property
    def tokens_used(self) -> int:
        return self.agent.total_usage["totalTokens"]

    @property
    def context_tokens(self) -> int:
        return self.agent.context_tokens()


class HostOrchestrator:
    def __init__(self, root: str | Path = "./memory", workspace: str | Path = ".", provider=None,
                 model: Optional[str] = None, tokens_per_hour: int = 80000,
                 worker_tokens_per_hour: int = 20000, context_limit: int = 16384,
                 commit_threshold: float = 6.0, max_retries: Optional[int] = None):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.workspace = Path(workspace).resolve()
        self.provider = provider or make_provider()
        self.model = model or os.environ.get("MUX_MODEL", "llama3.1:8b-instruct-q5_K_M")
        self.router = MuxRouter(self.root, tokens_per_hour=worker_tokens_per_hour)
        self.morph = RoleMorphEngine(self.provider, self.model)
        self.quota = {"tokens_per_hour": tokens_per_hour, "context_limit": context_limit}
        self.commit_threshold = commit_threshold
        self.max_retries = max_retries if max_retries is not None else int(os.environ.get("MUX_MAX_RETRIES", "2"))
        self.events: Deque[str] = deque(maxlen=200)
        self.commits: Deque[str] = deque(maxlen=50)
        self.listeners: List[Callable] = []
        self.started = time.time()
        self._hour_window: Deque[tuple] = deque()
        self._loops: List[asyncio.Task] = []
        # Serializes the whole execute->review->commit/retry/archive lifecycle for
        # TOOL-channel tasks (builder/security/devops file mutation), so a concurrent
        # task's uncommitted edits can never be swept into another task's `git add -A`.
        # ponytail: global lock, not per-file/per-worktree -> builder/security/devops
        # can't mutate files concurrently even on unrelated tasks. Upgrade to per-task
        # git worktrees if that throughput loss matters. Also: under heavy load,
        # select_worker's fallback could route a review to a worker queued behind a
        # lock-blocked TOOL task ahead of it (priority order favors TOOL), deadlocking
        # until that worker's queue drains on its own -- not solved here.
        self._workspace_lock = asyncio.Lock()
        self._lock_held_by: Optional[str] = None
        self.results: Dict[str, dict] = {}
        self.workers: Dict[str, AgentWorker] = {}
        for wid in WORKER_IDS:
            role = DEFAULT_ROLES[wid]
            agent = Agent(self.provider, self.model, self.workspace, emit=self._emitter(wid), role=role,
                          role_prompt=ROLE_PROMPTS[role], session_dir=self.root / "sessions" / wid,
                          name=wid, context_limit=context_limit,
                          tools=None if role in ("BUILDER", "PLANNER", "DEVOPS_ENGINEER", "XR_COCKPIT")
                          else ["read", "grep", "find", "ls", "bash", "todo"])
            model_tag, lora = lora_for(role, self.provider, self.model)
            agent.model = model_tag
            self.workers[wid] = AgentWorker(id=wid, role=role, agent=agent, lora=lora)

    # ------------------------------------------------------------------ events
    def _emitter(self, wid: str):
        async def emit(ev: dict):
            t = ev["type"]
            line = None
            if t == "tool_execution_start":
                line = f"{wid} → {ev['toolName']} {json.dumps(ev['args'])[:70]}"
            elif t == "tool_execution_end" and ev["isError"]:
                line = f"{wid} ✗ {ev['toolName']}: {ev['result']['content'][0]['text'][:70]}"
            elif t == "compaction_end":
                line = f"{wid} compacted {ev.get('tokensBefore')}→{ev.get('tokensAfter')} tok"
            elif t == "error":
                line = f"{wid} ERROR {ev.get('error')}"
                w = self.workers.get(wid)
                if w is not None:
                    w.last_error = ev.get("error")
            elif t == "subagent_event" and ev["event"]["type"] == "tool_execution_start":
                line = f"{wid}/{ev['role'].lower()} → {ev['event']['toolName']}"
            if line:
                self.note(line)
            for fn in list(self.listeners):
                try:
                    await fn({"worker": wid, **ev})
                except Exception:
                    pass
        return emit

    def note(self, line: str) -> None:
        stamp = time.strftime("%H:%M:%S")
        self.events.append(f"{stamp} {line}")
        log.info(line)

    # ------------------------------------------------------------------ quota
    def _spend(self, n: int) -> None:
        now = time.time()
        self._hour_window.append((now, n))
        while self._hour_window and now - self._hour_window[0][0] > 3600:
            self._hour_window.popleft()

    def tokens_last_hour(self) -> int:
        now = time.time()
        return sum(n for t, n in self._hour_window if now - t <= 3600)

    # ------------------------------------------------------------------ API
    async def submit(self, goal: str, priority: str = "high") -> str:
        """AgentsRoom NEEDS INPUT -> CH0 INPUT -> planner."""
        task = Task(id=uuid.uuid4().hex[:8], goal=goal, priority=priority, action="plan")
        wid = self.router.select_worker(Channel.INPUT, priority)
        if wid != "planner":
            wid = "planner" if not self.router.workers["planner"].parked else wid
        state = await self.router.route(task, wid, Channel.INPUT)
        self.results[task.id] = {"goal": goal, "status": "queued", "steps": []}
        self.note(f"submit {task.id} → {wid} ({state}): {goal[:60]}")
        return task.id

    async def morph_worker(self, worker_id: str, new_role: str, keep_context: bool = True) -> AgentWorker:
        w = self._w(worker_id)
        await self.morph.morph(w, new_role, keep_context)
        self.note(f"morph {worker_id} → {w.role} (lora {w.lora or '-'}, ctx kept={keep_context})")
        return w

    async def park(self, worker_id: str) -> int:
        w = self._w(worker_id)
        # Mark parked before aborting: if a task is mid-flight, its run_to_completion()
        # may resume (via abort or natural finish) before this coroutine gets back around
        # to router.park() below. Setting parked=True first guarantees _worker_loop's
        # `if wq.parked:` guard sees it and re-spills instead of treating the aborted
        # output as a normal completion.
        self.router.workers[worker_id].parked = True
        if w.agent.is_streaming:
            await w.agent.abort()
        n = await self.router.park(worker_id)
        await self.checkpoint(worker_id)
        w.status = "parked"
        self.note(f"park {worker_id}: {n} task(s) → NVMe")
        return n

    async def unpark(self, worker_id: str) -> int:
        w = self._w(worker_id)
        n = await self.router.unpark(worker_id)
        w.status = "idle"
        self.note(f"unpark {worker_id}: {n} task(s) restored")
        return n

    async def checkpoint(self, worker_id: str) -> str:
        w = self._w(worker_id)
        snap = {"worker": w.id, "role": w.role, "lora": w.lora, "tokens": w.tokens_used,
                "context_tokens": w.context_tokens, "session": str(w.agent.session.file),
                "leaf": w.agent.session.leaf, "time": time.time()}
        d = self.root / "checkpoints"
        d.mkdir(exist_ok=True)
        p = d / f"{w.id}_{int(time.time() * 1000)}.json"
        await asyncio.to_thread(p.write_text, json.dumps(snap, indent=2))
        self.note(f"checkpoint {worker_id} → {p.name}")
        return str(p)

    def _w(self, wid: str) -> AgentWorker:
        if wid not in self.workers:
            raise KeyError(f"unknown worker {wid}; valid: {', '.join(self.workers)}")
        return self.workers[wid]

    # ------------------------------------------------------------------ execution
    async def _worker_loop(self, w: AgentWorker) -> None:
        wq = self.router.workers[w.id]
        while True:
            task = await wq.dequeue()
            try:
                await self._process_task(w, wq, task)
            except Exception as e:
                # A worker loop must never die: an uncaught exception anywhere below would
                # silently stop this worker from ever picking up another task again — nothing
                # supervises or restarts it (asyncio.create_task in start() is fire-and-forget).
                # Log it, release any lock this task still held, reset status so the worker
                # reads as idle again (not stuck "running"), and keep the loop alive.
                self.note(f"{w.id} loop error on {task.id}: {type(e).__name__}: {e}")
                w.status, w.current = "error", None
                if self._lock_held_by == task.id:
                    self._workspace_lock.release()
                    self._lock_held_by = None

    async def _process_task(self, w: AgentWorker, wq, task: Task) -> None:
        is_tool = task.channel_hint == Channel.TOOL
        if self.tokens_last_hour() >= self.quota["tokens_per_hour"]:
            await self.router.route(task, w.id, task.channel_hint)  # spills (worker parked below)
            await self.park(w.id)
            self.note(f"host quota hit ({self.tokens_last_hour()}/h) — parked {w.id}")
            if is_tool and self._lock_held_by == task.id:
                self._workspace_lock.release()
                self._lock_held_by = None
            return
        if is_tool and self._lock_held_by != task.id:
            # Fresh TOOL task (not a same-id retry continuing an already-held lock):
            # wait for exclusive workspace access before touching any files.
            await self._workspace_lock.acquire()
            self._lock_held_by = task.id
        w.status, w.current = "running", task.id
        before = w.tokens_used
        w.last_error = None
        failed = False
        try:
            out = await w.agent.run_to_completion(self._prompt_for(w, task))
        except Exception as e:
            out = f"ERROR {type(e).__name__}: {e}"
            failed = True
            self.note(f"{w.id} ERROR on {task.id}: {type(e).__name__}: {e}")
        if w.last_error and not failed:
            # Agent._run() swallows its own exceptions (emits an "error" event, keeps the
            # loop alive) rather than raising, so run_to_completion() returns normally with
            # whatever partial/empty last_output it had. Without this check the task would
            # be miscounted as a real completion and its empty/garbage output would be fed
            # into _after()'s parse_plan/parse_score.
            failed = True
            out = f"ERROR {w.last_error}"
        spent = w.tokens_used - before
        self._spend(spent)
        self.router.token_bucket[w.id].consume(spent)
        w.current = None
        if wq.parked:  # parked mid-task: task was aborted -> spill it back, don't lose it
            self.router._spill(w.id, task.channel_hint, task)
            if is_tool and self._lock_held_by == task.id:
                self._workspace_lock.release()
                self._lock_held_by = None
            return
        if failed:
            # Don't count a provider/agent exception as completed work, and don't feed
            # the error text into _after()'s parse_plan/parse_score — that would silently
            # misinterpret it as real model output. Archive it for traceability instead.
            w.status = "error"
            self._archive(task.id, out, None)
            if is_tool and self._lock_held_by == task.id:
                self._workspace_lock.release()
                self._lock_held_by = None
            return
        w.last_output, w.completed = out, w.completed + 1
        w.status = "idle"
        try:
            await self._after(w, task, out)
        except Exception as e:
            self.note(f"{w.id} post-process error: {e}")
            # _after threw before reaching a commit/retry/archive outcome that would
            # normally release the lock -> release here so a bug in review-handling
            # can't wedge the whole swarm's TOOL channel permanently.
            if is_tool and self._lock_held_by == task.id:
                self._workspace_lock.release()
                self._lock_held_by = None
        if w.context_tokens > self.quota["context_limit"] * 0.9:
            await w.agent.compact("host-quota")
        if self.router.token_bucket[w.id].available < self.router.min_tokens:
            await self.park(w.id)
            w.status = "checkpointed"

    def _prompt_for(self, w: AgentWorker, task: Task) -> str:
        if task.action == "plan":
            return (f"GOAL: {task.goal}\nInspect the workspace, then output the plan JSON "
                    '{"steps":[...]} as the last thing in your reply.')
        if task.action == "review":
            return f"Review this work against its goal. Inspect files/tests as needed.\n{task.goal}"
        return f"[{task.action.upper()}] {task.goal}"

    async def _after(self, w: AgentWorker, task: Task, out: str) -> None:
        if task.action == "plan":
            steps = parse_plan(out) or [
                {"id": i + 1, "action": "implement" if t["status"] != "completed" else "verify", "desc": t["content"]}
                for i, t in enumerate(w.agent.ctx.todos)]
            rec = self.results.setdefault(task.id, {"goal": task.goal, "steps": []})
            rec["status"] = "planned" if steps else "plan_failed"
            self.note(f"plan {task.id}: {len(steps)} step(s)")
            for s in steps:
                ch = Channel.INPUT if s["action"] == "research" else Channel.TOOL
                prio = "medium" if s["action"] == "research" else task.priority
                wid = self.router.select_worker(ch, prio)
                sub = Task(id=f"{task.id}.{s['id']}", goal=s["desc"], priority=task.priority,
                           action=s["action"], parent=task.id)
                rec["steps"].append({"id": sub.id, "action": s["action"], "desc": s["desc"], "worker": wid})
                await self.router.route(sub, wid, ch)
        elif task.action == "review":
            score = parse_score(out)
            w.last_score = score
            parent = task.parent or ""
            target = self.results.get(parent.split(".")[0], {})
            step = next((s for s in target.get("steps", []) if s["id"] == parent), None)
            if step is not None:
                step["score"] = score
            if score is not None and score >= self.commit_threshold:
                self.commits.append(await self._git_commit(f"{parent}: score {score:.1f}"))
                if self._lock_held_by == parent:
                    self._workspace_lock.release()
                    self._lock_held_by = None
            elif step is not None and step.setdefault("retries", 0) < self.max_retries:
                # Recursive self-correction: feed the critic's rejection back to the SAME
                # worker (same id -> its Agent session still remembers the failed attempt)
                # instead of dead-ending into archived.jsonl. Reusing `parent` as the retry
                # task's own id keeps future review rounds matching this same `step` entry,
                # and _worker_loop skips re-acquiring the workspace lock for it (still held
                # continuously from the original attempt through every retry).
                step["retries"] += 1
                fix = parse_fix(out)
                score_txt = f"{score:.1f}" if score is not None else "unscored"
                feedback = (f"CRITIC FEEDBACK: {fix}" if fix else "try a different, more thorough approach.")
                retry = Task(id=parent, goal=f"{step['desc']}\n\nPREVIOUS ATTEMPT SCORED {score_txt}/10 — {feedback}",
                             priority=task.priority, action=step["action"],
                             parent=parent.split(".")[0], retries=step["retries"])
                ch = Channel.INPUT if step["action"] == "research" else Channel.TOOL
                await self.router.route(retry, step["worker"], ch)
                self.note(f"review {parent}: score {score} < {self.commit_threshold} → "
                          f"retry {step['retries']}/{self.max_retries} on {step['worker']}")
            else:
                self._archive(parent, out, score)
                reason = "retries exhausted" if step is not None and step.get("retries") else "no step context"
                self.note(f"review {parent}: score {score} < {self.commit_threshold} → archived ({reason})")
                if self._lock_held_by == parent:
                    self._workspace_lock.release()
                    self._lock_held_by = None
        elif task.channel_hint == Channel.TOOL:
            review = Task(id=f"{task.id}.r", goal=f"GOAL: {task.goal}\n\nWORKER OUTPUT:\n{out[:6000]}",
                          action="review", parent=task.id, priority=task.priority)
            await self.router.route(review, self.router.select_worker(Channel.CONTEXT, task.priority),
                                    Channel.CONTEXT)

    async def _git_commit(self, msg: str) -> str:
        # subprocess.run() blocks; offload so a slow/large git op doesn't stall every
        # other worker's event-loop turn (this coroutine is on the shared loop).
        line = await asyncio.to_thread(self._git_commit_sync, msg)
        self.note(f"commit {line}")
        return line

    def _git_commit_sync(self, msg: str) -> str:
        import subprocess
        line = f"{time.strftime('%H:%M')} {msg}"
        if os.environ.get("MUX_GIT", "0") == "1" and (self.workspace / ".git").exists():
            try:
                subprocess.run(["git", "add", "-A"], cwd=self.workspace, check=True, capture_output=True, timeout=20)
                subprocess.run(["git", "commit", "-m", f"mux: {msg}", "--no-verify"], cwd=self.workspace,
                               check=True, capture_output=True, timeout=20)
                sha = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=self.workspace,
                                     capture_output=True, text=True).stdout.strip()
                line = f"{sha}: {msg}"
            except subprocess.CalledProcessError as e:
                line = f"(no commit: {(e.stderr or b'').decode()[:60].strip() or 'nothing to commit'}) {msg}"
        return line

    def _archive(self, task_id: str, out: str, score) -> None:
        with open(self.root / "archived.jsonl", "a") as f:
            f.write(json.dumps({"t": time.time(), "task": task_id, "score": score, "output": out[:2000]}) + "\n")

    # ------------------------------------------------------------------ lifecycle
    async def start(self) -> None:
        if not self._loops:
            self._loops = [asyncio.create_task(self._worker_loop(w), name=f"worker-{w.id}")
                           for w in self.workers.values()]
            self.note(f"host up • provider={self.provider.name} model={self.model} • {len(self.workers)} workers")

    async def stop(self) -> None:
        for w in self.workers.values():
            if w.agent.is_streaming:
                await w.agent.abort()
        for t in self._loops:
            t.cancel()
        await asyncio.gather(*self._loops, return_exceptions=True)
        self._loops = []

    async def overnight_loop(self, cadence: float = 900) -> None:
        """Keeps workers alive; every cadence: checkpoint running workers, unpark refilled ones."""
        await self.start()
        while True:
            await asyncio.sleep(cadence)
            for wid, w in self.workers.items():
                try:
                    if w.status in ("parked", "checkpointed") and \
                            self.router.token_bucket[wid].available >= self.router.min_tokens * 4 and \
                            self.tokens_last_hour() < self.quota["tokens_per_hour"]:
                        await self.unpark(wid)
                    elif w.status == "running":
                        await self.checkpoint(wid)
                except Exception as e:  # one bad worker must not kill the loop
                    self.note(f"overnight {wid}: {e}")

    async def wait_quiet(self, settle: float = 1.0, timeout: float = 600) -> None:
        """Wait until all queues are empty and no worker is running."""
        end = time.time() + timeout
        quiet_since = None
        while time.time() < end:
            busy = self.router.total_depth() or any(w.agent.is_streaming or w.status == "running"
                                                    for w in self.workers.values())
            if busy:
                quiet_since = None
            else:
                quiet_since = quiet_since or time.time()
                if time.time() - quiet_since >= settle:
                    return
            await asyncio.sleep(0.1)
        raise TimeoutError("swarm did not settle")

    # ------------------------------------------------------------------ telemetry
    def status(self) -> dict:
        tl = self.tokens_last_hour()
        ctx_max = max((w.context_tokens for w in self.workers.values()), default=0)
        running = sum(1 for w in self.workers.values() if w.status == "running")
        cps = len(list((self.root / "checkpoints").glob("*.json"))) if (self.root / "checkpoints").exists() else 0

        def st(used, lim):
            r = used / lim if lim else 0
            return "exhausted" if r >= 1 else "near-limit" if r >= 0.75 else "healthy"

        return {
            "host": f"HOST: {os.uname().nodename} • provider {self.provider.name} • model {self.model}",
            "provider": self.provider.name,
            "uptime": int(time.time() - self.started),
            "mux_depth": self.router.total_depth(),
            "workers": [{
                "id": w.id, "role": w.role, "status": w.status,
                "tokens": w.tokens_used, "context": w.context_tokens,
                "load": min(100, int(100 * self.router.workers[w.id].depth() / self.router.workers[w.id].max_depth)),
                "lora": w.lora or "-", "spilled": self.router.spilled_count(w.id),
                "completed": w.completed, "score": w.last_score, "current": w.current,
                "todos": w.agent.ctx.todos[-6:],
            } for w in self.workers.values()],
            "channels": self.router.channel_stats(),
            "quota": [
                {"resource": "Tokens / hour", "used": tl, "limit": self.quota["tokens_per_hour"],
                 "state": st(tl, self.quota["tokens_per_hour"])},
                {"resource": "Context (max)", "used": ctx_max, "limit": self.quota["context_limit"],
                 "state": st(ctx_max, self.quota["context_limit"])},
                {"resource": "Workers busy", "used": running, "limit": len(self.workers),
                 "state": st(running, len(self.workers))},
                {"resource": "Checkpoints", "used": cps, "limit": 0, "state": "NVMe"},
            ],
            "events": list(self.events)[-14:],
            "git_log": list(self.commits)[-6:],
            "roles": list(ROLE_PROMPTS),
        }


# ---------------------------------------------------------------------- CLI
async def _serve(args) -> None:
    from cockpit_integration import CockpitBridge
    host = HostOrchestrator(args.memory, args.workspace)
    bridge = CockpitBridge(host, args.sock)
    await asyncio.gather(host.overnight_loop(args.cadence), bridge.start_server())


async def _submit(args) -> None:
    host = HostOrchestrator(args.memory, args.workspace)
    await host.start()
    tid = await host.submit(args.goal)
    await host.wait_quiet(timeout=args.timeout)
    await host.stop()
    print(json.dumps({"task": tid, **host.results.get(tid, {}), "events": list(host.events)}, indent=2))


def main():
    ap = argparse.ArgumentParser(description="MUX host orchestrator")
    ap.add_argument("cmd", choices=["serve", "submit"], nargs="?", default="serve")
    ap.add_argument("goal", nargs="?")
    ap.add_argument("--memory", default=os.environ.get("MUX_MEMORY", "./memory"))
    ap.add_argument("--workspace", default=os.environ.get("MUX_WORKSPACE", "."))
    ap.add_argument("--sock", default=os.environ.get("MUX_SOCK", "/tmp/mux_host.sock"))
    ap.add_argument("--cadence", type=float, default=900)
    ap.add_argument("--timeout", type=float, default=1800)
    args = ap.parse_args()
    Path(args.memory).mkdir(parents=True, exist_ok=True)
    logging.basicConfig(filename=str(Path(args.memory) / "host.log"), level=logging.INFO,
                        format="%(asctime)s %(name)s %(message)s")  # never stdout: TUI owns the tty
    if args.cmd == "submit":
        if not args.goal:
            ap.error("submit needs a goal")
        asyncio.run(_submit(args))
    else:
        asyncio.run(_serve(args))


if __name__ == "__main__":
    main()
