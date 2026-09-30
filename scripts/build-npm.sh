#!/usr/bin/env bash
# scripts/build-npm.sh — Reproducible multi-platform npm package build for MUX Cockpit
# Creates 5 platform-specific binary packages + 1 wrapper package in dist/npm/
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DIST_DIR="${ROOT_DIR}/dist/npm"
VERSION="${1:-0.2.0}"

echo "▓▓▓ Building MUX Cockpit npm packages v${VERSION} ▓▓▓"
rm -rf "${DIST_DIR}"
mkdir -p "${DIST_DIR}"

LICENSE_TEXT="MIT License

Copyright (c) 2026 LoveLogic AI

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the \"Software\"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED \"AS IS\", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
"

# 1. Platform binaries and packages
declare -A TARGETS=(
  ["darwin-arm64"]="darwin arm64"
  ["darwin-x64"]="darwin amd64"
  ["linux-x64"]="linux amd64"
  ["linux-arm64"]="linux arm64"
  ["win32-x64"]="windows amd64"
)

for target in "${!TARGETS[@]}"; do
  read -r goos goarch <<< "${TARGETS[$target]}"
  pkg_dir="${DIST_DIR}/mux-cockpit-${target}"
  mkdir -p "${pkg_dir}"

  ext=""
  [[ "$goos" == "windows" ]] && ext=".exe"

  echo "→ Compiling for ${target} (${goos}/${goarch})..."
  (cd "${ROOT_DIR}" && GOOS="${goos}" GOARCH="${goarch}" go build \
    -trimpath -ldflags="-s -w -X main.version=${VERSION}" \
    -o "${pkg_dir}/cockpit${ext}" .)

  # Platform package.json
  cat > "${pkg_dir}/package.json" <<EOF
{
  "name": "mux-cockpit-${target}",
  "version": "${VERSION}",
  "description": "mux-cockpit TUI binary for ${goos}/${goarch} (internal optionalDependency for mux-cockpit)",
  "license": "MIT",
  "repository": {
    "type": "git",
    "url": "git+https://github.com/LoveLogicAILLC/mux-cockpit.git"
  },
  "os": [
    "${goos}"
  ],
  "cpu": [
    "${goarch/amd64/x64}"
  ],
  "files": [
    "cockpit${ext}"
  ]
}
EOF

  echo "${LICENSE_TEXT}" > "${pkg_dir}/LICENSE"
done

# 2. Main wrapper package
MAIN_DIR="${DIST_DIR}/mux-cockpit"
mkdir -p "${MAIN_DIR}/bin"

cat > "${MAIN_DIR}/package.json" <<EOF
{
  "name": "mux-cockpit",
  "version": "${VERSION}",
  "description": "Terminal UI (Charm/BubbleTea) for the MUX Cockpit agent-swarm dashboard. Requires a running mux-cockpit Python host_orchestrator.py backend -- see README.",
  "license": "MIT",
  "repository": {
    "type": "git",
    "url": "git+https://github.com/LoveLogicAILLC/mux-cockpit.git"
  },
  "bin": {
    "mux-cockpit": "bin/mux-cockpit.js"
  },
  "files": [
    "bin/"
  ],
  "engines": {
    "node": ">=14"
  },
  "optionalDependencies": {
    "mux-cockpit-darwin-arm64": "${VERSION}",
    "mux-cockpit-darwin-x64": "${VERSION}",
    "mux-cockpit-linux-x64": "${VERSION}",
    "mux-cockpit-linux-arm64": "${VERSION}",
    "mux-cockpit-win32-x64": "${VERSION}"
  },
  "keywords": [
    "mux-cockpit",
    "tui",
    "agent-swarm",
    "bubbletea",
    "cli"
  ]
}
EOF

cat > "${MAIN_DIR}/bin/mux-cockpit.js" <<'EOF'
#!/usr/bin/env node
"use strict";
const { spawnSync } = require("child_process");

const platformKey = `${process.platform}-${process.arch}`;
const pkgName = `mux-cockpit-${platformKey}`;
const binName = process.platform === "win32" ? "cockpit.exe" : "cockpit";

let binPath;
try {
  binPath = require.resolve(`${pkgName}/${binName}`);
} catch (e) {
  process.stderr.write(
    `mux-cockpit: no prebuilt binary for platform "${platformKey}".\n` +
    `Supported: darwin-arm64, darwin-x64, linux-x64, linux-arm64, win32-x64.\n` +
    `Expected optionalDependency "${pkgName}" was not installed -- ` +
    `check npm install output for a skipped/failed optional dependency.\n`
  );
  process.exit(1);
}

const result = spawnSync(binPath, process.argv.slice(2), { stdio: "inherit" });
if (result.error) {
  process.stderr.write(`mux-cockpit: failed to launch ${binPath}: ${result.error.message}\n`);
  process.exit(1);
}
process.exit(result.status === null ? 1 : result.status);
EOF
chmod +x "${MAIN_DIR}/bin/mux-cockpit.js"

cp "${ROOT_DIR}/README.md" "${MAIN_DIR}/README.md"
echo "${LICENSE_TEXT}" > "${MAIN_DIR}/LICENSE"

echo "✓ All 6 npm packages built in ${DIST_DIR}:"
ls -1 "${DIST_DIR}"
