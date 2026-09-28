#!/usr/bin/env bash
# run.sh — one-command launch: Ollama / OpenAI / Gemini -> host_orchestrator (harness workers + MUX) -> cockpit TUI
#   MUX_PROVIDER=auto|ollama|openai|gemini|mock   MUX_MODEL=...   MUX_WORKSPACE=.   MUX_SOCK=/tmp/mux_host.sock
set -euo pipefail
cd "$(dirname "$0")"

SOCK="${MUX_SOCK:-/tmp/mux_host.sock}"
MODEL="${MUX_MODEL:-gpt-5}"
MEM="${MUX_MEMORY:-./memory}"
PROVIDER="${MUX_PROVIDER:-openai}"
HOST_PID=""

cleanup() {
  local code=$?
  [[ -n "$HOST_PID" ]] && kill "$HOST_PID" 2>/dev/null && wait "$HOST_PID" 2>/dev/null || true
  rm -f "$SOCK"
  exit $code
}
trap cleanup EXIT INT TERM

echo "▓▓▓ MUX HOST-DRIVEN STACK ▓▓▓"
python3 -c 'import sys; sys.exit(sys.version_info < (3, 10))' || { echo "✗ python3 >= 3.10 required"; exit 1; }
command -v go >/dev/null || { echo "✗ go not found — brew install go"; exit 1; }

if [[ "$PROVIDER" == "openai" ]]; then
  if [[ -z "${OPENAI_API_KEY:-}" && -z "${OPENAI_BASE_URL:-}" ]]; then
    echo "✗ OPENAI_API_KEY not set — export it (or OPENAI_BASE_URL for a proxy), or use MUX_PROVIDER=mock/ollama"; exit 1
  fi
elif [[ "$PROVIDER" == "gemini" ]]; then
  if [[ -z "${GEMINI_API_KEY:-}" ]]; then
    echo "✗ GEMINI_API_KEY not set — export it, or use MUX_PROVIDER=mock/ollama"; exit 1
  fi
fi

if [[ "$PROVIDER" == "auto" || "$PROVIDER" == "ollama" ]]; then
  if command -v ollama >/dev/null; then
    if ! curl -sf http://127.0.0.1:11434/api/tags >/dev/null; then
      echo "→ starting ollama serve"
      (ollama serve >"$MEM.ollama.log" 2>&1 &)   # left running on exit on purpose
      for _ in $(seq 1 30); do curl -sf http://127.0.0.1:11434/api/tags >/dev/null && break; sleep 0.5; done
    fi
    ollama list | awk '{print $1}' | grep -qx "$MODEL" || { echo "→ pulling $MODEL"; ollama pull "$MODEL"; }
  elif [[ "$PROVIDER" == "ollama" ]]; then
    echo "✗ ollama not found — brew install ollama"; exit 1
  else
    echo "! ollama not found — using OpenAI-compatible (if OPENAI_BASE_URL set) or mock provider"
  fi
fi

[[ -x bin/cockpit ]] || bash build.sh

mkdir -p "$MEM"
echo "→ host_orchestrator (logs: $MEM/host.log)"
python3 host_orchestrator.py serve --memory "$MEM" --sock "$SOCK" >>"$MEM/host.stdout.log" 2>&1 &
HOST_PID=$!
for _ in $(seq 1 40); do [[ -S "$SOCK" ]] && break; kill -0 "$HOST_PID" 2>/dev/null || { echo "✗ host died:"; tail -20 "$MEM/host.stdout.log"; exit 1; }; sleep 0.25; done
[[ -S "$SOCK" ]] || { echo "✗ host socket never appeared"; exit 1; }

echo "→ cockpit (q to quit)"
./bin/cockpit --sock "$SOCK"
