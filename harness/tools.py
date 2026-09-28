"""
Tool surface.

Pi core (same names + params as pi-coding-agent):
  read(path, offset?, limit?)     write(path, content)     edit(path, oldText, newText)
  bash(command, timeout?)         grep(pattern, path?, glob?, ignoreCase?, literal?, context?, limit?)
  find(pattern, path?, limit?)    ls(path?, limit?)
OMP-style upgrades:
  read(..., hashline=true)  -> lines tagged "N:hhh|content"
  hashline_edit(path, edits[])   -> edits anchored by line+content-hash; stale anchors rejected
  task(tasks[])                  -> parallel subagents (MUX workers when hosted), JSON results
  todo(todos[])                  -> tracked plan, surfaced to the UI
  checkpoint(label) / rewind(label, lesson) -> context checkpoints; rewind prunes the branch but
                                    keeps the lesson (context-budget recovery)
Truncation matches Pi: 2000 lines or 50KB, whichever hits first.
"""
from __future__ import annotations

import asyncio
import fnmatch
import hashlib
import json
import os
import re
import shutil
import signal
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, Optional

MAX_LINES = 2000
MAX_BYTES = 50 * 1024
SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", "dist", "build", ".mypy_cache"}


class ToolError(Exception):
    pass


@dataclass
class ToolContext:
    cwd: Path
    jail: bool = True
    abort: asyncio.Event = field(default_factory=asyncio.Event)
    # hooks provided by the Agent
    spawn_subagents: Optional[Callable[[List[dict]], Awaitable[List[dict]]]] = None
    todos: List[dict] = field(default_factory=list)
    checkpoints: Dict[str, str] = field(default_factory=dict)
    request_rewind: Optional[Callable[[str, str], None]] = None
    mark_checkpoint: Optional[Callable[[str], None]] = None
    bash_policy: Optional[Callable[[str], Optional[str]]] = None  # returns deny reason

    def resolve(self, p: str) -> Path:
        if not isinstance(p, str) or not p:
            raise ToolError("path is required")
        p = os.path.expanduser(p.lstrip("@"))
        full = (self.cwd / p).resolve() if not os.path.isabs(p) else Path(p).resolve()
        if self.jail:
            root = self.cwd.resolve()
            if full != root and root not in full.parents:
                raise ToolError(f"path escapes workspace: {p}")
        return full


def result(txt: str, **details) -> dict:
    return {"content": [{"type": "text", "text": txt}], "details": details or {}}


def truncate_head(txt: str) -> tuple[str, bool]:
    lines = txt.split("\n")
    out, size, cut = [], 0, False
    for ln in lines:
        b = len(ln.encode()) + 1
        if len(out) >= MAX_LINES or size + b > MAX_BYTES:
            cut = True
            break
        out.append(ln)
        size += b
    return "\n".join(out), cut


def truncate_tail(txt: str) -> tuple[str, bool]:
    lines = txt.split("\n")
    out, size, cut = [], 0, False
    for ln in reversed(lines):
        b = len(ln.encode()) + 1
        if len(out) >= MAX_LINES or size + b > MAX_BYTES:
            cut = True
            break
        out.append(ln)
        size += b
    return "\n".join(reversed(out)), cut


def line_hash(line: str) -> str:
    return hashlib.blake2s(line.rstrip("\r").encode(), digest_size=2).hexdigest()[:3]


# --------------------------------------------------------------------------- #
# Pi core tools
# --------------------------------------------------------------------------- #
async def t_read(a: dict, ctx: ToolContext, upd) -> dict:
    path = ctx.resolve(a.get("path"))
    if not path.exists():
        raise ToolError(f"File not found: {a.get('path')}")
    if path.is_dir():
        raise ToolError(f"{a.get('path')} is a directory; use ls")
    raw = path.read_bytes()
    if b"\x00" in raw[:8192]:
        raise ToolError(f"{a.get('path')} looks binary ({len(raw)} bytes)")
    lines = raw.decode("utf-8", errors="replace").split("\n")
    offset = max(1, int(a.get("offset") or 1))
    limit = int(a.get("limit") or MAX_LINES)
    sel = lines[offset - 1: offset - 1 + limit]
    if a.get("hashline"):
        body = "\n".join(f"{offset + i}:{line_hash(l)}|{l}" for i, l in enumerate(sel))
    else:
        body = "\n".join(sel)
    body, cut = truncate_head(body)
    shown = body.count("\n") + 1 if body else 0
    end = offset + shown - 1
    if cut or end < len(lines):
        body += f"\n\n[Showing lines {offset}-{end} of {len(lines)}. Use offset={end + 1} to continue.]"
    return result(body, path=str(path), totalLines=len(lines), truncated=cut)


async def t_write(a: dict, ctx: ToolContext, upd) -> dict:
    path = ctx.resolve(a.get("path"))
    content = a.get("content")
    if not isinstance(content, str):
        raise ToolError("content must be a string")
    path.parent.mkdir(parents=True, exist_ok=True)
    existed = path.exists()
    path.write_text(content, encoding="utf-8")
    return result(f"{'Overwrote' if existed else 'Created'} {a['path']} ({len(content.encode())} bytes)",
                  path=str(path))


def _diff_stub(old: str, new: str) -> str:
    import difflib
    d = difflib.unified_diff(old.splitlines(), new.splitlines(), lineterm="", n=2)
    return "\n".join(list(d)[2:])[:4000]


async def t_edit(a: dict, ctx: ToolContext, upd) -> dict:
    path = ctx.resolve(a.get("path"))
    old, new = a.get("oldText"), a.get("newText")
    if not isinstance(old, str) or not isinstance(new, str) or old == "":
        raise ToolError("oldText (non-empty) and newText are required")
    if not path.exists():
        raise ToolError(f"File not found: {a.get('path')}")
    src = path.read_text(encoding="utf-8")
    n = src.count(old)
    if n == 0:
        # tolerate CRLF / trailing-whitespace drift, like Pi's fuzzy fallback
        norm = lambda s: "\n".join(l.rstrip() for l in s.replace("\r\n", "\n").split("\n"))
        if norm(src).count(norm(old)) == 1:
            src2 = norm(src).replace(norm(old), new, 1)
            path.write_text(src2, encoding="utf-8")
            return result(f"Edited {a['path']} (whitespace-normalized match)",
                          diff=_diff_stub(src, src2))
        raise ToolError(f"oldText not found in {a['path']}. Re-read the file and copy the exact text.")
    if n > 1:
        raise ToolError(f"oldText matches {n} locations in {a['path']}; include more context to make it unique.")
    out = src.replace(old, new, 1)
    path.write_text(out, encoding="utf-8")
    return result(f"Edited {a['path']}", diff=_diff_stub(src, out))


async def t_bash(a: dict, ctx: ToolContext, upd) -> dict:
    cmd = a.get("command")
    if not isinstance(cmd, str) or not cmd.strip():
        raise ToolError("command is required")
    if ctx.bash_policy:
        why = ctx.bash_policy(cmd)
        if why:
            raise ToolError(f"blocked by policy: {why}")
    timeout = float(a.get("timeout") or 120)
    proc = await asyncio.create_subprocess_shell(
        cmd, cwd=str(ctx.cwd), stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
        start_new_session=True, env={**os.environ, "PAGER": "cat", "GIT_PAGER": "cat"})
    buf: List[str] = []
    killed = None

    async def pump():
        while True:
            chunk = await proc.stdout.read(4096)
            if not chunk:
                break
            buf.append(chunk.decode("utf-8", errors="replace"))
            tail, _ = truncate_tail("".join(buf)[-MAX_BYTES:])
            await upd({"content": [{"type": "text", "text": tail}], "details": {}})

    pump_t = asyncio.create_task(pump())
    wait_t = asyncio.create_task(proc.wait())
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not wait_t.done():  # poll: robust to Events bound to another loop
        if ctx.abort.is_set():
            killed = "aborted"
        elif loop.time() >= deadline:
            killed = f"timed out after {timeout:g}s"
        if killed:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            break
        await asyncio.wait({wait_t}, timeout=0.05)
    await wait_t
    await pump_t
    out, cut = truncate_tail("".join(buf))
    code = proc.returncode
    if cut:
        out = "[output truncated, showing tail]\n" + out
    if killed:
        raise ToolError(f"{out}\n\nCommand {killed}")
    if code != 0:
        raise ToolError(f"{out}\n\nCommand exited with code {code}")
    return result(out or "(no output)", exitCode=code, truncated=cut)


def _walk(root: Path):
    for dp, dns, fns in os.walk(root):
        dns[:] = [d for d in dns if d not in SKIP_DIRS]
        for fn in fns:
            yield Path(dp) / fn


async def t_grep(a: dict, ctx: ToolContext, upd) -> dict:
    pat = a.get("pattern")
    if not pat:
        raise ToolError("pattern is required")
    root = ctx.resolve(a.get("path") or ".")
    limit = int(a.get("limit") or 100)
    context = int(a.get("context") or 0)
    rg = shutil.which("rg")
    if rg:
        args = [rg, "--line-number", "--no-heading", "--color=never", "-m", str(limit)]
        if a.get("ignoreCase"):
            args.append("-i")
        if a.get("literal"):
            args.append("-F")
        if context:
            args += ["-C", str(context)]
        if a.get("glob"):
            args += ["--glob", a["glob"]]
        for d in SKIP_DIRS:
            args += ["--glob", f"!{d}/"]
        args += ["--", pat, str(root)]
        p = await asyncio.create_subprocess_exec(*args, stdout=asyncio.subprocess.PIPE,
                                                 stderr=asyncio.subprocess.PIPE)
        so, se = await p.communicate()
        if p.returncode == 2:
            raise ToolError(se.decode()[:2000])
        lines = so.decode(errors="replace").splitlines()[:limit * (1 + 2 * context)]
    else:
        flags = re.I if a.get("ignoreCase") else 0
        rx = re.compile(re.escape(pat) if a.get("literal") else pat, flags)
        lines = []
        files = [root] if root.is_file() else _walk(root)
        for f in files:
            if a.get("glob") and not fnmatch.fnmatch(f.name, a["glob"]):
                continue
            try:
                fl = f.read_text(encoding="utf-8").split("\n")
            except (UnicodeDecodeError, OSError):
                continue
            for i, l in enumerate(fl):
                if rx.search(l):
                    lines.append(f"{f}:{i + 1}:{l}")
                    if len(lines) >= limit:
                        break
            if len(lines) >= limit:
                break
    cwd = str(ctx.cwd) + os.sep
    body = "\n".join(l.replace(cwd, "", 1) for l in lines)
    body, cut = truncate_head(body)
    return result(body or "No matches found", matches=len(lines), truncated=cut)


async def t_find(a: dict, ctx: ToolContext, upd) -> dict:
    pat = a.get("pattern")
    if not pat:
        raise ToolError("pattern is required")
    root = ctx.resolve(a.get("path") or ".")
    limit = int(a.get("limit") or 1000)
    hits = []
    for f in _walk(root):
        rel = str(f.relative_to(root))
        if fnmatch.fnmatch(rel, pat) or fnmatch.fnmatch(f.name, pat):
            hits.append(rel)
            if len(hits) >= limit:
                break
    hits.sort()
    body, cut = truncate_head("\n".join(hits))
    return result(body or "No files found", count=len(hits), truncated=cut or len(hits) >= limit)


async def t_ls(a: dict, ctx: ToolContext, upd) -> dict:
    root = ctx.resolve(a.get("path") or ".")
    if not root.is_dir():
        raise ToolError(f"Not a directory: {a.get('path')}")
    limit = int(a.get("limit") or 500)
    items = sorted(root.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower()))
    body = "\n".join(p.name + ("/" if p.is_dir() else "") for p in items[:limit])
    if len(items) > limit:
        body += f"\n[{len(items) - limit} more entries]"
    return result(body or "(empty directory)", count=len(items))


# --------------------------------------------------------------------------- #
# OMP-style tools
# --------------------------------------------------------------------------- #
def _anchor(s: str, lines: List[str]) -> int:
    m = re.fullmatch(r"\s*(\d+):([0-9a-f]{3})\s*", str(s or ""))
    if not m:
        raise ToolError(f"bad anchor {s!r}; expected 'LINE:HASH' from read(hashline=true)")
    n, h = int(m.group(1)), m.group(2)
    if not 1 <= n <= len(lines):
        raise ToolError(f"anchor {s} out of range (file has {len(lines)} lines)")
    if line_hash(lines[n - 1]) != h:
        raise ToolError(f"stale anchor {s}: line {n} changed since you read it. Re-read with hashline=true.")
    return n - 1


async def t_hashline_edit(a: dict, ctx: ToolContext, upd) -> dict:
    path = ctx.resolve(a.get("path"))
    if not path.exists():
        raise ToolError(f"File not found: {a.get('path')}")
    edits = a.get("edits")
    if not isinstance(edits, list) or not edits:
        raise ToolError("edits must be a non-empty list")
    src = path.read_text(encoding="utf-8")
    lines = src.split("\n")
    plan = []
    for e in edits:  # validate ALL anchors against the original before touching anything
        op = e.get("op", "replace")
        s = _anchor(e.get("anchor"), lines)
        end = _anchor(e["end"], lines) if e.get("end") else s
        if end < s:
            raise ToolError(f"end before anchor in {e}")
        if op not in ("replace", "insert_before", "insert_after", "delete"):
            raise ToolError(f"unknown op {op}")
        new = [] if op == "delete" else str(e.get("content", "")).split("\n")
        plan.append((s, end, op, new))
    spans = sorted((s, end) for s, end, op, _ in plan if op in ("replace", "delete"))
    for (s1, e1), (s2, _) in zip(spans, spans[1:]):
        if s2 <= e1:
            raise ToolError("overlapping edits; merge them")
    for s, end, op, new in sorted(plan, key=lambda p: p[0], reverse=True):
        if op in ("replace", "delete"):
            lines[s:end + 1] = new
        elif op == "insert_before":
            lines[s:s] = new
        else:
            lines[end + 1:end + 1] = new
    out = "\n".join(lines)
    path.write_text(out, encoding="utf-8")
    return result(f"Applied {len(plan)} hashline edit(s) to {a['path']}", diff=_diff_stub(src, out))


async def t_todo(a: dict, ctx: ToolContext, upd) -> dict:
    todos = a.get("todos")
    if not isinstance(todos, list):
        raise ToolError("todos must be a list of {content, status}")
    clean = []
    for t in todos:
        st = t.get("status", "pending")
        if st not in ("pending", "in_progress", "completed"):
            st = "pending"
        clean.append({"content": str(t.get("content", ""))[:300], "status": st})
    ctx.todos[:] = clean
    mark = {"pending": "[ ]", "in_progress": "[~]", "completed": "[x]"}
    return result("\n".join(f"{mark[t['status']]} {t['content']}" for t in clean) or "(empty)", todos=clean)


async def t_task(a: dict, ctx: ToolContext, upd) -> dict:
    tasks = a.get("tasks")
    if not isinstance(tasks, list) or not tasks:
        raise ToolError("tasks must be a non-empty list of {prompt, role?}")
    if not ctx.spawn_subagents:
        raise ToolError("subagents unavailable at this depth")
    if len(tasks) > 8:
        raise ToolError("max 8 parallel subagents")
    results = await ctx.spawn_subagents(tasks)
    return result(json.dumps(results, indent=2)[:MAX_BYTES], results=results)


async def t_checkpoint(a: dict, ctx: ToolContext, upd) -> dict:
    label = str(a.get("label") or f"cp{len(ctx.checkpoints) + 1}")
    if not ctx.mark_checkpoint:
        raise ToolError("checkpoints unavailable")
    ctx.mark_checkpoint(label)
    return result(f"Checkpoint '{label}' set. rewind(label='{label}', lesson=...) returns here.")


async def t_rewind(a: dict, ctx: ToolContext, upd) -> dict:
    label = str(a.get("label") or "")
    if label not in ctx.checkpoints:
        raise ToolError(f"unknown checkpoint {label!r}; have {list(ctx.checkpoints)}")
    lesson = str(a.get("lesson") or "")
    if not lesson:
        raise ToolError("lesson is required: summarize what you learned before rewinding")
    ctx.request_rewind(label, lesson)
    return result(f"Rewinding to '{label}' after this turn. Lesson retained.")


# --------------------------------------------------------------------------- #
# Registry (JSON-schema, provider-agnostic)
# --------------------------------------------------------------------------- #
def _s(props: dict, req: List[str]) -> dict:
    return {"type": "object", "properties": props, "required": req}


STR, INT, BOOL = {"type": "string"}, {"type": "integer"}, {"type": "boolean"}

TOOLS: Dict[str, dict] = {
    "read": {"fn": t_read, "description": "Read a text file. Output truncated to 2000 lines/50KB; use offset/limit to page. hashline=true tags each line 'N:hhh|' for hashline_edit.",
             "parameters": _s({"path": STR, "offset": {**INT, "description": "1-indexed start line"},
                               "limit": INT, "hashline": BOOL}, ["path"])},
    "write": {"fn": t_write, "description": "Create or overwrite a file with content. Creates parent dirs.",
              "parameters": _s({"path": STR, "content": STR}, ["path", "content"])},
    "edit": {"fn": t_edit, "description": "Replace exactly one occurrence of oldText with newText. oldText must match uniquely.",
             "parameters": _s({"path": STR, "oldText": STR, "newText": STR}, ["path", "oldText", "newText"])},
    "bash": {"fn": t_bash, "description": "Run a shell command in the workspace. Returns combined stdout/stderr (tail-truncated). Non-zero exit is an error.",
             "parameters": _s({"command": STR, "timeout": {**INT, "description": "seconds, default 120"}}, ["command"])},
    "grep": {"fn": t_grep, "description": "Search file contents by regex (ripgrep if installed).",
             "parameters": _s({"pattern": STR, "path": STR, "glob": STR, "ignoreCase": BOOL,
                               "literal": BOOL, "context": INT, "limit": INT}, ["pattern"])},
    "find": {"fn": t_find, "description": "Find files by glob pattern (e.g. '**/*.py' or '*.go').",
             "parameters": _s({"pattern": STR, "path": STR, "limit": INT}, ["pattern"])},
    "ls": {"fn": t_ls, "description": "List a directory.", "parameters": _s({"path": STR, "limit": INT}, [])},
    "hashline_edit": {"fn": t_hashline_edit,
                      "description": "Edit by hash anchors from read(hashline=true). Each edit: {op: replace|insert_before|insert_after|delete, anchor: 'N:hhh', end?: 'N:hhh', content?}. All anchors are verified; stale anchors are rejected.",
                      "parameters": _s({"path": STR, "edits": {"type": "array", "items": _s({
                          "op": {"type": "string", "enum": ["replace", "insert_before", "insert_after", "delete"]},
                          "anchor": STR, "end": STR, "content": STR}, ["anchor"])}}, ["path", "edits"])},
    "todo": {"fn": t_todo, "description": "Replace the task list. Keep exactly one item in_progress.",
             "parameters": _s({"todos": {"type": "array", "items": _s({
                 "content": STR, "status": {"type": "string", "enum": ["pending", "in_progress", "completed"]}},
                 ["content", "status"])}}, ["todos"])},
    "task": {"fn": t_task, "description": "Run subagents in parallel, each with a fresh context. Each task: {prompt, role?}. Returns [{prompt, role, ok, output}]. Use for independent research/implementation slices.",
             "parameters": _s({"tasks": {"type": "array", "items": _s({"prompt": STR, "role": STR}, ["prompt"])}}, ["tasks"])},
    "checkpoint": {"fn": t_checkpoint, "description": "Mark the current conversation point before exploratory work.",
                   "parameters": _s({"label": STR}, [])},
    "rewind": {"fn": t_rewind, "description": "Discard the conversation since a checkpoint, keeping a written lesson. Use after a dead-end exploration to reclaim context.",
               "parameters": _s({"label": STR, "lesson": STR}, ["label", "lesson"])},
}

PI_CORE = ["read", "bash", "edit", "write"]
PI_ALL = PI_CORE + ["grep", "find", "ls"]
OMP_EXTRA = ["hashline_edit", "todo", "task", "checkpoint", "rewind"]
DEFAULT_TOOLS = PI_ALL + OMP_EXTRA


def schemas(names: List[str]) -> List[dict]:
    return [{"name": n, "description": TOOLS[n]["description"], "parameters": TOOLS[n]["parameters"]}
            for n in names if n in TOOLS]


async def run_tool(name: str, args: Any, ctx: ToolContext,
                   on_update: Callable[[dict], Awaitable[None]]) -> tuple[dict, bool]:
    """Returns (result, isError). Never raises."""
    spec = TOOLS.get(name)
    if spec is None:
        return result(f"Unknown tool: {name}. Available: {', '.join(TOOLS)}"), True
    if not isinstance(args, dict):
        return result(f"Invalid arguments for {name}: expected an object"), True
    missing = [k for k in spec["parameters"].get("required", []) if k not in args]
    if missing:
        return result(f"Missing required argument(s) for {name}: {', '.join(missing)}"), True
    try:
        return await spec["fn"](args, ctx, on_update), False
    except ToolError as e:
        return result(str(e)), True
    except asyncio.CancelledError:
        raise
    except Exception as e:  # tool bug must not kill the agent loop
        return result(f"{name} failed: {type(e).__name__}: {e}"), True
