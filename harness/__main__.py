"""
python3 -m harness --mode rpc|print|json [-p PROMPT] [--provider auto|ollama|openai|mock]
                   [--model M] [--cwd DIR] [--no-session] [--session-dir DIR] [--role ROLE]
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

from .agent import Agent
from .providers import make_provider
from .roles import ROLE_PROMPTS
from .rpc import serve_stdio

DEFAULT_MODEL = os.environ.get("MUX_MODEL", "llama3.1:8b-instruct-q5_K_M")


def build(args, emit):
    provider = make_provider(args.provider)
    model = args.model or (DEFAULT_MODEL if provider.name != "openai" else os.environ.get("OPENAI_MODEL", "local-model"))
    role = args.role.upper()
    return Agent(provider, model, args.cwd, emit=emit, role=role, role_prompt=ROLE_PROMPTS.get(role, ""),
                 session_dir=None if args.no_session else Path(args.session_dir).expanduser(),
                 persist=not args.no_session, name=args.name, context_limit=args.context,
                 jail=not args.no_jail)


async def main_async(args) -> int:
    if args.mode == "rpc":
        await serve_stdio(lambda emit: build(args, emit))
        return 0
    prompt = args.print or sys.stdin.read()

    async def emit(ev):
        if args.mode == "json":
            print(json.dumps(ev, default=str), flush=True)
        elif ev["type"] == "message_update" and ev["assistantMessageEvent"]["type"] == "text_delta":
            print(ev["assistantMessageEvent"]["delta"], end="", flush=True)
        elif ev["type"] == "tool_execution_start":
            print(f"\n\033[2m→ {ev['toolName']} {json.dumps(ev['args'])[:160]}\033[0m", file=sys.stderr, flush=True)
        elif ev["type"] == "tool_execution_end" and ev["isError"]:
            print(f"\033[31m  ✗ {ev['result']['content'][0]['text'][:300]}\033[0m", file=sys.stderr, flush=True)

    agent = build(args, emit)
    await agent.run_to_completion(prompt)
    if args.mode == "print":
        print()
    last = next((m for m in reversed(agent.messages) if m["role"] == "assistant"), {})
    return 0 if last.get("stopReason") == "stop" else 1


def main():
    ap = argparse.ArgumentParser(prog="harness", description="Pi-compatible coding agent harness")
    ap.add_argument("--mode", choices=["rpc", "print", "json"], default="print")
    ap.add_argument("-p", "--print", help="prompt (print/json mode); stdin if omitted")
    ap.add_argument("--provider", default=os.environ.get("MUX_PROVIDER", "auto"))
    ap.add_argument("--model")
    ap.add_argument("--role", default="GENERAL", choices=[r for r in ROLE_PROMPTS] + [r.lower() for r in ROLE_PROMPTS])
    ap.add_argument("--cwd", default=os.getcwd())
    ap.add_argument("--name")
    ap.add_argument("--context", type=int, default=int(os.environ.get("MUX_CONTEXT", "16384")))
    ap.add_argument("--no-session", action="store_true")
    ap.add_argument("--session-dir", default="~/.mux/sessions")
    ap.add_argument("--no-jail", action="store_true", help="allow file tools outside --cwd")
    args = ap.parse_args()
    if args.print and args.mode == "rpc":
        ap.error("RPC mode takes prompts via the prompt command, not -p")
    sys.exit(asyncio.run(main_async(args)))


if __name__ == "__main__":
    main()
