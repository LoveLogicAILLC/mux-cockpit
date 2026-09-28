"""
Pi-compatible RPC: strict JSONL, commands on stdin, responses + events on stdout.

Commands: prompt{message, streamingBehavior?} steer{message} follow_up{message} abort
          get_state get_messages new_session compact{customInstructions?} set_model{model}
          set_thinking_level{level} bash{command, timeout?} get_available_tools
MUX extensions (ignored by plain Pi clients): morph{role}, checkpoint_list
"""
from __future__ import annotations

import asyncio
import json
import sys
from typing import Callable, Optional

from . import tools as T
from .agent import Agent
from .protocol import response
from .roles import ROLE_PROMPTS


async def handle_command(agent: Agent, cmd: dict, emit: Callable) -> dict:
    ctype = cmd.get("type") or cmd.get("action")
    rid = cmd.get("id")
    try:
        if ctype == "prompt":
            msg = cmd.get("message")
            if not isinstance(msg, str) or not msg:
                return response("prompt", rid, False, error="message is required")
            if msg.lstrip().startswith("@"):
                return response("prompt", rid, False, error="RPC mode rejects @file prompt arguments")
            disp = await agent.prompt(msg, cmd.get("streamingBehavior"))
            return response("prompt", rid, data={"disposition": disp})
        if ctype == "steer":
            await agent.steer(str(cmd.get("message", "")))
            return response("steer", rid)
        if ctype == "follow_up":
            await agent.follow_up(str(cmd.get("message", "")))
            return response("follow_up", rid)
        if ctype == "abort":
            await agent.abort()
            return response("abort", rid)
        if ctype == "get_state":
            return response("get_state", rid, data=agent.state())
        if ctype == "get_messages":
            return response("get_messages", rid, data={"messages": agent.messages})
        if ctype == "new_session":
            if agent.is_streaming:
                await agent.abort()
            agent.new_session()
            return response("new_session", rid, data={"sessionId": agent.session.id})
        if ctype == "compact":
            info = await agent.compact("manual", str(cmd.get("customInstructions", "")))
            return response("compact", rid, data=info)
        if ctype == "set_model":
            agent.model = str(cmd["model"])
            return response("set_model", rid, data={"model": agent.model})
        if ctype == "set_thinking_level":
            agent.thinking = str(cmd.get("level", "off"))
            return response("set_thinking_level", rid)
        if ctype == "get_available_tools":
            return response("get_available_tools", rid, data={"tools": T.schemas(agent.tool_names)})
        if ctype == "morph":
            role = str(cmd.get("role") or cmd.get("new_role") or "").upper()
            if role not in ROLE_PROMPTS:
                return response("morph", rid, False, error=f"unknown role {role}; valid: {list(ROLE_PROMPTS)}")
            agent.set_role(role, ROLE_PROMPTS[role])
            return response("morph", rid, data={"role": role, "messageCount": len(agent.messages)})
        if ctype == "bash":
            async def upd(partial):
                await emit({"type": "bash_execution_update", "id": rid, "partialResult": partial})
            res, err = await T.run_tool("bash", {"command": cmd.get("command"), "timeout": cmd.get("timeout")},
                                        agent.ctx, upd)
            return response("bash", rid, not err, data=res, error=res["content"][0]["text"] if err else None)
        return response(str(ctype), rid, False, error=f"Unknown command: {ctype}")
    except Exception as e:
        return response(str(ctype), rid, False, error=f"{type(e).__name__}: {e}")


async def serve_stdio(make_agent: Callable[[Callable], Agent]) -> None:
    loop = asyncio.get_running_loop()
    reader = asyncio.StreamReader(limit=16 * 1024 * 1024)
    await loop.connect_read_pipe(lambda: asyncio.StreamReaderProtocol(reader), sys.stdin)
    out = sys.stdout.buffer
    lock = asyncio.Lock()

    async def write(obj: dict) -> None:
        async with lock:
            out.write((json.dumps(obj, ensure_ascii=False, default=str) + "\n").encode())
            out.flush()

    agent = make_agent(write)
    pending = set()
    while True:
        line = await reader.readline()
        if not line:
            break
        s = line.decode("utf-8", errors="replace").strip()
        if not s:
            continue
        try:
            cmd = json.loads(s)
            if not isinstance(cmd, dict):
                raise ValueError("command must be an object")
        except Exception as e:
            await write({"type": "response", "command": "parse", "success": False,
                         "error": f"Failed to parse command: {e}"})
            continue

        async def run(c=cmd):
            await write(await handle_command(agent, c, write))
        t = asyncio.create_task(run())
        pending.add(t)
        t.add_done_callback(pending.discard)
    # stdin closed -> orderly shutdown (Pi semantics)
    if agent.is_streaming:
        await agent.abort()
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)
