# Troubleshooting

## Socket never appears / `run.sh` exits with "host socket never appeared"

`run.sh` starts `host_orchestrator.py serve` in the background and polls for
`/tmp/mux_host.sock` (or `$MUX_SOCK`) to show up, up to 10 seconds. If it
times out or the host process dies first, it tails the last 20 lines of
`$MUX_MEMORY/host.stdout.log` for you. The usual causes, in order of
likelihood:

- **Missing/wrong provider key.** `run.sh` pre-flight-checks
  `OPENAI_API_KEY`/`GEMINI_API_KEY` for `MUX_PROVIDER=openai`/`gemini`
  *before* even trying to start the host, and exits with an explicit
  message rather than letting the host crash — but if you bypassed `run.sh`
  and ran `python3 host_orchestrator.py serve` directly, that guard doesn't
  run, and you'll instead see the host actually start (the socket exists
  fine) but every task fail with a provider error. See the 401/403 section
  below. Full pre-flight matrix: [`docs/PROVIDERS.md`](../docs/PROVIDERS.md).
- **`go` not found.** `run.sh` needs `go` on `PATH` only to build
  `bin/cockpit` if it's missing (`bash build.sh`). `brew install go` and
  retry.
- **Ollama selected but not installed**, `MUX_PROVIDER=ollama` with no
  `ollama` binary → explicit `brew install ollama` message, host never
  starts.
- **A real Python exception on startup** (bad `MUX_MODEL`, permissions on
  `$MUX_MEMORY`, etc.) — this is what the tailed log will show; it's the
  actual traceback, not a generic failure.

**Stale socket file left over from a crashed run:** not actually a blocker.
`CockpitBridge.start_server()` in `cockpit_integration.py` removes any
existing file at the socket path before binding, so the *next* `serve`
always succeeds regardless of leftovers. What a stale file *does* cause is
a confusing moment if you try to connect to it while no host is running —
you'll get a plain connection-refused error (see next section), not a
missing-file error, because the inode still exists even though nothing's
listening on it. If that's bothering you, `rm -f /tmp/mux_host.sock` is
always safe when you're sure no host is running.

## "HOST OFFLINE" in the TUI

This is `cockpit.go`'s explicit offline screen — it fires whenever the last
status poll (every 500ms) fails to connect or times out, and shows the raw
connection error plus the socket path it tried. It is not a crash: the TUI
keeps polling and will flip back to the live view automatically the moment
a host starts answering on that socket, no restart needed. Causes:

- Host isn't running yet, or you pointed `--sock`/`MUX_SOCK` at the wrong
  path (compare what you passed to the TUI vs. what you passed to
  `host_orchestrator.py serve`).
- Host crashed after startup — check `memory/host.log` /
  `memory/host.stdout.log` for the reason.
- Socket permissions: it's created `0600` (owner-only) by design — if
  you're running the TUI as a different user than the host, this is
  expected, not a bug (see [`SECURITY.md`](../SECURITY.md)).

## Provider errors (401 / 403)

Auth failures don't crash the host — they surface per-task. Under the hood,
`urllib`'s `HTTPError` is caught and re-raised as `RuntimeError(f"HTTP
{code}: {body}")` inside `harness/providers.py`'s stream reader, which the
agent's error-event path turns into `w.last_error`. Look for a line like:

```
<worker> ERROR RuntimeError: HTTP 401: {"error": {...}}
```

in the TUI's event pane, `memory/host.log`, or the socket's `status.events`
field. The task that triggered it gets archived (not silently swallowed —
`_after()` explicitly refuses to feed error text into the planner/critic
parsers) so you can find it in `memory/archived.jsonl` too. Fix is almost
always the key itself (expired, wrong project/org scoping, or exported in
the wrong shell); `run.sh`'s pre-flight check catches the *missing* case but
can't catch an invalid-but-present key. Provider-specific setup and
model-string requirements: [`docs/PROVIDERS.md`](../docs/PROVIDERS.md).

## TUI keystrokes don't register when driving it programmatically

If you're scripting the cockpit (integration tests, demo automation,
anything that writes raw bytes to the TUI's stdin faster than a human
types), you can hit a real Bubble Tea input-handling gotcha: Bubble Tea's
terminal reader can coalesce several fast, contiguous non-space keystrokes
that arrive in the same read into a **single** `tea.KeyMsg`, instead of
delivering one `KeyMsg` per character the way `cockpit.go`'s handlers
(`updateNormal`/`updateInput`/`updateMorph`, all switching on
`k.String()`) expect. The practical symptom: a fast-piped string like
`addutils` can land as one `KeyMsg` whose `.String()` doesn't match any of
the single-character cases those handlers check, so the input silently does
nothing instead of typing each letter.

This isn't a cockpit.go bug to fix — it's an inherent property of how
terminal input readers batch bytes under load, and it only shows up with
artificially fast automated input; a human typing at any normal pace never
triggers it. The reliable fix for scripting: send keystrokes through
**`tmux send-keys`** one at a time (or with small delays between calls)
rather than piping a raw byte stream into the process's stdin — this is
what was used to live-verify the full plan → fan-out → reject → retry →
approve → commit pipeline end-to-end through real TUI keystrokes in this
session, and it reproduced correctly every time. If you need to script
something more elaborate, prefer driving the host directly over the Unix
socket (`echo '{"action":"submit","goal":"..."}' | nc -U
/tmp/mux_host.sock`) instead of the TUI at all — see
[`docs/PROTOCOL.md`](../docs/PROTOCOL.md) for the socket action list.
