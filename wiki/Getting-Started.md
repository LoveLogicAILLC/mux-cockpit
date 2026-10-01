# Getting Started

Fastest path from a fresh clone to a swarm you can watch move. This assumes
nothing beyond what's in the repo — see the root [`README.md`](../README.md)
for the full pitch and architecture diagram if you want more context first.

## Quickest: Install via Homebrew

If you are on macOS or Linux with Homebrew installed, you can skip cloning and building entirely:

```bash
brew install LoveLogicAILLC/tap/mux-cockpit
```

This installs the complete stack (`mux-cockpit`, `mux-host`, and `mux-stack`) into your system `$PATH`.

---

## 1. Prerequisites

- **Python 3.10+** (stdlib-only, no `pip install` needed for the host)
- **Go 1.22+** (only needed to build `bin/cockpit`; `run.sh` builds it for
  you if missing)
- Optional: **Ollama** if you want a fully local/offline run, or an
  **OpenAI** / **Gemini** API key for a hosted model

You do *not* need any of the above to do a first dry run — the `mock`
provider needs nothing but Python.

## 2. Clone and check the layout

```bash
git clone <repo-url> mux-cockpit
cd mux-cockpit
```

Sanity-check the pieces are there: `host_orchestrator.py` (the scheduler),
`cockpit.go` (the TUI), `harness/` (the agent runtime each worker uses),
`run.sh` (the one-command launcher), `Makefile` (test/build shortcuts).

## 3. Run the test suite

```bash
make test
```

This runs `python3 -m unittest discover -s tests -v` (30 tests covering the
harness, the router, and the retry/lock behavior) and `go vet ./... && go
test ./...` (3 tests covering the TUI's socket handling). Both suites use
their own `mock`/`fake` providers — no network, no API keys, no Ollama
required. If this doesn't pass clean, nothing below will work either.

## 4. First run: offline, no keys, no Ollama

```bash
make demo
# equivalent to: MUX_PROVIDER=mock bash run.sh
```

`run.sh` builds `bin/cockpit` if it's missing, starts
`host_orchestrator.py serve` in the background, waits for its Unix socket to
appear, then launches the TUI pointed at it. With the `mock` provider every
worker's "model" is a small deterministic Python function
(`mock_brain` in `harness/providers.py`) — it plans, calls real tools
(`read`/`grep`/`bash`/etc.), and returns scripted-ish text, so you get a
real end-to-end run of the plan → fan-out → review → commit/retry pipeline
without spending a token.

In the TUI:

| Key | Action |
|---|---|
| `n` | submit a new goal to the swarm |
| `tab` / `shift+tab` | move focus between the agents / MUX channels / quota panels |
| `↑` `↓` | move selection within the focused panel |
| `m` | morph the selected worker to a different role |
| `p` | park (or unpark) the selected worker |
| `s` | checkpoint the selected worker |
| `i` | prompt the selected worker directly |
| `a` | abort the selected worker's current run |
| `q` | quit (cleans up the socket) |

Press `n`, type something like `add a health check endpoint`, hit enter, and
watch the `AGENT` table: `planner` picks it up, decomposes it into steps,
those steps route onto `builder`/`researcher` per the `MUX` channel table,
and finished TOOL work flows to `critic` for a score. Score ≥ 6.0 → a git
commit (only if `MUX_GIT=1` was set and the workspace has a `.git`); below
that → one retry, then archival to `memory/archived.jsonl`.

## 5. Run it for real

Pick a provider. `run.sh` defaults to `MUX_PROVIDER=openai MUX_MODEL=gpt-5`
and fails fast with a clear message if the matching key is missing, instead
of erroring deep inside a stream:

```bash
# OpenAI (default)
export OPENAI_API_KEY="sk-..."
bash run.sh

# Gemini
export GEMINI_API_KEY="AIza..."
MUX_PROVIDER=gemini MUX_MODEL="gemini-2.5-flash" bash run.sh

# Local Ollama (pulls the model if you don't have it)
MUX_PROVIDER=ollama MUX_MODEL="dolphin3:8b" bash run.sh
```

To actually watch it commit to a real git history rather than just
archiving/scoring, point it at a workspace with a `.git` and set
`MUX_GIT=1`:

```bash
MUX_WORKSPACE=/path/to/some/repo MUX_GIT=1 bash run.sh
```

## 6. Where things land

- `memory/` (override with `MUX_MEMORY`) — per-worker session JSONL, spilled
  queue files under `memory/parked/`, checkpoints, `memory/archived.jsonl`
  for rejected work, and `memory/host.log`.
- `/tmp/mux_host.sock` (override with `MUX_SOCK`) — the control socket the
  TUI and any scripting talk to, mode `0600`.
- `bin/cockpit` — the built TUI binary (`bash build.sh` or `make build` to
  rebuild).

## 7. Next steps

- Hit something unexpected? Check [Troubleshooting.md](Troubleshooting.md)
  first.
- Curious *why* certain things work the way they do (serialized TOOL
  execution, retry loop, etc.)? See [Design-Decisions.md](Design-Decisions.md).
- Common questions about models, quota, and safety: [FAQ.md](FAQ.md).
- Full protocol and architecture reference: [`docs/`](../docs/).
