# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/).

## [0.2.0] - 2026-09-27

### Added
- `GeminiProvider` (`harness/providers.py`): Gemini Flash support via SSE streaming
  (`&alt=sse`), function-call/function-response round-tripping, and `auto` provider selection
  when `GEMINI_API_KEY` is set.
- Self-correction retry loop: `morph_engine.py` gained `parse_fix()` to extract critic rejection
  feedback; `host_orchestrator.py` gained `max_retries` (default 2, override via
  `MUX_MAX_RETRIES`). A rejected TOOL-channel step (score below `commit_threshold`) now requeues
  to the *same* worker with the critic's fix appended to the goal, instead of archiving
  immediately.
- Workspace serialization lock: `HostOrchestrator._workspace_lock` (`asyncio.Lock`), held across
  the full execute→review→commit/retry/archive lifecycle for TOOL-channel tasks, so one task's
  `git add -A` can never sweep up another concurrent task's uncommitted files.
- `run.sh` multi-provider defaults and pre-flight checks: fails fast with a clear message if
  `OPENAI_API_KEY`/`GEMINI_API_KEY` is missing for the selected provider, instead of failing at
  request time with a confusing error.
- Regression tests: `tests/test_host.py::test_review_rejection_retries_then_archives` and
  `tests/test_host.py::test_concurrent_tool_tasks_never_mix_commits`.

### Changed
- Default provider is now `openai` (`gpt-5`) instead of an Ollama-first `auto` fallback.

### Fixed
Found via a recursive-improvement audit (5 parallel review slices: providers, orchestration,
TUI, launch config, self-correction loop):
- `harness/providers.py`: `GeminiProvider` never populated its tool-call tracking dict, so every
  Gemini tool call reported the wrong stop reason (`stop` instead of `toolUse`) and the agent
  never continued after calling a tool.
- `harness/providers.py`: `GeminiProvider` assumed NDJSON streaming; the real API needs
  `&alt=sse`, so the first real call would throw `JSONDecodeError`.
- `harness/providers.py`: `GeminiProvider`'s `toolResult`→`functionResponse` conversion always
  sent the literal name `"tool"`, so Gemini never learned which tool actually ran, breaking
  multi-tool reasoning.
- `harness/providers.py`: `GeminiProvider`'s `functionResponse` used the wrong API role
  (`function` instead of `user`) and omitted the `id` echo, so tool results were likely
  rejected or mismatched by the current Gemini API.
- `host_orchestrator.py`: `park()` called `agent.abort()` before `router.park()`, an asyncio
  callback-ordering race that let aborted/parked tasks be processed as real completions (garbage
  output fed to the critic) instead of being spilled for retry.
- `host_orchestrator.py`: `_git_commit` ran `subprocess.run` synchronously on the shared event
  loop, blocking all 7 workers and the cockpit socket for up to 60s during any commit.
- `host_orchestrator.py`: worker exceptions silently swallowed by `Agent._run()` were counted as
  successful completions, so a failed LLM call produced empty/garbage output scored by the
  critic as real work.

### Known limitations (not fixed, documented in-code)
- Under heavy load, `select_worker`'s fallback can route a review behind a lock-blocked
  TOOL task ahead of it in the same worker's priority queue, deadlocking until that worker's
  queue drains on its own. Marked with a `ponytail:` comment in `host_orchestrator.py`.
