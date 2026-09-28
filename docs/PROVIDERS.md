# Providers

`harness/providers.py` implements four model-calling backends behind one interface. Every
worker (planner/builder/critic/security/devops/researcher/xr) shares the same provider
instance, selected once at host startup by `make_provider()`.

## Selection

```python
# harness/providers.py — make_provider()
kind = (kind or os.environ.get("MUX_PROVIDER") or "auto").lower()
```

| `MUX_PROVIDER` | Class | Notes |
|---|---|---|
| `openai` | `OpenAIProvider` | OpenAI or any OpenAI-compatible endpoint (LM Studio, vLLM, llama.cpp server, OpenRouter) |
| `gemini` | `GeminiProvider` | Google Gemini via `generateContent` streaming |
| `ollama` | `OllamaProvider` | Local Ollama daemon |
| `mock` | `ScriptedProvider` | Deterministic offline brain, no network |
| `auto` (default if unset) | first match below | |

`auto` fallback chain, in order:
1. `GEMINI_API_KEY` set → `GeminiProvider`.
2. Ollama reachable (`GET /api/tags` succeeds) → `OllamaProvider`.
3. `OPENAI_BASE_URL` or `OPENAI_API_KEY` set → `OpenAIProvider`.
4. Otherwise → `ScriptedProvider` (mock).

`run.sh` does **not** use `auto` by default — it hardcodes `MUX_PROVIDER=openai` (override
with `MUX_PROVIDER=ollama|gemini|mock`) and runs its own pre-flight checks before the Python
side ever calls `make_provider()`:

```bash
# run.sh
PROVIDER="${MUX_PROVIDER:-openai}"
```
- `openai`: fails fast (`exit 1`) unless `OPENAI_API_KEY` or `OPENAI_BASE_URL` is set.
- `gemini`: fails fast unless `GEMINI_API_KEY` is set.
- `ollama` / `auto`: if the `ollama` binary is installed, starts `ollama serve` if not already
  running, then `ollama pull "$MODEL"` if the model isn't local. If `ollama` isn't installed
  and `PROVIDER=ollama` was explicit, exits with an error. If `PROVIDER=auto` and ollama is
  missing, it warns and falls through to whatever `make_provider("auto")` resolves to.

## Environment variables

| Var | Used by | Default | Required? |
|---|---|---|---|
| `MUX_PROVIDER` | `make_provider()` | `auto` (Python) / `openai` (`run.sh`) | No |
| `MUX_MODEL` | `run.sh` (passed as `model` arg to `stream()`) | `gpt-5` | No |
| `OPENAI_API_KEY` | `OpenAIProvider.__init__` | `""` | Yes for `openai`, unless `OPENAI_BASE_URL` points at a keyless proxy |
| `OPENAI_BASE_URL` | `OpenAIProvider.__init__` | `http://127.0.0.1:1234/v1` | No — substitutes for the key check in `run.sh`'s pre-flight |
| `GEMINI_API_KEY` | `GeminiProvider.__init__` | `""` | Yes for `gemini` |
| `OLLAMA_HOST` | `OllamaProvider.__init__` | `http://127.0.0.1:11434` | No |

Values above are read directly from `harness/providers.py` and `run.sh` — confirm with
`grep os.environ.get harness/providers.py` if this drifts.

## Cost / latency tradeoffs

Figures below are as measured in `DEMO_REPORT.md` (2026-09-27 session) and haven't been
contradicted by later testing in this repo.

| Provider | Cost | Tool-call accuracy | Best for |
|---|---|---|---|
| OpenAI (`gpt-5`) | $2–5 / M tokens | 97%+ | Production swarm work |
| Gemini (`gemini-2.5-flash`) | ~$0.10 / M tokens | 95% | Cost-effective operations |
| Ollama (`dolphin3:8b` local) | Free | 88% | Offline / prototyping |
| Mock | Free | 90% (scripted, not a real accuracy measure) | Tests / demos |

Ollama has zero network latency but is bottlenecked by local inference throughput (Mac Mini
M4, 8B model). Cloud providers add round-trip latency per streamed chunk but generally finish
a full tool-calling turn faster than an 8B local model on Apple Silicon.

## Provider interface — adding a new one

Every provider is a plain class with:

```python
class YourProvider:
    name = "yourprovider"          # read by lora_for() and status/logging — must be unique

    def __init__(self, ...):
        ...

    async def stream(self, model: str, system: str, messages: List[dict], tools: List[dict],
                     abort: asyncio.Event, thinking: str = "off") -> AsyncIterator[Event]:
        ...
```

`stream()` is an async generator. It must:
- Check `abort` periodically (or race it against the network read, see
  `_stream_lines()` in `providers.py` for the thread→asyncio bridge pattern used by the
  three real providers) and yield `("stop", "aborted")` then `return` if set.
- Yield zero or more of:
  - `("text_delta", str)` — a chunk of assistant text.
  - `("thinking_delta", str)` — a chunk of reasoning/thinking text (optional; omit if the
    backend has no separate reasoning channel).
  - `("toolcall", {"id": str, "name": str, "arguments": dict})` — one per completed tool call.
  - `("usage", (input_tokens: int, output_tokens: int))` — emitted once, near the end.
- Terminate with exactly one of:
  - `("stop", "toolUse")` if any tool calls were emitted.
  - `("stop", "stop")` on normal completion with no tool calls.
  - `("stop", "length")` if the backend truncated on a token/length limit.
  - `("stop", "aborted")` if `abort` fired.
  - `("error", msg)` on unrecoverable failure (do **not** also yield `stop` after this).

You'll also want a `to_wire(system, messages) -> <backend payload>` static method that
converts the harness's internal message list (`user` / `assistant` / `toolResult` roles, see
`harness/protocol.py`'s `message_text()` / `tool_calls()` helpers) into the target API's
request shape — every existing provider follows this pattern; copy `OpenAIProvider.to_wire`
as the simplest template.

Finally, register it in `make_provider()` (`harness/providers.py`) and, if it should
participate in `auto` selection, add a branch to the fallback chain in the order you want it
tried.

## LoRA-per-role (Ollama only)

Ollama has no per-request LoRA parameter, so a "LoRA switch" is implemented as calling a
different Ollama model tag per role. `harness/roles.py` maps roles to tags:

```python
LORA_TAGS = {
    "PLANNER": "lora_planner_r16",
    "BUILDER": "lora_builder_r16",
    "CRITIC":  "lora_critic_r16",
}
```

`morph_engine.lora_for(role, provider, base_model)` only activates this when
`provider.name == "ollama"`: it checks `provider.list_models()` for the tag (or
`f"{tag}:latest"`) and returns `(tag, tag)` if present, else falls back to
`(base_model, None)` — the cockpit UI shows `-` for the LoRA column when a tag isn't
installed. Other roles (SECURITY_ENGINEER, DEVOPS_ENGINEER, RESEARCHER, XR_COCKPIT) have no
tag mapping and always run on the base model.

To install a tag, bake the adapter into an Ollama Modelfile and create it:

```
# Modelfile.planner
FROM llama3.1:8b-instruct-q5_K_M
ADAPTER ./lora_planner_r16.gguf
```

```bash
ollama create lora_planner_r16 -f Modelfile.planner
```

This mechanism only applies to `OllamaProvider` — OpenAI and Gemini have no equivalent
per-role model switch in this codebase.
