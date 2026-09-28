# MUX Cockpit

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Tests: 31 passing](https://img.shields.io/badge/tests-31%20passing-brightgreen.svg)](tests/)
[![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue.svg)](https://www.python.org/)
[![Go 1.22+](https://img.shields.io/badge/go-1.22%2B-00ADD8.svg)](https://go.dev/)
[![Node 14+](https://img.shields.io/badge/node-14%2B-339933.svg)](https://nodejs.org/)

A Pi-compatible agent RPC harness plus a host-driven, 7-worker agent swarm
(planner, builder, critic, security, devops, researcher, xr), scheduled
through a 3-channel priority router and watched over by a Charm/Bubble Tea
terminal dashboard. Point it at OpenAI, Gemini, local Ollama, or a fully
offline mock provider, submit a goal, and watch the swarm plan, implement,
self-review, and — if the critic isn't satisfied — retry with the critic's
own feedback before it ever touches `git commit`.

## Architecture

```
            ┌──────────── cockpit (Go, Bubble Tea) ─────────────┐
            │ agents • MUX channels • quota • todos • events    │
            └──────────────▲──────────────┬─────────────────────┘
                 status 500ms             │ morph/park/submit/prompt  (JSONL, /tmp/mux_host.sock 0600)
            ┌──────────────┴──────────────▼─────────────────────┐
            │ host_orchestrator.py — scheduling, quota, git,     │
            │   workspace lock, critic-feedback retry loop       │
            │ mux_router.py   CH0 INPUT │ CH1 CONTEXT │ CH2 TOOL │
            └──┬──────┬──────┬──────┬──────┬──────┬──────┬──────┘
             planner builder critic security devops research xr    ← each = harness Agent
            ┌──────────────────── harness/ ──────────────────────┐
            │ Pi RPC protocol • tools • steer/follow-up/abort     │
            │ compaction • JSONL session tree • task subagents    │
            └── mock │ openai (SSE) │ gemini (SSE) │ ollama ──────┘
                                                      /api/chat
```

## Quick start

Pick one provider, set its env var, then `bash run.sh`. `run.sh` fails fast
with a clear message if the required key is missing — it will not fall
through to a confusing runtime error.

```bash
# 1. OpenAI (default — MUX_PROVIDER defaults to "openai", MUX_MODEL to "gpt-5")
export OPENAI_API_KEY="sk-..."      # or OPENAI_BASE_URL for an OpenAI-compatible proxy
bash run.sh

# 2. Gemini
export GEMINI_API_KEY="AIza..."
MUX_PROVIDER=gemini MUX_MODEL="gemini-2.5-flash" bash run.sh

# 3. Local Ollama (auto-starts `ollama serve`, pulls MUX_MODEL if missing)
MUX_PROVIDER=ollama MUX_MODEL="llama3.1:8b-instruct-q5_K_M" bash run.sh

# 4. Offline mock — no model, no network, no API key
MUX_PROVIDER=mock bash run.sh

make test    # 28 Python + 3 Go tests
```

`MUX_PROVIDER=auto` (used by `make demo`-style flows) tries Ollama first if
it's installed and reachable, otherwise falls back to Gemini/OpenAI-compatible
(if configured) or the mock provider.

### npm install (TUI only)

The `mux-cockpit` npm package ships **only the compiled Go dashboard binary**,
resolved per-platform via `optionalDependencies` (darwin-arm64, darwin-x64,
linux-x64, linux-arm64, win32-x64) — no postinstall download step. It does
**not** include the swarm; you still need the Python host running separately:

```bash
git clone https://github.com/LoveLogicAILLC/mux-cockpit.git
cd mux-cockpit
export OPENAI_API_KEY="sk-..."          # or GEMINI_API_KEY, or MUX_PROVIDER=ollama/mock
python3 host_orchestrator.py serve --sock /tmp/mux_host.sock
```

Then, in another terminal:

```bash
npx mux-cockpit --sock /tmp/mux_host.sock
```

Running `mux-cockpit` with no host reachable shows an offline/disconnected
state — that's expected, not a bug; start `host_orchestrator.py` first.

```bash
npm install -g mux-cockpit
mux-cockpit --sock /tmp/mux_host.sock              # interactive TUI
mux-cockpit --status --sock /tmp/mux_host.sock      # one-shot JSON status (scripts/CI)
```

## The harness (`harness/`) — Pi standard, OMP upgrades

Drop-in for anything that drives `pi --mode rpc`:

```bash
python3 -m harness --mode rpc [--provider ollama|openai|gemini|mock] [--model M] [--no-session]
python3 -m harness -p "fix the failing test"          # print mode
python3 -m harness --mode json -p "..."               # event stream
```

| Pi parity | |
|---|---|
| RPC | strict JSONL; `prompt` (+`streamingBehavior` steer/followUp), `steer`, `follow_up`, `abort`, `get_state`, `get_messages`, `new_session`, `compact`, `set_model`, `set_thinking_level`, `bash`; parse errors → `{"command":"parse"}`; `@file` prompts rejected |
| Events | `agent_start` `turn_start` `message_start` `message_update`(start/text_*/thinking_*/toolcall_*/done/error) `message_end` `tool_execution_start/update/end` `turn_end` `queue_update` `agent_end` `agent_settled` |
| Messages | `user` / `assistant`(content blocks, usage, stopReason) / `toolResult` — Pi shapes |
| Tools | `read(path,offset,limit)` `write(path,content)` `edit(path,oldText,newText)` `bash(command,timeout)` `grep` `find` `ls` — 2000 lines / 50KB truncation |
| Semantics | steer skips remaining tool calls; follow-ups wait for settle; abort kills bash process groups |
| Context | AGENTS.md / CLAUDE.md (global + root→cwd); auto-compaction at 80% that never orphans a tool result |
| Sessions | JSONL tree (`id`/`parentId`), `~/.mux/sessions` |

| OMP-style upgrades | |
|---|---|
| `read(hashline=true)` + `hashline_edit` | line+content-hash anchors; all anchors verified, stale → rejected, file untouched |
| `task` | parallel subagents with fresh context, role overlay, JSON results |
| `todo` | tracked plan, shown live in the cockpit |
| `checkpoint` / `rewind` | prune a dead-end branch, keep the written lesson |
| Local-model robustness | JSON-as-text tool calls (llama3.1 habit) are recovered into real calls |

Providers: `mock` (offline, deterministic), `ollama` (`/api/chat`, native tool
calls), `openai` (SSE streaming), `gemini` (SSE streaming via
`generativelanguage.googleapis.com`, `functionCall`/`functionResponse`). See
[docs/PROVIDERS.md](docs/PROVIDERS.md) for provider-specific wiring and
env vars.

Safety: file tools jailed to the workspace (`--no-jail` to lift), destructive-bash guard
(`HARNESS_BASH_POLICY=off` to disable), socket mode 0600. **bash itself is not sandboxed** —
run in a VM/container for untrusted work.

## Swarm flow — plan, build, review, **retry**, commit

```
NEEDS INPUT ──submit──▶ planner (CH0) ──plan JSON──▶ steps on MUX
implement/design ──▶ builder (CH2) ──▶ every output reviewed by critic (CH1)
```

- **score ≥ commit_threshold (6.0)** → workspace lock released, `git commit` (needs `MUX_GIT=1`).
- **score < threshold, retries remain** → the critic's rejection feedback
  (`morph_engine.parse_fix`) is appended to the goal and **requeued to the
  SAME worker under the same task id**, so its session still remembers the
  failed attempt. Up to `max_retries` (default **2**, override
  `MUX_MAX_RETRIES`) before giving up.
- **retries exhausted** → archived to `memory/archived.jsonl` instead of
  silently vanishing.

This closes a loop that used to be a dead end: critic feedback was generated
but never fed back to the builder. Now rejected work gets a real second (and
third) chance before anything is thrown away.

The whole execute → review → commit-or-retry-or-archive lifecycle for a
TOOL-channel task holds a single `asyncio.Lock` on the workspace, so one
task's `git add -A` can never sweep up another concurrent task's
uncommitted files. This serializes builder/security/devops file mutation
swarm-wide (INPUT/CONTEXT channels are unaffected). See
[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for the one known unfixed
edge case (`ponytail:` comment in `host_orchestrator.py`'s `__init__`).

`SEEN` → `park`: queue spilled to `memory/parked/<id>.jsonl` + checkpoint;
`unpark` restores in order. Routing to a parked/full/over-quota worker
**spills, never drops**.

Socket (`nc -U /tmp/mux_host.sock`):
```json
{"action":"status"}
{"action":"submit","goal":"..."}
{"action":"morph","worker_id":"xr","new_role":"SECURITY_ENGINEER","keep_context":true}
{"action":"park","worker_id":"builder"}      {"action":"unpark","worker_id":"builder"}
{"action":"subscribe","verbose":false}       // live event stream
{"type":"prompt","worker":"devops","message":"...","streamingBehavior":"followUp"}  // Pi passthrough
```

## TUI keys

| Key | Action |
|---|---|
| `tab` | focus |
| `↑` `↓` | select |
| `m` | morph role |
| `p` | park / unpark |
| `s` | checkpoint |
| `n` | new goal |
| `i` | prompt worker |
| `a` | abort |
| `q` | quit |

`./bin/cockpit --status` prints one JSON snapshot (scripts/CI).

## LoRA note

Ollama has no per-request LoRA flag. Bake each adapter into a tag and the role switch uses it automatically:
```
# Modelfile.planner
FROM llama3.1:8b-instruct-q5_K_M
ADAPTER ./lora_planner_r16.gguf
```
`ollama create lora_planner_r16 -f Modelfile.planner` — absent tags fall back to base model + role prompt (cockpit shows `-`).

## Env

`MUX_PROVIDER` `MUX_MODEL` `MUX_CONTEXT` `MUX_WORKSPACE` `MUX_MEMORY` `MUX_SOCK` `MUX_GIT=1` `MUX_MAX_RETRIES`
`OLLAMA_HOST` `OPENAI_BASE_URL` `OPENAI_API_KEY` `OPENAI_MODEL` `GEMINI_API_KEY` `HARNESS_SUBAGENT_PARALLEL` `HARNESS_BASH_POLICY`

## Docs

- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) — component boundaries, the workspace lock, the known deadlock edge case
- [docs/PROTOCOL.md](docs/PROTOCOL.md) — Pi RPC message/event shapes, the cockpit socket protocol
- [docs/PROVIDERS.md](docs/PROVIDERS.md) — provider wiring, streaming formats, adding a new provider
- [docs/TESTING.md](docs/TESTING.md) — test suite layout, what each test actually proves
- [CONTRIBUTING.md](CONTRIBUTING.md) — dev setup, PR conventions
- [SECURITY.md](SECURITY.md) — threat model, reporting a vulnerability
- [CHANGELOG.md](CHANGELOG.md) — release history
- [wiki/README.md](wiki/README.md) — wiki index

## Status

Actively-developed personal project. Multi-provider support, the
self-correction retry loop, and the workspace serialization lock all landed
in this session and were validated with `make test` plus a live interactive
TUI drive (real keystrokes through plan → fanout → reject → retry → approve
→ commit), not just unit tests. **There is no CI yet** — `make test` is run
locally before every commit; treat the test badge above as "passing as of
last local run," not as a live status check.

## License

[MIT](LICENSE) © 2026 LoveLogic AI
