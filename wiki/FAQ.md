# FAQ

## Why is my TOOL work no longer running in parallel?

By design, as of this session. `HostOrchestrator._workspace_lock`
(`asyncio.Lock` in `host_orchestrator.py`) is acquired before any
TOOL-channel task (builder/security/devops — the workers that actually
mutate files) starts executing, and held across its *entire* lifecycle:
execute → critic review → commit-or-retry-or-archive. Only one TOOL task can
be doing file-mutating work anywhere in the swarm at a time. `planner`,
`researcher`, and `critic` are untouched — they run on INPUT/CONTEXT
channels, which never touch this lock, so plan generation and review still
overlap freely.

This replaced a real bug: without it, two workers' concurrent `git add -A`
could sweep up each other's uncommitted files into the wrong commit. The
lock trades throughput for correctness — see
[Design-Decisions.md](Design-Decisions.md) for the two alternatives that
were considered and rejected, and the one known unfixed edge case (a review
task could theoretically queue behind a lock-blocked TOOL task in the same
worker's priority queue and stall until that queue drains on its own).

## Why did my rejected task retry instead of failing immediately?

Also by design. When `critic` scores a TOOL task's output below
`commit_threshold` (default `6.0`), `morph_engine.parse_fix()` pulls the
critic's suggested correction out of its response, and `HostOrchestrator`
requeues the **same task id** to the **same worker** with that feedback
appended to the goal (`_after()` in `host_orchestrator.py`). Reusing the
same worker means its `Agent` session still has the failed attempt in
context — it isn't starting cold. This happens up to `max_retries` times
(default `2`, override with `MUX_MAX_RETRIES`); only once retries are
exhausted does the task get written to `memory/archived.jsonl` instead of
committed. If you want a rejection to fail immediately with no retry, set
`MUX_MAX_RETRIES=0`.

## Can I use a model besides gpt-5 / gemini-flash / dolphin3?

Yes — those are just `run.sh`'s defaults, not hardcoded. Set `MUX_MODEL` to
whatever your chosen provider accepts:

```bash
MUX_PROVIDER=openai MUX_MODEL="gpt-4o-mini" bash run.sh
MUX_PROVIDER=gemini MUX_MODEL="gemini-2.5-pro" bash run.sh
MUX_PROVIDER=ollama MUX_MODEL="qwen2.5-coder:14b" bash run.sh
```

For `ollama`, `run.sh` will `ollama pull` the tag automatically if you don't
have it locally. `MUX_MODEL` also flows straight into
`HostOrchestrator(model=...)`, which every one of the 7 workers uses as its
base model. Per-role LoRA tags (`harness/roles.py`'s `LORA_TAGS`) are looked
up on top of that base — if a role-specific Ollama tag isn't installed, it
silently falls back to the base model plus the role's system prompt, so
there's no failure mode here, just reduced role specialization.

## What happens if I run out of API budget mid-swarm?

Two independent quota mechanisms exist, and neither drops work:

1. **Host-wide hourly spend cap** (`HostOrchestrator.quota["tokens_per_hour"]`,
   default `80000`, tracked by a sliding window in `tokens_last_hour()`).
   Each worker loop checks this *before* pulling its next task; if the host
   is over budget, that worker spills its just-dequeued task back to disk
   and parks itself.
2. **Per-worker token bucket** (`mux_router.TokenBucket`, default `20000`
   tokens/hour per worker, continuously refilling). `MuxRouter.route()`
   spills new work to `memory/parked/<worker>.jsonl` instead of queuing it
   whenever a worker's bucket drops below `min_tokens` (`500`). The same
   check runs again after every task completes, so a worker parks itself
   the moment it's too depleted to safely take more work.

"Parked" means: queue flushed to disk, session checkpointed, dispatch
stopped — nothing is lost. `overnight_loop()` (cadence configurable, default
900s) automatically unparks a worker once its bucket has refilled past
`min_tokens * 4` and the host-wide window has room again; you can also force
it from the TUI by selecting the worker and pressing `p`. If a TOOL task
happens to be holding the workspace lock when it gets parked mid-run, the
lock is released before parking so it can't strand the rest of the swarm.

## Is my API key safe?

It's read once from the environment (`OPENAI_API_KEY` / `GEMINI_API_KEY`)
into the provider object and used only as an `Authorization` header on
outbound requests — it's never written to `memory/`, session JSONL files,
`host.log`, or sent over the Unix control socket. The socket itself
(`/tmp/mux_host.sock` by default) is created mode `0600`, so only your own
user account can even connect to drive the swarm. See
[`SECURITY.md`](../SECURITY.md) for the full threat model, including the
explicit caveat that `bash` tool execution itself is *not* sandboxed.
