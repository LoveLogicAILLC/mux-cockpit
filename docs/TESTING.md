# Testing

## Running the suite

From the repo root:

```bash
make test          # both suites
make test-py        # python3 -m unittest discover -s tests -v
make test-go         # go vet ./... && go test ./...
```

(`make test` is just `test-py` + `test-go`, see `Makefile`.) Both suites run with zero
external dependencies — no network, no running Ollama daemon, no API keys. All tests force
`MUX_PROVIDER=mock` (`tests/test_host.py` sets it at import time) or construct a
`ScriptedProvider` directly.

If you only want one file:

```bash
python3 -m unittest tests.test_host -v
python3 -m unittest tests.test_harness -v
go test -run TestName ./...
```

## Philosophy

- **Deterministic swarm behavior via `ScriptedProvider` (mock).** Real LLM output is
  non-deterministic and slow; every host/orchestrator test scripts the "model" as a plain
  Python function `(messages, system) -> {"text": ..., "calls": [...]}` keyed on the role
  extracted from the system prompt (`# Role: (\w+)`). This lets tests assert exact scores,
  exact retry counts, and exact plan shapes without ever hitting a real API.
- **Real git and real Unix sockets, not mocks of them.** `tests/test_host.py`'s `setUp()`
  creates a real temp-dir git repo (`git init`, one commit) and tests exercise the actual
  `subprocess.run(["git", ...])` calls in `host_orchestrator.py` and the actual
  `CockpitBridge` Unix socket server — not stubs. This is deliberate: the bugs this project
  has hit historically (event-loop blocking on synchronous git, socket permission bits, race
  conditions between `park()` and the router) only show up when the real OS primitives are
  in the loop.
- **Two layers.** `tests/test_harness.py` tests the Pi-RPC agent in isolation (tools, the
  agent run loop, provider wire formats against fake HTTP servers, RPC-over-stdio).
  `tests/test_host.py` tests the orchestrator on top: worker pool, MUX router, morphing,
  park/unpark, git integration, and the cockpit socket bridge.

## The two most important regression tests

Both live in `tests/test_host.py`.

### `test_review_rejection_retries_then_archives`

**What it proves:** critic rejection feedback actually loops back to the *same* worker
instead of dead-ending straight to `archived.jsonl` on the first low score.

**Setup:** a scripted provider that always plans one `implement` step, always scores it
`3.0` (below the `6.0` commit threshold) with `fix: "be more thorough"`, and constructs the
host with `max_retries=2`.

**Assertions:**
- `step["retries"] == 2` — the retry counter reached `max_retries` (proves it actually looped
  twice, not zero or one time).
- `step["score"] == 3.0` — the final recorded score is still the (consistently failing)
  rejection score.
- `archived.jsonl` exists — after retries are exhausted, the task is archived, not silently
  dropped or force-committed.
- Event log contains `"retry 1/2"`, `"retry 2/2"`, and `"retries exhausted"` — the retry
  sequence is observable via `h.events`, which the cockpit TUI surfaces live.

**Why it matters:** before this session's fix, `morph_engine.parse_fix()` extracted the
critic's `fix` field but nothing consumed it — a rejected task went straight to
`_archive()` with no second attempt. This test is the only thing in the suite that would
catch a regression where the retry requeue silently stops firing (e.g. someone "simplifies"
the review branch in `host_orchestrator.py` and accidentally removes the retry path).

### `test_concurrent_tool_tasks_never_mix_commits`

**What it proves:** the workspace serialization lock (`HostOrchestrator._workspace_lock`)
actually prevents two concurrent TOOL-channel tasks from interleaving file writes and
`git add -A` calls into the same commit.

**Setup:** two `implement` tasks routed directly onto two different workers (`builder`,
`security`) via `h.router.route(...)`, each scripted to sleep 0.2s, write its own file
(`a.txt` / `b.txt`), then return — simulating a real race window. Both critics score `8.0`
(above threshold, so both commit).

**Assertions:**
- `peak["max_seen"] == 1` — a shared counter incremented/decremented around each script's
  file write proves at most one TOOL task was ever inside its execution window at a time
  (i.e. the lock actually serializes, not just "usually doesn't race").
- Walking every commit (`git log --format=%H`, skipping `setUp`'s initial commit) and
  `git show --name-only` on each: no single commit contains both `a.txt` and `b.txt`.

**Why it matters:** before the lock was added and extended to cover the *full*
execute→review→commit lifecycle (not just execution), one task's `git add -A` could sweep
up another concurrent task's uncommitted files, producing a commit that mixes two unrelated
tasks' output under one message. This is the regression test for that specific
"lock was too narrow" bug class — it fails if the lock is removed, scoped too tightly (e.g.
only around execution, not around the commit step), or replaced with something that doesn't
actually block concurrent TOOL tasks.

Known limitation this test does *not* cover (documented in `host_orchestrator.py`'s
`__init__` as a `ponytail:` comment): under heavy load, `select_worker`'s fallback can still
route a review behind a lock-blocked TOOL task in the same worker's priority queue
(`TOOL > INPUT > CONTEXT` dequeue order), causing a deadlock until that worker's own queue
drains. No test reproduces this deliberately yet.

## Adding a new test

Follow the existing pattern rather than introducing a new one. Template from
`tests/test_harness.py`'s `WS` base class + `test_read_offset_and_truncation_notice`:

```python
class WS(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ws = Path(self.tmp.name)
        self.ctx = T.ToolContext(cwd=self.ws)

    def tearDown(self):
        self.tmp.cleanup()

class TestTools(WS):
    def test_read_offset_and_truncation_notice(self):
        (self.ws / "big.txt").write_text("\n".join(f"line{i}" for i in range(1, 3001)))
        res, err = self.tool("read", path="big.txt")
        self.assertFalse(err)
        ...
```

For orchestrator-level tests, use `tests/test_host.py`'s `HostTest.setUp()` /
`HostTest.host()` helpers (real temp git repo, `ScriptedProvider(delay=0)` by default) and
write your own `script(messages, system)` function if you need role-specific responses —
copy the `role = (re.search(r"# Role: (\w+)", system) or [None, ""])[1]` dispatch pattern
used throughout the file.

Rules of thumb, matching what's already there:
- Always use a real `tempfile.TemporaryDirectory()` workspace, never mock the filesystem.
- Never hit a real network or real Ollama/OpenAI/Gemini endpoint from a unit test —
  `ScriptedProvider` or the `FakeLLM` `BaseHTTPRequestHandler` in `test_harness.py`
  (`TestProviders`) if you're specifically testing wire-format parsing.
- Assert on concrete values (scores, retry counts, event substrings, file contents), not just
  "it didn't throw."
- `asyncio.run(go())` wrapping an inner `async def go():` is the standard shape for anything
  that touches `HostOrchestrator` or `Agent`, both of which are async.
