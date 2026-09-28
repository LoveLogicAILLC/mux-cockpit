# Security

MUX Cockpit is a personal/homelab agent harness, not an audited product. It has no external
security review, no bug bounty, and no hardening beyond what's described below. Read this
before pointing it at anything you'd mind losing.

## Threat model

Single local user, single local machine. Everything here assumes the process runs as *your*
Unix user, on *your* box, against workspaces *you* chose. It is not designed to isolate
mutually-distrusting users on the same host, and it is not designed to run untrusted/adversarial
LLM output safely — see "bash is not sandboxed" below.

## Workspace jail (file tools)

`harness/tools.py`'s `ToolContext.resolve()` restricts `read`/`write`/`edit`/`hashline_edit`/
`grep`/`find`/`ls` to paths under the agent's `cwd`: every path is resolved to an absolute path
and rejected with `path escapes workspace` unless it *is* the workspace root or a descendant of
it. This stops accidental `../../etc/passwd`-style reads and stops an agent editing files
outside the folder it was pointed at.

**Escape hatch**: `harness/__main__.py --no-jail` turns this off entirely (`Agent(..., jail=False)`
in `harness/agent.py`). It exists because some workflows genuinely need to read/write across
multiple sibling repos or shared config outside the primary workspace — there was no attempt to
build a partial/allow-listed jail, so `--no-jail` is all-or-nothing. Only use it when you trust
the agent's task and the account it's running as.

The jail applies to file *tools* only. It does nothing to the `bash` tool.

## Destructive-bash guard (not a sandbox)

`harness/agent.py:default_bash_policy()` runs before every `bash` tool call and blocks the
command if it contains one of a short list of literal destructive patterns (`DANGEROUS` in
`harness/agent.py`): `rm -rf /`, `rm -rf /*`, `rm -rf ~`, `mkfs`, the fork bomb `:(){ `, `dd
if=/dev/zero of=/dev/`, `> /dev/sda`, `chmod -R 777 /`, `shutdown`, `reboot`.

This is a plain substring match against the raw command string — it catches the obvious literal
forms and nothing else. It does **not** catch equivalent commands phrased differently (variables,
extra whitespace tricks beyond simple normalization, `python3 -c "os.system(...)"`,
piped/chained obfuscation, symlink tricks, etc.). Treat it as a guardrail against the dumbest
accidental self-destruction, not a security boundary.

Set `HARNESS_BASH_POLICY=off` to disable the guard entirely (`default_bash_policy` returns
`None` immediately). Default is `guard` (the check runs).

`t_bash` in `harness/tools.py` runs the command via `asyncio.create_subprocess_shell` in the
agent's `cwd`, inheriting the **full parent environment** (`env={**os.environ, ...}`) — which
means any `bash` command the agent runs can read `OPENAI_API_KEY`/`GEMINI_API_KEY` and anything
else in the host process's environment. There is no timeout the process can't be killed after
(`timeout` param, default 120s, then killed), but nothing stops the command itself from doing
anything your Unix user can do for the duration it runs.

## bash itself is not sandboxed

**This is the load-bearing statement in this document and it is still true**: the `bash` tool
gives the agent your full shell, your full user permissions, and your full environment, subject
only to the substring guard above. There is no container, VM, seccomp profile, chroot, or
capability drop anywhere in this codebase. If you are running an untrusted model, an untrusted
prompt, or code you haven't reviewed, **run the whole stack inside a VM or container** — do not
rely on `HARNESS_BASH_POLICY` or the workspace jail to contain it. Neither was built for that.

## Unix socket permissions

`cockpit_integration.py`'s `CockpitBridge.start_server()` creates the host↔TUI socket
(`MUX_SOCK`, default `/tmp/mux_host.sock`) under `os.umask(0o177)` and then explicitly
`os.chmod`s it to `0o600` — owner read/write only, no group/other access. It's a filesystem-path
Unix domain socket, not a TCP listener, so it is never reachable over the network regardless of
firewall state.

That said: anyone who *can* connect to the socket (your user, or root) gets full Pi RPC
passthrough to every worker, including that worker's `bash` tool. The 0600 permission is the
entire access control — there's no additional auth token or handshake. Don't loosen the
permission bits, and don't put `MUX_SOCK` on a shared/network filesystem.

## API key handling

`OPENAI_API_KEY` and `GEMINI_API_KEY` are read from the environment only
(`harness/providers.py`: `OpenAIProvider.__init__` / `GeminiProvider.__init__`), never from a
config file the repo writes, and never printed. Checked directly:
- Error paths in `providers.py` only surface `type(e).__name__` and the HTTP response body on
  failure (`RuntimeError(f"HTTP {e.code}: {body}")`) — that's the *server's* error response, not
  the outgoing request, so the `Authorization: Bearer <key>` header or Gemini's `?key=` query
  param are never included in anything logged or raised.
- `run.sh`'s pre-flight checks only test whether the variable is set (`-z "${OPENAI_API_KEY:-}"`)
  and never echo its value.
- No `print`/`logging` call anywhere in `harness/providers.py` touches `self.key`.

Standard caveats still apply: it's a plain environment variable, so it's visible to `ps
eauxww`-style env inspection by anything running as your user (including any `bash` tool call
the agent makes — see above), and to anything with read access to your shell's exported
environment or `.env`-sourcing scripts.

## Git commits

`host_orchestrator.py:_git_commit_sync()` only runs `git add -A` + `git commit --no-verify`
locally when `MUX_GIT=1` and the workspace has a `.git` directory. There is no `git push`
anywhere in this codebase — commits never leave the local repo on their own.

## What this does *not* protect against

- Prompt injection causing the agent to run arbitrary (non-blocklisted) shell commands.
- A malicious or buggy provider response exfiltrating data via `bash` (network egress from bash
  is not restricted).
- Multiple local users on the same machine (0600 covers "other local users" for the socket, but
  the file jail and bash guard offer nothing extra there).
- Supply-chain risk in whatever the `bash` tool ends up invoking (package managers, curl-pipe-sh,
  etc.) — none of that is intercepted.

If any of these matter for your use case, put the whole stack behind a VM/container boundary
rather than trusting the checks described above.

## Reporting a vulnerability

There is no dedicated security contact or email for this project yet. If you find a real
vulnerability, please open a private security advisory on the GitHub repository rather than a
public issue, so there's time to land a fix before it's public.
