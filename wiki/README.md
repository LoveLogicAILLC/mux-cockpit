# MUX Cockpit Wiki

> **Not a GitHub wiki.** This repo doesn't exist on GitHub yet, so these are
> regular markdown files living in `wiki/` in the main tree — not GitHub's
> separate wiki-repo feature. If/when the project moves to GitHub's native
> wiki, these files can be copied over as-is; until then, treat this
> directory as the knowledge base and edit it with normal PRs.

This is the conversational knowledge base: "how do I get this running,"
"why does it behave this way," "what broke and how do I fix it." For the
formal technical reference (wire protocol, module boundaries, class-level
API), see [`docs/`](../docs/) instead — `docs/ARCHITECTURE.md`,
`docs/PROTOCOL.md`, `docs/PROVIDERS.md`, `docs/TESTING.md`. For the
top-level pitch and quick-start, see the root [`README.md`](../README.md).

## Pages

- **[Getting-Started.md](Getting-Started.md)** — fastest path from `git clone`
  to a running swarm, for someone who hasn't read the full README yet.
- **[FAQ.md](FAQ.md)** — real questions people actually ask: why TOOL work
  isn't parallel anymore, why rejected tasks retry instead of failing, which
  models you can use, what happens when you run out of quota mid-swarm, and
  whether your API key is safe.
- **[Troubleshooting.md](Troubleshooting.md)** — common failure modes and
  their fixes: missing/stale socket, "HOST OFFLINE" in the TUI, provider
  401/403 errors, and a documented Bubble Tea keystroke-batching gotcha when
  scripting the TUI.
- **[Design-Decisions.md](Design-Decisions.md)** — lightweight ADR-style log
  of the non-obvious architectural choices made this session (workspace
  serialization lock, retry-loop design, npm distribution model) with the
  tradeoffs each one accepted.
