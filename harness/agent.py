"""
Agent loop with Pi semantics.

  prompt      -> starts a run (or, while streaming, requires streamingBehavior steer|followUp)
  steer       -> delivered after the current tool finishes; remaining tool calls in that
                 assistant message are skipped ("Skipped due to queued user message.")
  follow_up   -> delivered only when the agent would otherwise stop (one at a time)
  abort       -> cancels the stream and kills running bash process groups
Events emitted (Pi RPC names): agent_start, turn_start, message_start, message_update,
message_end, tool_execution_start/update/end, turn_end, queue_update, compaction_start/end,
agent_end, agent_settled.
"""
from __future__ import annotations

import asyncio
import copy
import datetime as _dt
import os
from pathlib import Path
from typing import Awaitable, Callable, Dict, List, Optional

from . import tools as T
from .protocol import (assistant_message, estimate_tokens, message_text, now_ms, text,
                       tool_calls, tool_result_message, usage, user_message)
from .session import Session

Emit = Callable[[dict], Awaitable[None]]

BASE_PROMPT = """You are an expert coding agent operating inside a real workspace via tools.
Rules:
- Use tools; never invent file contents. Read before you edit.
- Prefer `edit` (exact unique oldText) or `hashline_edit` (anchors from read hashline=true) over rewriting whole files.
- Run code/tests with `bash` to verify. Report failures honestly.
- For multi-step work keep a `todo` list; use `task` to parallelize independent slices.
- Before risky exploration call `checkpoint`; if it dead-ends, `rewind` with a lesson.
- Be concise. When finished, state what changed and how it was verified."""

DANGEROUS = ("rm -rf / ", "rm -rf /*", "rm -rf ~", "mkfs", ":(){", "dd if=/dev/zero of=/dev/",
             "> /dev/sda", "chmod -R 777 /", "shutdown", "reboot")


def default_bash_policy(cmd: str) -> Optional[str]:
    if os.environ.get("HARNESS_BASH_POLICY", "guard") == "off":
        return None
    c = " ".join(cmd.split()) + " "
    for d in DANGEROUS:
        if d in c:
            return f"matches destructive pattern '{d.strip()}'"
    return None


def load_context_files(cwd: Path) -> str:
    """AGENTS.md / CLAUDE.md from global dirs, then from filesystem root down to cwd."""
    found: List[Path] = []
    for g in (Path.home() / ".pi" / "agent" / "AGENTS.md", Path.home() / ".mux" / "AGENTS.md"):
        if g.is_file():
            found.append(g)
    chain = list(reversed([cwd, *cwd.parents]))
    for d in chain:
        for n in ("AGENTS.md", "CLAUDE.md"):
            p = d / n
            if p.is_file() and p not in found:
                found.append(p)
    parts = []
    for p in found:
        try:
            parts.append(f"## {p}\n{p.read_text(encoding='utf-8')[:20000]}")
        except OSError:
            pass
    return "\n\n".join(parts)


class Agent:
    def __init__(self, provider, model: str, cwd: str | Path = ".", *, emit: Optional[Emit] = None,
                 role_prompt: str = "", role: str = "GENERAL", tools: Optional[List[str]] = None,
                 session_dir: Optional[Path] = None, persist: bool = True, name: Optional[str] = None,
                 depth: int = 0, max_depth: int = 1, thinking: str = "off",
                 context_limit: int = 16384, auto_compact: bool = True, max_turns: int = 60,
                 subagent_runner: Optional[Callable[[List[dict]], Awaitable[List[dict]]]] = None,
                 jail: bool = True):
        self.provider = provider
        self.model = model
        self.cwd = Path(cwd).resolve()
        self.emit_cb = emit
        self.role = role
        self.role_prompt = role_prompt
        self.depth, self.max_depth = depth, max_depth
        names = list(tools or T.DEFAULT_TOOLS)
        if depth >= max_depth:
            names = [n for n in names if n != "task"]
        self.tool_names = names
        self.thinking = thinking
        self.context_limit = context_limit
        self.auto_compact = auto_compact
        self.max_turns = max_turns
        self.session = Session(session_dir, str(self.cwd), name=name, persist=persist)
        self.steering: List[str] = []
        self.follow_ups: List[str] = []
        self.abort_event = asyncio.Event()
        self.is_streaming = False
        self.is_compacting = False
        self._run_task: Optional[asyncio.Task] = None
        self._pending_cp: List[str] = []
        self._pending_rewind: Optional[tuple] = None
        self.total_usage = usage()
        self.last_output = ""
        self.ctx = T.ToolContext(cwd=self.cwd, jail=jail, abort=self.abort_event,
                                 spawn_subagents=subagent_runner or self._spawn_local,
                                 mark_checkpoint=self._pending_cp.append,
                                 request_rewind=lambda l, s: setattr(self, "_pending_rewind", (l, s)),
                                 bash_policy=default_bash_policy)
        self._agents_md = load_context_files(self.cwd)

    # ------------------------------------------------------------------ utils
    async def emit(self, ev: dict) -> None:
        if self.emit_cb:
            await self.emit_cb(ev)

    @property
    def messages(self) -> List[dict]:
        return self.session.messages()

    def system_prompt(self) -> str:
        parts = [BASE_PROMPT]
        if self.role_prompt:
            parts.append(f"# Role: {self.role}\n{self.role_prompt}")
        parts.append(f"Workspace: {self.cwd}\nDate: {_dt.date.today().isoformat()}")
        if self._agents_md:
            parts.append("# Project context\n" + self._agents_md)
        return "\n\n".join(parts)

    def set_role(self, role: str, prompt: str) -> None:
        """Morph: swap the role prompt, keep the whole conversation."""
        self.role, self.role_prompt = role, prompt
        self.session.append("role_change", role=role)

    def context_tokens(self) -> int:
        return estimate_tokens(self.messages) + len(self.system_prompt()) // 4

    def state(self) -> dict:
        return {"model": {"id": self.model, "provider": self.provider.name},
                "thinkingLevel": self.thinking, "isStreaming": self.is_streaming,
                "isCompacting": self.is_compacting, "steeringMode": "all",
                "followUpMode": "one-at-a-time",
                "sessionFile": str(self.session.file) if self.session.file else None,
                "sessionId": self.session.id, "sessionName": self.session.name,
                "autoCompactionEnabled": self.auto_compact, "messageCount": len(self.messages),
                "pendingMessageCount": len(self.steering) + len(self.follow_ups),
                "role": self.role, "contextTokens": self.context_tokens(),
                "contextLimit": self.context_limit, "usage": self.total_usage,
                "todos": list(self.ctx.todos), "checkpoints": list(self.ctx.checkpoints)}

    async def _queue_update(self):
        await self.emit({"type": "queue_update", "steering": list(self.steering), "followUp": list(self.follow_ups)})

    # ------------------------------------------------------------------ public API
    async def prompt(self, message: str, streaming_behavior: Optional[str] = None) -> str:
        if self.is_streaming:
            if streaming_behavior == "steer":
                await self.steer(message)
                return "queued_steer"
            if streaming_behavior in ("followUp", "follow_up"):
                await self.follow_up(message)
                return "queued_follow_up"
            raise RuntimeError("Agent is streaming; pass streamingBehavior 'steer' or 'followUp'")
        self.abort_event.clear()
        self.is_streaming = True
        self._run_task = asyncio.create_task(self._run(message))
        return "started"

    async def steer(self, message: str) -> None:
        self.steering.append(message)
        await self._queue_update()

    async def follow_up(self, message: str) -> None:
        if not self.is_streaming:
            await self.prompt(message)
            return
        self.follow_ups.append(message)
        await self._queue_update()

    async def abort(self) -> None:
        self.abort_event.set()
        if self._run_task:
            try:
                await asyncio.wait_for(asyncio.shield(self._run_task), 10)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                self._run_task.cancel()

    async def wait_idle(self) -> None:
        if self._run_task:
            await asyncio.shield(self._run_task)

    async def run_to_completion(self, message: str) -> str:
        await self.prompt(message)
        await self.wait_idle()
        return self.last_output

    def new_session(self, session_dir: Optional[Path] = None) -> None:
        self.session = Session(session_dir or (self.session.file.parent if self.session.file else None),
                               str(self.cwd), persist=self.session.persist)
        self.ctx.todos.clear()
        self.ctx.checkpoints.clear()

    # ------------------------------------------------------------------ loop
    async def _run(self, first: str) -> None:
        new_msgs: List[dict] = []
        try:
            await self.emit({"type": "agent_start"})
            pending_user: Optional[str] = first
            turns = 0
            while True:
                if pending_user is not None:
                    um = user_message(pending_user)
                    self.session.append_message(um)
                    new_msgs.append(um)
                    await self.emit({"type": "message_start", "message": um})
                    await self.emit({"type": "message_end", "message": um})
                    pending_user = None
                turns += 1
                if turns > self.max_turns:
                    await self.emit({"type": "error", "error": f"max_turns ({self.max_turns}) reached"})
                    break
                await self.emit({"type": "turn_start"})
                if self.auto_compact and self.context_tokens() > int(self.context_limit * 0.8):
                    await self.compact(reason="threshold")
                am = await self._stream_turn()
                self.session.append_message(am)
                new_msgs.append(am)
                results = await self._run_tools(am)
                new_msgs.extend(results)
                await self.emit({"type": "turn_end", "message": am, "toolResults": results})
                self._apply_checkpoints()
                if am["stopReason"] in ("error", "aborted"):
                    break
                if self.steering:
                    pending_user = "\n\n".join(self.steering)
                    self.steering.clear()
                    await self._queue_update()
                    continue
                if am["stopReason"] == "toolUse":
                    continue
                if self.follow_ups:
                    pending_user = self.follow_ups.pop(0)
                    await self._queue_update()
                    continue
                break
        except asyncio.CancelledError:
            pass
        except Exception as e:  # never leave the agent wedged in streaming state
            await self.emit({"type": "error", "error": f"{type(e).__name__}: {e}"})
        finally:
            self.is_streaming = False
            last_a = next((m for m in reversed(new_msgs) if m["role"] == "assistant"), None)
            self.last_output = message_text(last_a) if last_a else ""
            await self.emit({"type": "agent_end", "messages": new_msgs})
            await self.emit({"type": "agent_settled"})

    async def _stream_turn(self) -> dict:
        am = assistant_message(self.provider.name, self.model)
        await self.emit({"type": "message_start", "message": am})

        async def upd(ev: dict):
            await self.emit({"type": "message_update", "message": am, "assistantMessageEvent": ev})

        await upd({"type": "start"})
        cur: Optional[dict] = None  # open text/thinking block
        schemas = T.schemas(self.tool_names)

        async def close_block():
            nonlocal cur
            if cur is not None:
                idx = am["content"].index(cur)
                kind = cur["type"]
                await upd({"type": f"{kind}_end", "contentIndex": idx,
                           "content": cur.get("text", cur.get("thinking", ""))})
                cur = None

        stop = "stop"
        try:
            async for kind, val in self.provider.stream(self.model, self.system_prompt(), self.messages,
                                                        schemas, self.abort_event, self.thinking):
                if kind in ("text_delta", "thinking_delta"):
                    btype = "text" if kind == "text_delta" else "thinking"
                    if cur is None or cur["type"] != btype:
                        await close_block()
                        cur = {"type": btype, btype: ""}
                        am["content"].append(cur)
                        await upd({"type": f"{btype}_start", "contentIndex": len(am["content"]) - 1})
                    cur[btype] += val
                    await upd({"type": f"{btype}_delta", "contentIndex": am["content"].index(cur), "delta": val})
                elif kind == "toolcall":
                    await close_block()
                    tc = {"type": "toolCall", **val}
                    am["content"].append(tc)
                    i = len(am["content"]) - 1
                    await upd({"type": "toolcall_start", "contentIndex": i})
                    await upd({"type": "toolcall_end", "contentIndex": i, "toolCall": tc})
                elif kind == "usage":
                    am["usage"] = usage(*val)
                    for k in ("input", "output", "totalTokens"):
                        self.total_usage[k] += am["usage"][k]
                elif kind == "stop":
                    stop = val
                elif kind == "error":
                    am["errorMessage"] = str(val)
                    stop = "error"
        except asyncio.CancelledError:
            stop = "aborted"
        await close_block()
        if self.abort_event.is_set():
            stop = "aborted"
        # salvage JSON-as-text tool calls from local models
        if stop == "stop" and not tool_calls(am):
            from .providers import salvage_text_toolcall
            sc = salvage_text_toolcall(message_text(am), self.tool_names)
            if sc:
                am["content"] = [{"type": "toolCall", **sc}]
                stop = "toolUse"
        am["stopReason"] = stop
        await upd({"type": "error" if stop == "error" else "done", "reason": stop})
        await self.emit({"type": "message_end", "message": am})
        return am

    async def _run_tools(self, am: dict) -> List[dict]:
        out: List[dict] = []
        for tc in tool_calls(am):
            cid, name, args = tc["id"], tc["name"], tc.get("arguments", {})
            await self.emit({"type": "tool_execution_start", "toolCallId": cid, "toolName": name, "args": args})
            if self.abort_event.is_set():
                res, err = T.result("Aborted by user."), True
            elif self.steering:
                res, err = T.result("Skipped due to queued user message."), True
            else:
                async def on_update(partial, _cid=cid, _n=name, _a=args):
                    await self.emit({"type": "tool_execution_update", "toolCallId": _cid, "toolName": _n,
                                     "args": _a, "partialResult": partial})
                res, err = await T.run_tool(name, args, self.ctx, on_update)
            await self.emit({"type": "tool_execution_end", "toolCallId": cid, "toolName": name,
                             "result": res, "isError": err})
            trm = tool_result_message(cid, name, res["content"], err, res.get("details"))
            self.session.append_message(trm)
            await self.emit({"type": "message_start", "message": trm})
            await self.emit({"type": "message_end", "message": trm})
            out.append(trm)
        return out

    def _apply_checkpoints(self) -> None:
        for label in self._pending_cp:
            self.ctx.checkpoints[label] = self.session.leaf
        self._pending_cp.clear()
        if self._pending_rewind:
            label, lesson = self._pending_rewind
            self._pending_rewind = None
            self.session.branch_to(self.ctx.checkpoints[label])
            self.session.append_message(user_message(
                f"[Rewound to checkpoint '{label}'. Lesson from the discarded branch]\n{lesson}"))

    # ------------------------------------------------------------------ compaction
    async def compact(self, reason: str = "manual", instructions: str = "") -> dict:
        msgs = self.messages
        # keep the tail starting at a user message so toolCall/toolResult pairs stay intact
        cut = len(msgs)
        users_seen = 0
        for i in range(len(msgs) - 1, -1, -1):
            if msgs[i]["role"] == "user":
                users_seen += 1
                cut = i
                if users_seen >= 2 or estimate_tokens(msgs[i:]) > self.context_limit // 3:
                    break
        head, kept = msgs[:cut], msgs[cut:]
        if not head:
            return {"compacted": False, "reason": "nothing to compact"}
        self.is_compacting = True
        await self.emit({"type": "compaction_start", "reason": reason})
        transcript = "\n".join(f"{m['role']}: {message_text(m)[:1500]}" +
                               "".join(f"\n  -> {t['name']}({str(t['arguments'])[:200]})" for t in tool_calls(m))
                               for m in head)
        sys = ("Summarize this coding-agent transcript for continuation. Keep: goal, decisions, files "
               "touched and their state, open problems, next steps. Terse bullets. " + instructions)
        summary = ""
        try:
            async for kind, val in self.provider.stream(self.model, sys, [user_message(transcript[-60000:])],
                                                        [], asyncio.Event()):
                if kind == "text_delta":
                    summary += val
        except Exception as e:
            summary = f"(summary failed: {e})"
        if not summary.strip():
            summary = transcript[-4000:]
        before = estimate_tokens(msgs)
        self.session.append("compaction", summary=summary, kept=copy.deepcopy(kept), reason=reason)
        after = estimate_tokens(self.messages)
        self.is_compacting = False
        info = {"compacted": True, "tokensBefore": before, "tokensAfter": after}
        await self.emit({"type": "compaction_end", **info})
        return info

    # ------------------------------------------------------------------ subagents
    async def _spawn_local(self, tasks: List[dict]) -> List[dict]:
        from .roles import ROLE_PROMPTS
        return await self._spawn_with(tasks, ROLE_PROMPTS)

    async def _spawn_with(self, tasks: List[dict], role_prompts: Dict[str, str]) -> List[dict]:
        sem = asyncio.Semaphore(int(os.environ.get("HARNESS_SUBAGENT_PARALLEL", "2")))

        async def one(t: dict) -> dict:
            role = str(t.get("role") or "BUILDER").upper()
            async with sem:
                child = Agent(self.provider, self.model, self.cwd, role=role,
                              role_prompt=role_prompts.get(role, ""),
                              tools=[n for n in self.tool_names if n not in ("task", "rewind", "checkpoint")],
                              persist=False, depth=self.depth + 1, max_depth=self.max_depth,
                              context_limit=self.context_limit, max_turns=25,
                              emit=self._child_emitter(role))
                child.abort_event = self.abort_event
                child.ctx.abort = self.abort_event
                out = await child.run_to_completion(str(t["prompt"]))
                last = next((m for m in reversed(child.messages) if m["role"] == "assistant"), {})
                ok = last.get("stopReason") == "stop"
                for k in ("input", "output", "totalTokens"):
                    self.total_usage[k] += child.total_usage[k]
                return {"prompt": t["prompt"][:200], "role": role, "ok": ok, "output": out[:8000]}

        return list(await asyncio.gather(*(one(t) for t in tasks)))

    def _child_emitter(self, role: str) -> Emit:
        async def fwd(ev: dict):
            if ev["type"] in ("tool_execution_start", "tool_execution_end", "agent_end"):
                slim = {k: v for k, v in ev.items() if k not in ("messages",)}
                await self.emit({"type": "subagent_event", "role": role, "event": slim})
        return fwd
