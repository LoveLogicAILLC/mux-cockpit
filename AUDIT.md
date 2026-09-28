# v0.1 → v0.2 audit (what was broken, what replaced it)

| # | v0.1 defect | Impact | v0.2 |
|---|---|---|---|
| 1 | `json.dumps(Task)` in planner call | `submit()` crashed on first use | harness Agent; tasks are plain prompts |
| 2 | `worker.last_output` never defined | overnight loop crashed when a worker ran | tracked per worker |
| 3 | `morph_worker("xr")` — xr not a host worker | README's own example → KeyError | 7 workers, consistent everywhere |
| 4 | Two separate `ContextStore`s | "keeps session context" never worked | one persistent session per worker; morph swaps role only (tested) |
| 5 | Sync `ollama.chat` inside async loop | froze socket + TUI during every call | streaming in a thread, never blocks the loop |
| 6 | `asyncio.wait` on bare coroutines in `dequeue` | error on 3.11+, leaked getters stole tasks | single-event strict-priority dequeue |
| 7 | Backpressure / low-token → `return` | tasks silently dropped | spill to NVMe, restored on unpark (tested) |
| 8 | `park()` only printed; tokens never counted | quota + parking were decorative | real token bucket, hourly window, park/unpark |
| 9 | Unparseable critic score → `6.5` "pass" | garbage auto-committed | unscored = not shippable → archived |
| 10 | `lora=` per request | not an Ollama feature | role → Modelfile tag, base fallback |
| 11 | Planner parse `if "steps" in output` + mock plan | real plans ignored | balanced JSON extraction by key |
| 12 | TUI 100% hardcoded, `m`/`p`/`tab` no-ops | cockpit showed fiction | live socket, all keys wired, offline state |
| 13 | Host `print()` into the TUI's tty | corrupted the screen | logs to `memory/host.log` |
| 14 | Socket world-default perms | any local user could drive agents | 0600 |
| 15 | `run.sh`: pkill wrong process, unreachable kill, `exit 0` masks errors | orphaned host, false success | trap kills host PID, propagates exit |
| 16 | "Git commit" printed only | nothing committed | real `git commit` with `MUX_GIT=1` (tested) |
| 17 | No tools at all ("You have tools: bash…") | agents couldn't act | Pi tool set + OMP extras, jailed |
