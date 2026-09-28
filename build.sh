#!/usr/bin/env bash
# build.sh — build the cockpit (native + Mac Mini M4 darwin/arm64)
set -euo pipefail
cd "$(dirname "$0")"
VERSION="$(git rev-parse --short HEAD 2>/dev/null || echo 0.2.0)"
LDFLAGS="-s -w -X main.version=${VERSION}"

echo "▓▓▓ MUX COCKPIT BUILD ▓▓▓  $(uname -sm) • $(go version | awk '{print $3}')"
mkdir -p bin
go mod tidy
go vet ./...
go build -trimpath -ldflags="$LDFLAGS" -o bin/cockpit .
GOOS=darwin GOARCH=arm64 go build -trimpath -ldflags="$LDFLAGS" -o bin/cockpit-darwin-arm64 .
ls -lh bin/cockpit*
echo "run: bash run.sh   |   ./bin/cockpit --status   |   python3 -m harness --mode rpc"
