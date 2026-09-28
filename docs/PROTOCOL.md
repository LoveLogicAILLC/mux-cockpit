# Wire Protocols

This is the reference for the two JSON protocols in this repo. Everything here is
verified against the current source (`harness/agent.py`, `harness/rpc.py`,
`harness/protocol.py`, `harness/tools.py`, `cockpit_integration.py`,
`host_orchestrator.py`, `mux_router.py`). Line numbers will drift; message shapes
won't — they're read straight off the code paths that build them.

Two independent layers:

1. **Pi RPC** (`harness/`) — one JSONL stream per `Agent`. This is what a single
   coding-agent process speaks on stdin/stdout, and it's also what
   `cockpit_integration.py` speaks internally to each of the 7 in-process workers.
2. **Host socket protocol** (`cockpit_integration.py`) — a Unix domain socket that
   multiplexes host-level actions (`status`, `submit`, `morph`, `park`, `unpark`,
   `checkpoint`, `subscribe`) with a **passthrough** of the Pi RPC protocol above,
   routed to one of the 7 named workers.

If you're building an alternative client to the Go TUI, you want Part 2 — it's the
one exposed on the Unix socket. Part 1 is included because every passthrough
command and every broadcast event *is* a Pi RPC message, just with a `"worker"`
key added.

---

## Part 1 — Pi RPC protocol (`harness/`)

### Transport

- Run as `python3 -m harness --mode rpc [...]`. Line-delimited JSON (JSONL) on
  stdin/stdout — one JSON object per line, no framing beyond `\n`.
- The server does **not** require request/response pairing to be synchronous:
  each incoming command is dispatched as its own `asyncio.Task`
  (`harness/rpc.py::serve_stdio`), so responses and event lines can interleave
  across concurrent commands (e.g. a `bash` command's events can arrive between
  a `prompt` command's `message_update` events). Pass `"id"` on a command and it
  is echoed back verbatim on that command's `response` line if you need to
  correlate.
- A malformed line gets a response instead of killing the connection:
  ```json
  {"type":"response","command":"parse","success":false,"error":"Failed to parse command: ..."}
  ```
- On stdin EOF (pipe closed): if the agent is mid-stream it is aborted first,
  then the process exits after any in-flight command tasks finish.

### Message shapes

These are the objects that appear in `"message"` fields of events, in
`get_messages` responses, and in `session.messages()`. Built by
`harness/protocol.py`.

**user** (`user_message`):
```json
{"role": "user", "content": "add a health check endpoint", "timestamp": 1732500000000}
```

**assistant** (`assistant_message`, created empty then filled in during
streaming — see `message_update` below):
```json
{
  "role": "assistant",
  "content": [],
  "api": "ollama",
  "provider": "ollama",
  "model": "llama3.1:8b-instruct-q5_K_M",
  "usage": {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0, "totalTokens": 0,
            "cost": {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0, "total": 0}},
  "stopReason": "pending",
  "timestamp": 1732500000000
}
```
`content` is a list of blocks appended while streaming, three shapes:
```json
{"type": "text", "text": "I'll check the router..."}
{"type": "thinking", "thinking": "..."}
{"type": "toolCall", "id": "call_a1b2c3d4e5f6", "name": "read", "arguments": {"path": "router.py"}}
```
`stopReason` becomes one of `stop | length | toolUse | error | aborted` when the
turn finishes.

**toolResult** (`tool_result_message`):
```json
{
  "role": "toolResult",
  "toolCallId": "call_a1b2c3d4e5f6",
  "toolName": "read",
  "content": [{"type": "text", "text": "1:import os\n2:..."}],
  "isError": false,
  "timestamp": 1732500000123,
  "details": {"path": "/ws/router.py", "totalLines": 40, "truncated": false}
}
```
`details` is only present when the tool result carries structured metadata
(diff, exitCode, todos, etc. — varies per tool, see the tools table below).

**response** (`harness/protocol.py::response`, the reply to every RPC command):
```json
{"type": "response", "command": "prompt", "success": true, "id": "req-1", "data": {"disposition": "started"}}
{"type": "response", "command": "prompt", "success": false, "id": "req-1", "error": "message is required"}
```
`id` is only present if the request set one. `data`/`error` are only present
when non-null.

### Commands

Send `{"type": "<command>", ...}` on a line. `id` is optional on every command
and echoed back on the response.

| command | fields | response `data` |
|---|---|---|
| `prompt` | `message` (string, required, must not start with `@`), `streamingBehavior?` (`"steer"` \| `"followUp"`) | `{"disposition": "started"}` if idle, or `{"disposition": "queued_steer"}` / `{"disposition": "queued_follow_up"}` if the agent was already streaming and a `streamingBehavior` was given. Omitting `streamingBehavior` while streaming is an **error**: `"Agent is streaming; pass streamingBehavior 'steer' or 'followUp'"`. |
| `steer` | `message` (string) | `{}` — delivered after the *current* tool call finishes; any remaining queued tool calls in that assistant turn are skipped with a `toolResult` of `"Skipped due to queued user message."` |
| `follow_up` | `message` (string) | `{}` — queued; delivered only when the agent would otherwise stop (one at a time). If the agent is idle, `follow_up` just starts a normal `prompt`. |
| `abort` | — | `{}` — cancels the stream, kills running `bash` process groups (SIGKILL on the process group). |
| `get_state` | — | `Agent.state()`, see below. |
| `get_messages` | — | `{"messages": [...]}` — the full active-branch message list. |
| `new_session` | — | `{"sessionId": "<new id>"}` — aborts first if streaming, clears todos/checkpoints, starts a fresh `Session`. |
| `compact` | `customInstructions?` (string) | `{"compacted": bool, "reason"?: "nothing to compact", "tokensBefore"?, "tokensAfter"?}` |
| `set_model` | `model` (string, **required**) | `{"model": "<new model>"}` |
| `set_thinking_level` | `level?` (string, default `"off"`) | `{}` |
| `get_available_tools` | — | `{"tools": [{"name","description","parameters"}, ...]}` for the agent's current `tool_names` |
| `bash` | `command` (string), `timeout?` (seconds, default 120) | The raw tool result (`{"content":[...], "details":{"exitCode":0,"truncated":false}}`); on failure the top-level `error` is the tool's error text and `success` is `false`. Streams `bash_execution_update` events with `partialResult` while running. |

`get_state` returns (`Agent.state()`):
```json
{
  "model": {"id": "llama3.1:8b-instruct-q5_K_M", "provider": "ollama"},
  "thinkingLevel": "off",
  "isStreaming": false,
  "isCompacting": false,
  "steeringMode": "all",
  "followUpMode": "one-at-a-time",
  "sessionFile": "/root/.mux/sessions/builder/1732500000000_a1b2c3d4e5f6.jsonl",
  "sessionId": "a1b2c3d4e5f6",
  "sessionName": "builder",
  "autoCompactionEnabled": true,
  "messageCount": 12,
  "pendingMessageCount": 0,
  "role": "BUILDER",
  "contextTokens": 3120,
  "contextLimit": 16384,
  "usage": {"input": 2800, "output": 320, "cacheRead": 0, "cacheWrite": 0, "totalTokens": 3120,
            "cost": {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0, "total": 0}},
  "todos": [{"content": "add /health route", "status": "in_progress"}],
  "checkpoints": ["cp1"]
}
```

Two host-only extensions live in the same dispatcher and are **not** part of
plain Pi (ignored/unknown to a vanilla Pi client):

| command | fields | response `data` |
|---|---|---|
| `morph` | `role` (or `new_role`) — one of `GENERAL PLANNER BUILDER CRITIC SECURITY_ENGINEER DEVOPS_ENGINEER RESEARCHER XR_COCKPIT` | `{"role": "SECURITY_ENGINEER", "messageCount": 12}`. Unknown role: `success:false`, `error: "unknown role X; valid: [...]"`. Swaps the role's system-prompt overlay **in place**, keeping the full conversation (this is the "morph" in `RoleMorphEngine`). |

> **Documentation/code mismatch:** the module docstring at the top of
> `harness/rpc.py` also lists `checkpoint_list` as a MUX extension command.
> It is **not implemented** in `handle_command` — sending it returns
> `{"success": false, "error": "Unknown command: checkpoint_list"}`. To read
> checkpoints, use `get_state`'s `checkpoints` array (labels only) or the
> host-level `checkpoint` action (Part 2), which snapshots to disk.

Unknown `type`/`action`:
```json
{"type": "response", "command": "frobnicate", "success": false, "error": "Unknown command: frobnicate"}
```

### Event stream

Events are pushed unsolicited while a `prompt`/`bash` command runs. Every event
is `{"type": "<event_type>", ...}`.

| event | fields | when |
|---|---|---|
| `agent_start` | — | a `prompt` run begins |
| `turn_start` | — | each loop turn (one assistant completion + its tool calls) begins |
| `message_start` | `message` | a user, assistant, or toolResult message is opened. For user/toolResult messages `message_start` and `message_end` fire back-to-back (they're not streamed); for assistant messages `message_start` fires once with an empty `content: []`, then is followed by `message_update` events, then `message_end` with the completed message. |
| `message_update` | `message` (the assistant message, mutated in place), `assistantMessageEvent` | streamed while the assistant message is being built — see sub-variants below |
| `message_end` | `message` | the message (user/assistant/toolResult) is finalized |
| `tool_execution_start` | `toolCallId`, `toolName`, `args` | a tool call is about to run |
| `tool_execution_update` | `toolCallId`, `toolName`, `args`, `partialResult` | streamed progress from a long-running tool (currently only `bash`, which streams tail-truncated output as it accumulates) |
| `tool_execution_end` | `toolCallId`, `toolName`, `result`, `isError` | tool finished; `result` has the same shape as a `toolResult.content`/`details` pair |
| `turn_end` | `message` (the assistant message), `toolResults` (list of `toolResult` messages from this turn) | a turn completes |
| `queue_update` | `steering` (list of pending strings), `followUp` (list of pending strings) | fires whenever `steer`/`follow_up` add to a queue, or the queue is drained |
| `compaction_start` | `reason` (`"manual"` or `"threshold"`) | before an automatic or `compact`-triggered summarization |
| `compaction_end` | `compacted`, `tokensBefore`, `tokensAfter` | after compaction (also the `data` of a `compact` response) |
| `error` | `error` (string) | non-fatal error during a turn (stream error, salvage failure) **or** `max_turns reached` — the agent loop breaks but the process stays alive |
| `agent_end` | `messages` (the full list of new messages produced by this `prompt` run) | the run loop exits (stop, error, aborted, or max_turns) |
| `agent_settled` | — | always the last event of a run, after `is_streaming` flips back to `false` — safe point to send the next `prompt` |
| `subagent_event` | `role`, `event` (slimmed inner event) | only emitted when the running agent used the `task` tool; forwards `tool_execution_start`/`tool_execution_end`/`agent_end` from each spawned child, with the child's `agent_end.messages` field stripped |
| `bash_execution_update` | `id` (the RPC command's `id`), `partialResult` | streamed only for the top-level `bash` RPC **command** (not the `bash` tool call inside a `prompt` run, which uses `tool_execution_update` instead) |

`assistantMessageEvent` sub-variants inside `message_update` (`kind` is `text`
or `thinking`):
```json
{"type": "start"}
{"type": "text_start",   "contentIndex": 0}
{"type": "text_delta",   "contentIndex": 0, "delta": "I'll add "}
{"type": "text_end",     "contentIndex": 0, "content": "I'll add a /health route."}
{"type": "thinking_start",  "contentIndex": 0}
{"type": "thinking_delta",  "contentIndex": 0, "delta": "..."}
{"type": "thinking_end",    "contentIndex": 0, "content": "..."}
{"type": "toolcall_start",  "contentIndex": 1}
{"type": "toolcall_end",    "contentIndex": 1, "toolCall": {"type": "toolCall", "id": "call_...", "name": "read", "arguments": {"path": "router.py"}}}
{"type": "done", "reason": "toolUse"}
{"type": "error", "reason": "error"}
```
A text/thinking block is only closed (`*_end`) when the next block starts or
the turn ends, so consumers should accumulate deltas by `contentIndex` rather
than assuming one `_start`/`_end` pair before the next.

### Tools

Two tiers, controlled by `--tools`/the `Agent(tools=...)` constructor arg.
Default set is `DEFAULT_TOOLS = PI_ALL + OMP_EXTRA` (all of them). Truncation
matches Pi: **2000 lines or 50KB**, whichever is hit first.

**Pi core** (`PI_CORE = ["read", "bash", "edit", "write"]`, `PI_ALL` adds `grep find ls`):

| tool | parameters | notes |
|---|---|---|
| `read` | `path` (req), `offset?` (1-indexed line, int), `limit?` (int), `hashline?` (bool) | `hashline=true` tags each line `N:hhh|content` (`hhh` = first 3 hex chars of a blake2s digest of the line) — this is what `hashline_edit` anchors against. |
| `write` | `path` (req), `content` (req, string) | Creates parent dirs. Overwrites unconditionally. |
| `edit` | `path` (req), `oldText` (req, non-empty), `newText` (req) | `oldText` must match **exactly one** location (falls back to a whitespace/CRLF-normalized single-match check before failing). Errors if 0 or >1 matches. |
| `bash` | `command` (req), `timeout?` (int seconds, default 120) | Runs via shell in the workspace cwd, own process group (`start_new_session=True`), killed with `SIGKILL` on the group on timeout/abort. Non-zero exit is a tool error. `PAGER=cat` / `GIT_PAGER=cat` forced. |
| `grep` | `pattern` (req), `path?`, `glob?`, `ignoreCase?` (bool), `literal?` (bool), `context?` (int), `limit?` (int) | ripgrep if installed, regex over file contents otherwise. |
| `find` | `pattern` (req, glob e.g. `**/*.py`), `path?`, `limit?` (int, default 1000) | Matches against the relative path or bare filename via `fnmatch`. |
| `ls` | `path?` (default `.`), `limit?` (int, default 500) | Dirs sorted first, then case-insensitive name. |

All file tools are jailed to the agent's `cwd` unless the agent was built with
`jail=False` (`--no-jail` on the CLI): any resolved path outside `cwd` raises
`path escapes workspace: <p>`.

**OMP-style extensions** (`OMP_EXTRA`):

| tool | parameters | notes |
|---|---|---|
| `hashline_edit` | `path` (req), `edits` (req, array of `{anchor, op?, end?, content?}`) | `op` defaults to `"replace"` if omitted; ∈ `replace \| insert_before \| insert_after \| delete`. `anchor`/`end` are `"N:hhh"` strings from a prior `read(hashline=true)`. **All anchors in the batch are validated against the file's current content before any edit is applied** — if a line changed since you read it, the hash won't match and the whole call fails with `stale anchor N:hhh: line N changed since you read it. Re-read with hashline=true.` Overlapping replace/delete spans in the same call are also rejected. |
| `todo` | `todos` (req, array of `{content, status}`, `status` ∈ `pending \| in_progress \| completed`) | **Replaces** the whole list (not additive). Surfaced in `get_state().todos` and in the host's `status().workers[].todos`. |
| `task` | `tasks` (req, non-empty array of `{prompt, role?}`, max 8) | Spawns subagents in parallel (bounded by `HARNESS_SUBAGENT_PARALLEL`, default 2 concurrent). Only available while `depth < max_depth` (default `max_depth=1`, so subagents can't spawn subagents by default) — otherwise errors `subagents unavailable at this depth`. Each spawned child shares the parent's `abort_event`, runs with `role_prompt` from `harness/roles.py::ROLE_PROMPTS` (role defaults to `BUILDER` if omitted/unrecognized), and cannot itself use `task`/`checkpoint`/`rewind`. Returns `{"results": [{"prompt": "<first 200 chars>", "role": "BUILDER", "ok": true, "output": "<first 8000 chars>"}, ...]}`; `ok` is true iff the child's last assistant `stopReason == "stop"`. |
| `checkpoint` | `label?` (defaults to `cp<N>`) | Marks the current session leaf; doesn't discard anything. |
| `rewind` | `label` (req), `lesson` (req) | Discards the conversation back to `checkpoint`'s leaf, but inserts one user message carrying the `lesson` text so the model retains what it learned. Applied at the **end** of the current turn (`_apply_checkpoints`), not immediately. Unknown label errors `unknown checkpoint 'X'; have [...]`. |

Every tool result is wrapped the same way regardless of tool:
`{"content": [{"type": "text", "text": "..."}], "details": {...}}`, and
`run_tool()` never raises — tool failures become `isError: true` toolResult
messages, not RPC-level errors.

---

## Part 2 — Host socket protocol (`cockpit_integration.py`)

This is what the Go TUI, and any alternative client, actually connects to.

### Transport & security

- Unix domain socket at `$MUX_SOCK`, default `/tmp/mux_host.sock`
  (`cockpit_integration.py::SOCK_PATH`).
- **Permissions: `0600`, owner-only.** Set by `umask(0o177)` around the
  `asyncio.start_unix_server` bind, then an explicit `os.chmod(sock, 0o600)`
  right after — belt-and-suspenders so no other local user can read/write it
  regardless of the process umask at start time.
- Line-delimited JSON, **one JSON object per line, both directions**. Max line
  length 1 MiB (`MAX_LINE`); exceeding it gets
  `{"success": false, "error": "line too long"}` and the connection is closed.
- Every connection is a fresh reader/writer pair — you can send multiple
  commands on one connection (each gets its own response line, in the order
  handled — command handling is *not* concurrent per-connection the way Pi RPC
  is, `await self.handle_command(cmd)` is awaited inline before reading the
  next line), or open a new connection per request the way `cockpit.go` does
  (`net.DialTimeout` per `request()` call, one command, one response, close).
- Malformed JSON gets the same `{"type":"response","command":"parse",...}`
  shape as Pi RPC.

Minimal manual probe:
```bash
echo '{"action":"status"}' | nc -U /tmp/mux_host.sock
```

### Host actions

Send `{"action": "<name>", ...}`. Responses are plain dicts (not wrapped in
`response(...)`/`{"type":"response",...}` — that shape is reserved for the Pi
RPC passthrough below).

#### `status`

Request:
```json
{"action": "status"}
```
Response — this is `HostOrchestrator.status()` verbatim:
```json
{
  "host": "HOST: mac-mini-m4 • provider ollama • model llama3.1:8b-instruct-q5_K_M",
  "provider": "ollama",
  "uptime": 842,
  "mux_depth": 3,
  "workers": [
    {"id": "planner", "role": "PLANNER", "status": "idle", "tokens": 1820, "context": 1820,
     "load": 0, "lora": "lora_planner_r16", "spilled": 0, "completed": 4, "score": null,
     "current": null, "todos": []},
    {"id": "builder", "role": "BUILDER", "status": "running", "tokens": 9400, "context": 4210,
     "load": 25, "lora": "lora_builder_r16", "spilled": 0, "completed": 2, "score": 7.4,
     "current": "a1b2c3d4", "todos": [{"content": "add /health route", "status": "in_progress"}]},
    {"id": "critic", "role": "CRITIC", "status": "idle", "tokens": 3100, "context": 900,
     "load": 0, "lora": "lora_critic_r16", "spilled": 0, "completed": 6, "score": 8.1,
     "current": null, "todos": []},
    {"id": "security", "role": "SECURITY_ENGINEER", "status": "idle", "tokens": 0, "context": 0,
     "load": 0, "lora": "-", "spilled": 0, "completed": 0, "score": null, "current": null, "todos": []},
    {"id": "devops", "role": "DEVOPS_ENGINEER", "status": "idle", "tokens": 0, "context": 0,
     "load": 0, "lora": "-", "spilled": 0, "completed": 0, "score": null, "current": null, "todos": []},
    {"id": "researcher", "role": "RESEARCHER", "status": "idle", "tokens": 0, "context": 0,
     "load": 0, "lora": "-", "spilled": 0, "completed": 0, "score": null, "current": null, "todos": []},
    {"id": "xr", "role": "XR_COCKPIT", "status": "idle", "tokens": 0, "context": 0,
     "load": 0, "lora": "-", "spilled": 0, "completed": 0, "score": null, "current": null, "todos": []}
  ],
  "channels": [
    {"channel": "INPUT", "queue": 0, "worker": "planner", "state": "idle"},
    {"channel": "CONTEXT", "queue": 1, "worker": "critic", "state": "active"},
    {"channel": "TOOL", "queue": 0, "worker": "builder", "state": "backpressure"}
  ],
  "quota": [
    {"resource": "Tokens / hour", "used": 14320, "limit": 80000, "state": "healthy"},
    {"resource": "Context (max)", "used": 4210, "limit": 16384, "state": "healthy"},
    {"resource": "Workers busy", "used": 1, "limit": 7, "state": "healthy"},
    {"resource": "Checkpoints", "used": 3, "limit": 0, "state": "NVMe"}
  ],
  "events": ["14:02:01 submit a1b2c3d4 → planner (routed): add health check endpoint", "..."],
  "git_log": ["a1b2c3d mux: add health check endpoint"],
  "roles": ["GENERAL", "PLANNER", "BUILDER", "CRITIC", "SECURITY_ENGINEER", "DEVOPS_ENGINEER", "RESEARCHER", "XR_COCKPIT"]
}
```
`workers[].id` ∈ `planner builder critic security devops researcher xr`
(`mux_router.WORKER_IDS`, fixed 7). `channels[].channel` ∈ `INPUT CONTEXT TOOL`
(`mux_router.Channel`). `events`/`git_log` are the last 14/6 entries
respectively (bounded deques).

#### `submit`

Request:
```json
{"action": "submit", "goal": "add a health check endpoint", "priority": "high"}
```
`priority` is optional (default `"high"`, one of `critical | high | medium | low`
per `Task.priority`'s comment in `mux_router.py`). It only affects which worker
`submit` prefers within the INPUT channel — `planner` for `critical`/`high`,
`researcher` for anything else (`MuxRouter.select_worker`); it falls back to the
least-loaded worker if the preferred one is full or parked.
`goal` is required — empty/missing is an error, not a
`success:false`-wrapped one:
```json
{"status": "error", "error": "goal is required"}
```
Success:
```json
{"status": "submitted", "task": "a1b2c3d4", "goal": "add a health check endpoint"}
```
This only enqueues the task on `planner`'s INPUT channel and returns
immediately — it does **not** wait for completion. Poll `status` (or
`subscribe`) to watch it move through plan → fanout → build → review →
commit/retry/archive. There is no "get result by task id" action over this
socket; the CLI one-shot path (`host_orchestrator.py submit "goal"`) prints
`self.results[tid]` directly instead, which isn't exposed here.

#### `morph`

Request:
```json
{"action": "morph", "worker_id": "researcher", "new_role": "SECURITY_ENGINEER", "keep_context": true}
```
`keep_context` optional, default `true`. Response:
```json
{"status": "morphed", "worker": "researcher", "role": "SECURITY_ENGINEER", "lora": "-"}
```
Unknown `worker_id`:
```json
{"status": "error", "error": "unknown worker researcher2; valid: planner, builder, critic, security, devops, researcher, xr"}
```

#### `park`

Request:
```json
{"action": "park", "worker_id": "builder"}
```
Aborts any in-flight run on that worker, spills its queued tasks to disk
(NVMe), snapshots a checkpoint, sets `status: "parked"`. Response:
```json
{"status": "parked", "worker": "builder", "flushed": 2}
```
`flushed` is the number of tasks spilled.

#### `unpark`

Request:
```json
{"action": "unpark", "worker_id": "builder"}
```
Response:
```json
{"status": "unparked", "worker": "builder", "restored": 2}
```

#### `checkpoint`

Request:
```json
{"action": "checkpoint", "worker_id": "critic"}
```
Writes `{worker, role, lora, tokens, context_tokens, session, leaf, time}` to
`<memory-root>/checkpoints/<worker_id>_<epoch_ms>.json`. Response:
```json
{"status": "checkpointed", "worker": "critic", "file": "./memory/checkpoints/critic_1732500001234.json"}
```

#### `subscribe`

Request:
```json
{"action": "subscribe", "verbose": false}
```
Ack (single line, then the connection is upgraded to a push stream — no more
request/response, just events until you disconnect):
```json
{"status": "subscribed"}
```
Every event any worker's agent emits (Part 1's event stream, each one wrapped
with `"worker": "<id>"`) is broadcast to every subscribed connection, e.g.:
```json
{"worker": "builder", "type": "tool_execution_start", "toolCallId": "call_...", "toolName": "edit", "args": {"path": "router.py", "oldText": "...", "newText": "..."}}
```
By default, **`message_update` and `tool_execution_update`** events are
filtered out (`QUIET` set in `cockpit_integration.py`) because they're
high-volume per-token/per-chunk streams — set `"verbose": true` on the
`subscribe` command to receive them too. A subscribed connection can still be
sent other commands on the same socket in between events (the read loop
doesn't special-case subscribers except for filtering what gets written to
them), but in practice treat a subscribed connection as receive-only.

There is no `unsubscribe` action — close the connection to stop receiving.

### Pi RPC passthrough

Any command with `"type"` (not `"action"`) and no `"action"` key is routed
straight into `harness.rpc.handle_command` against one worker's `Agent`:

```json
{"type": "prompt", "worker": "builder", "message": "add error handling to the router"}
```
`worker` is optional, defaults to `"builder"` if omitted. Any of the Part 1
commands work here — `prompt`, `steer`, `follow_up`, `abort`, `get_state`,
`get_messages`, `new_session`, `compact`, `set_model`, `set_thinking_level`,
`get_available_tools`, `bash`, and the MUX extension `morph`. The response is
the normal Pi RPC `response(...)` object with `"worker"` merged in:
```json
{"worker": "builder", "type": "response", "command": "prompt", "success": true, "data": {"disposition": "started"}}
```
Events emitted by that command (e.g. `message_update`, `tool_execution_start`
while the prompt streams) are **not** returned on this connection inline —
they go out only to `subscribe`d connections, wrapped the same way:
```json
{"worker": "builder", "type": "tool_execution_end", "toolCallId": "call_...", "toolName": "bash", "result": {...}, "isError": false}
```
So a full client needs two connections in practice: one to send commands
(`submit`, or passthrough `prompt`/`steer`/`abort` to a specific worker), and
one `subscribe`d connection to watch what happens.

Unknown worker on a passthrough command:
```json
{"status": "error", "error": "unknown worker frobnicator; valid: planner, builder, critic, security, devops, researcher, xr"}
```
(Raised as `KeyError` by `HostOrchestrator._w`, caught and reformatted —
notice this error shape differs from the host actions' `{"status":"error",...}`
in that it's the *same* shape, just produced from a different exception path;
both end up `{"status": "error", "error": "<message>"}`.)

Unrecognized command (neither a known `action` nor a `"type"` key present):
```json
{"status": "error", "error": "unknown action None"}
```

### What `submit` actually triggers (for context, not part of the wire format)

Useful to know when building a client that watches `subscribe` output:
`submit` always routes to the **planner** worker on the INPUT channel. The
planner's output is parsed for a `{"steps": [...]}` JSON plan
(`morph_engine.parse_plan`); each step gets fanned out to a worker by channel
(`implement`/`design` → builder on TOOL, `research` → researcher/planner on
INPUT). Every TOOL-channel result then gets reviewed by **critic** on CONTEXT;
`morph_engine.parse_score` extracts a `{"score": N, "fix": "..."}` verdict —
`score >= commit_threshold` (default `6.0`) triggers a `git commit` in the
workspace, otherwise the task is requeued to the **same worker** with the
critic's `fix` appended to the goal (up to `HostOrchestrator.max_retries`,
default 2, override via `MUX_MAX_RETRIES`) before being archived to
`<memory-root>/archived.jsonl`.

---

## Building an alternative client

1. Connect to `$MUX_SOCK` (default `/tmp/mux_host.sock`) with a Unix domain
   socket client (`socket.AF_UNIX` in Python, `net.Dial("unix", ...)` in Go,
   `nc -U` for manual probing).
2. Write one JSON object + `\n`. Read one JSON object + `\n` back. Repeat, or
   close and reconnect per request (either works; `cockpit.go` reconnects per
   request).
3. Open a **second** connection, send `{"action":"subscribe"}`, and read a
   continuous stream of `{"worker": ..., "type": ..., ...}` event lines to
   drive a live view — this is the only way to see `message_update`/
   `tool_execution_*` events; they are never returned inline on a command
   connection.
4. To drive a specific worker directly (bypass the planner/critic pipeline),
   use the Pi RPC passthrough (`{"type": "prompt", "worker": "...", ...}`)
   instead of `submit`.
5. Treat `status` as the poll-based source of truth for task/worker state
   (`workers[].status`, `.current`, `.completed`, `.score`) since there is no
   per-task result lookup over the socket.

Quick Python example (status once, then tail events):
```python
import json, socket

SOCK = "/tmp/mux_host.sock"

def request(cmd: dict) -> dict:
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
        s.connect(SOCK)
        s.sendall((json.dumps(cmd) + "\n").encode())
        return json.loads(s.makefile().readline())

print(request({"action": "status"})["host"])

sub = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
sub.connect(SOCK)
sub.sendall(b'{"action":"subscribe"}\n')
f = sub.makefile()
print(f.readline())  # {"status": "subscribed"}
for line in f:
    ev = json.loads(line)
    print(ev["worker"], ev["type"])
```
