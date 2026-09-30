#!/usr/bin/env bash
# scripts/publish-npm.sh — Automated topological npm publication for MUX Cockpit packages
# Usage:
#   bash scripts/publish-npm.sh [--dry-run] [--otp=<code>]
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DIST_DIR="${ROOT_DIR}/dist/npm"

if [[ ! -d "${DIST_DIR}" ]]; then
  echo "dist/npm not found. Building packages first..."
  bash "${ROOT_DIR}/scripts/build-npm.sh"
fi

EXTRA_ARGS=("$@")

echo "▓▓▓ Publishing MUX Cockpit npm packages ▓▓▓"

# Check auth unless --dry-run
IS_DRY_RUN=false
for arg in "${EXTRA_ARGS[@]}"; do
  if [[ "$arg" == "--dry-run" ]]; then
    IS_DRY_RUN=true
    break
  fi
done

if [[ "$IS_DRY_RUN" == false ]]; then
  echo "Checking npm authentication..."
  if ! npm whoami >/dev/null 2>&1; then
    echo "✗ npm is not authenticated."
    echo "  Please set an auth token via: npm config set //registry.npmjs.org/:_authToken=npm_..."
    echo "  Or run: npm login"
    exit 1
  fi
  WHOAMI=$(npm whoami)
  echo "✓ Authenticated as: ${WHOAMI}"
fi

PLATFORM_PACKAGES=(
  "mux-cockpit-darwin-arm64"
  "mux-cockpit-darwin-x64"
  "mux-cockpit-linux-arm64"
  "mux-cockpit-linux-x64"
  "mux-cockpit-win32-x64"
)

# 1. Publish platform packages first
for pkg in "${PLATFORM_PACKAGES[@]}"; do
  pkg_path="${DIST_DIR}/${pkg}"
  echo "→ Publishing ${pkg}..."
  (cd "${pkg_path}" && npm publish --access public "${EXTRA_ARGS[@]}")
  echo "✓ ${pkg} published"
done

# 2. Publish main package last
echo "→ Publishing main package mux-cockpit..."
(cd "${DIST_DIR}/mux-cockpit" && npm publish --access public "${EXTRA_ARGS[@]}")
echo "✓ mux-cockpit published"

echo "🎉 All 6 packages published successfully!"
