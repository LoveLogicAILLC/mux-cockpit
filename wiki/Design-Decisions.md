# Design Decisions

Lightweight ADR-style log of the non-obvious calls made this session — not
every code change, just the ones where a real alternative existed and the
tradeoff is worth knowing before you touch the surrounding code.

---

## 1. Workspace serialization lock (not snapshot-diff, not per-task worktrees)

**Context.** Two TOOL-channel workers (e.g. `builder` and `security`) could
run concurrently, each mutating files in the same shared workspace. Each
task's commit path runs `git add -A && git commit`. With no coordination,
one task's `-A` add would happily sweep up the *other* task's still
in-progress, uncommitted edits into the wrong commit — silent
cross-contamination, no error, no warning.

**Options considered:**

1. **Serialize TOOL execution** with a single lock held across the whole
   execute → review → commit/retry/archive lifecycle for TOOL tasks.
2. **Snapshot-diff**: let tasks run concurrently, snapshot the workspace
   before each task starts, diff-and-stage only that task's own changed
   files at commit time (`git add <files-that-changed-since-snapshot>`
   instead of `-A`).
3. **Per-task git worktrees**: give each concurrent TOOL task its own
   `git worktree`, let it mutate in isolation, merge/cherry-pick back to the
   main branch on success.

**Decision: (1), serialize.** `HostOrchestrator._workspace_lock`
(`asyncio.Lock`) is acquired the moment a TOOL task starts and released only
at a terminal outcome — commit, archive-after-retries-exhausted, or hard
failure — not just around the execution window. A same-id retry continuing
an already-held lock skips re-acquiring it, so the lock persists across
retries of the same task without a gap where another task could sneak in.

**Why this won over the alternatives:** correctness and simplicity, not
throughput. Snapshot-diff still leaves races on *shared* files if two tasks
happen to touch the same file (which the swarm has no way to predict or
prevent up front) and requires careful path-scoped staging logic that's
easy to get subtly wrong. Worktrees solve isolation properly but add real
operational complexity — merge conflicts between worktrees, working-tree
disk multiplication, and a harder mental model for anyone debugging a
failed run. For a 7-worker local swarm, "only one TOOL task mutates files at
a time" is a correctness guarantee you can verify by inspection; the other
two options are guarantees you have to trust more code to uphold.

**Accepted cost.** `builder`, `security`, and `devops` can no longer mutate
files concurrently — swarm-wide, only one TOOL task's file-mutating work
happens at a time. `planner`, `researcher`, and `critic` are unaffected
(INPUT/CONTEXT channels never touch this lock), so planning and review
still overlap freely; it's specifically implementation throughput that's
serialized.

**Known unfixed edge case.** Under heavy load, `MuxRouter.select_worker`'s
load-aware fallback could route a review task to a worker whose queue has a
lock-blocked TOOL task ahead of it — and since dequeue priority is strictly
`TOOL > INPUT > CONTEXT`, that review sits behind the blocked TOOL task
until *that worker's own queue* drains on its own. This can stall a review
that should otherwise be free to run immediately. Documented in a
`ponytail:` comment at `HostOrchestrator.__init__` rather than fixed — two
plausible upgrade paths if it becomes a real problem: give a
lock-holder's own review task artificially elevated dequeue priority, or
pin lock-holding-task reviews to the dedicated `critic` worker only (no
load-aware fallback for that specific case).

**Status:** implemented, live-verified (two TOOL tasks on different workers
racing to mutate different files — confirmed max concurrent execution = 1,
zero cross-contamination), regression-tested:
`tests/test_host.py::test_concurrent_tool_tasks_never_mix_commits`.

---

## 2. Retry loop: same task id, `MAX_RETRIES=2` default

**Context.** Before this session, a critic rejection (score below
`commit_threshold`) was a dead end — straight to `memory/archived.jsonl`,
no path back to the worker. The critic's feedback was generated
(`parse_score`) but never consumed.

**Decision.** `morph_engine.parse_fix()` now extracts the critic's
suggested correction from a rejection. `HostOrchestrator._after()` requeues
the rejected step back onto the **same worker**, reusing the **same task
id**, with the feedback appended to the goal
(`PREVIOUS ATTEMPT SCORED x/10 — CRITIC FEEDBACK: ...`). This repeats up to
`max_retries` times before falling through to archival.

**Why the same task id, on the same worker, instead of spawning a fresh
retry task:**

- The worker's `Agent` session is persistent and stateful. Reusing the same
  worker means the retry attempt happens in a session that still has the
  failed attempt in its own context — the model can see what it tried and
  why it was rejected, not just a paraphrased instruction from the host.
  A fresh worker or fresh session would lose that.
- Reusing the same task **id** keeps the bookkeeping trivial. `_after()`'s
  review branch looks up `self.results[...]["steps"]` by matching the
  parent task id string; a new id per retry would mean tracking a chain of
  ids back to one logical "step" instead of one dict entry with a
  `retries` counter that just increments in place. It also means a
  same-id retry can skip re-acquiring the workspace lock (see decision 1)
  since it's recognized as a continuation, not a new lock contender.

**Why `MAX_RETRIES=2` (env override `MUX_MAX_RETRIES`), not unlimited:** a
critic that never approves — because the goal is genuinely underspecified,
the workspace lacks a needed file, or the model is just stuck in a loop —
would otherwise retry forever, burning quota with no forward progress and
no clear failure signal. A small bounded default surfaces that as an
archived entry quickly, while still giving a model one real chance to
correct a plausible near-miss. `MUX_MAX_RETRIES=0` disables retries
entirely if you want rejections to fail fast; there's no code-level
ceiling on raising it, though a very high value re-introduces the
runaway-loop risk this default exists to avoid.

**Status:** implemented, live-verified against the real `mock_brain` (not
just a scripted unit test) — first attempt scored 5.1, rejected, retried
automatically, second attempt scored 8.2, committed. Regression test:
`tests/test_host.py::test_review_rejection_retries_then_archives`.

---

## 3. npm distribution: `optionalDependencies` platform packages, not a postinstall downloader

**Context.** The TUI binary (`bin/cockpit`) needs to reach `npm install -g
mux-cockpit` users on macOS (x64/arm64), Linux (x64/arm64), and Windows
(x64) without requiring a local Go toolchain.

**Options considered:**

1. **Per-platform packages wired through `optionalDependencies`** (the
   esbuild/swc/turbo pattern): a thin wrapper package (`mux-cockpit`)
   declares five platform-specific packages
   (`mux-cockpit-darwin-arm64`, `-darwin-x64`, `-linux-arm64`, `-linux-x64`,
   `-win32-x64`) as optional dependencies, each containing nothing but the
   prebuilt binary for that platform plus a tiny `package.json`. npm's
   install-time platform matching (`os`/`cpu` fields) means only the one
   matching package actually downloads; the wrapper's own `bin` entry
   resolves to whichever one landed.
2. **`postinstall` script that downloads the right binary** from a release
   host at install time (the classic pattern for tools without prebuilt
   npm packages).

**Decision: (1).** All six packages (`mux-cockpit` wrapper +
5 platform packages) are prepared as plain npm packages with the binaries
already embedded — no network fetch happens at `npm install` time beyond
npm's own registry resolution.

**Why:** this project has no GitHub Releases and no CI pipeline standing up
either yet — repo publication and npm publication are two separate,
currently-manual steps. A postinstall downloader needs *something* stable
to download from (a release asset URL, a CDN, a hosted bucket) that doesn't
exist yet, plus its own failure handling for network errors, checksum
verification, and retry logic. Shipping the binaries as npm package
contents sidesteps all of that: it works the moment the packages are
published to the registry, using nothing but infrastructure npm already
provides. It also means `npm install` never silently fails offline or in
network-restricted CI environments, since there's no secondary fetch to
fail.

**Consequence.** Package sizes go up front-loaded into the registry
(~4.5–4.8MB per platform binary) instead of being fetched lazily, and
adding a new target platform means preparing and publishing a new package
rather than just updating a download-URL table — an acceptable tradeoff for
a five-platform CLI tool.

**Status:** packages prepared and validated locally (six `package.json` +
binaries under `/tmp/mux-npm-build/`); publication itself is blocked on
completing the npm authentication flow, not on any remaining design work.
