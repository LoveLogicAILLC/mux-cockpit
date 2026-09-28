"""
cockpit_integration.py
Unix-socket bridge between the host and clients (Go TUI, AgentsRoom, scripts).
JSON lines over MUX_SOCK (default /tmp/mux_host.sock), mode 0600 (owner only).

Host actions ({"action": ...}):
  status | submit{goal, priority?} | morph{worker_id, new_role, keep_context?}
  park{worker_id} | unpark{worker_id} | checkpoint{worker_id} | subscribe
Pi RPC passthrough ({"type": ..., "worker": "<id>"}): any harness/rpc.py command
  (prompt, steer, follow_up, abort, get_state, get_messages, compact, bash, ...)
  routed to that worker's agent. `subscribe` streams every worker's events.

  echo '{"action":"status"}' | nc -U /tmp/mux_host.sock
"""
from __future__ import annotations

import asyncio
import json
import os
from typing import Set

from harness.rpc import handle_command

SOCK_PATH = os.environ.get("MUX_SOCK", "/tmp/mux_host.sock")
MAX_LINE = 1024 * 1024
QUIET = {"message_update", "tool_execution_update"}  # high-volume; opt in with {"verbose":true}


class CockpitBridge:
    def __init__(self, orchestrator, sock_path: str = SOCK_PATH):
        self.orch = orchestrator
        self.sock_path = sock_path
        self.subscribers: Set[tuple] = set()
        orchestrator.listeners.append(self.broadcast_event)

    async def start_server(self):
        if os.path.exists(self.sock_path):
            os.remove(self.sock_path)
        old = os.umask(0o177)  # socket created 0600: other local users can't drive your agents
        try:
            server = await asyncio.start_unix_server(self.handle_client, path=self.sock_path, limit=MAX_LINE)
        finally:
            os.umask(old)
        os.chmod(self.sock_path, 0o600)
        self.orch.note(f"cockpit socket {self.sock_path}")
        async with server:
            await server.serve_forever()

    async def handle_client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        sub = None
        try:
            while True:
                try:
                    data = await reader.readline()
                except (asyncio.LimitOverrunError, ValueError):
                    await self._send(writer, {"success": False, "error": "line too long"})
                    break
                if not data:
                    break
                try:
                    cmd = json.loads(data.decode())
                    if not isinstance(cmd, dict):
                        raise ValueError("command must be a JSON object")
                except Exception as e:
                    await self._send(writer, {"type": "response", "command": "parse", "success": False,
                                              "error": f"Failed to parse command: {e}"})
                    continue
                if cmd.get("action") == "subscribe":
                    sub = (writer, bool(cmd.get("verbose")))
                    self.subscribers.add(sub)
                    await self._send(writer, {"status": "subscribed"})
                    continue
                await self._send(writer, await self.handle_command(cmd))
        except (ConnectionResetError, BrokenPipeError):
            pass
        finally:
            if sub:
                self.subscribers.discard(sub)
            writer.close()

    @staticmethod
    async def _send(writer, obj):
        writer.write((json.dumps(obj, default=str) + "\n").encode())
        await writer.drain()

    async def handle_command(self, cmd: dict) -> dict:
        o = self.orch
        action = cmd.get("action")
        try:
            if action == "status":
                return o.status()
            if action == "submit":
                goal = str(cmd.get("goal") or "").strip()
                if not goal:
                    return {"status": "error", "error": "goal is required"}
                tid = await o.submit(goal, str(cmd.get("priority", "high")))
                return {"status": "submitted", "task": tid, "goal": goal}
            if action == "morph":
                w = await o.morph_worker(str(cmd.get("worker_id")), str(cmd.get("new_role")),
                                         bool(cmd.get("keep_context", True)))
                return {"status": "morphed", "worker": w.id, "role": w.role, "lora": w.lora or "-"}
            if action == "park":
                n = await o.park(str(cmd.get("worker_id")))
                return {"status": "parked", "worker": cmd.get("worker_id"), "flushed": n}
            if action == "unpark":
                n = await o.unpark(str(cmd.get("worker_id")))
                return {"status": "unparked", "worker": cmd.get("worker_id"), "restored": n}
            if action == "checkpoint":
                p = await o.checkpoint(str(cmd.get("worker_id")))
                return {"status": "checkpointed", "worker": cmd.get("worker_id"), "file": p}
            if action is None and "type" in cmd:  # Pi RPC passthrough
                wid = str(cmd.get("worker") or "builder")
                agent = o._w(wid).agent

                async def emit(ev):
                    await self.broadcast_event({"worker": wid, **ev})
                return {"worker": wid, **(await handle_command(agent, cmd, emit))}
            return {"status": "error", "error": f"unknown action {action}"}
        except KeyError as e:
            return {"status": "error", "error": str(e).strip("'\"")}
        except Exception as e:
            return {"status": "error", "error": f"{type(e).__name__}: {e}"}

    async def broadcast_event(self, event: dict):
        if not self.subscribers:
            return
        line = (json.dumps(event, default=str) + "\n").encode()
        for sub in list(self.subscribers):
            w, verbose = sub
            if event.get("type") in QUIET and not verbose:
                continue
            try:
                w.write(line)
                await w.drain()
            except Exception:
                self.subscribers.discard(sub)
