# Gates: MUX Cockpit Release and Credentials Verification

OWNS: GATES.md, scripts/build-npm.sh, scripts/publish-npm.sh, Makefile, .gitignore

Scope: Complete all pending project work including Doppler credentials, persistent npm packaging, test suite verification, and git synchronization.

- [x] G1: NPM 2FA recovery codes are stored in Doppler project lovelogic dev
  CHECK: doppler secrets get NPM_RECOVERY_CODE_1 NPM_RECOVERY_CODE_2 NPM_RECOVERY_CODE_3 NPM_RECOVERY_CODE_4 NPM_RECOVERY_CODE_5 --project lovelogic --config dev --plain
  EXPECT: dd14777c247d0f55e67c23a25c6190e7f3032056be5c70c810a8048e5dab37be
  EVIDENCE: automatic-evidence=v1; definition-sha256=4439e3af9ef271705a11af07814764fb3af031eb6637f362df9b06c7242c417c; exit=0; EXPECT=matched; output-sha256=6dfc86dd9842ea1870efe5d1ac6ccc930b50968b1e727dc30db4defdd417c347; output-bytes=325; shell=/bin/sh; cwd=/Users/lovelogic/mux-cockpit; path=1b21d6cfc206/25 entries

- [x] G2: Full test suite passes across Python and Go
  CHECK: python3 -m unittest discover -s tests -v 2>&1 | grep "Ran 30 tests" && go test ./...
  EXPECT: Ran 30 tests in
  EVIDENCE: automatic-evidence=v1; definition-sha256=bc93ed23bc033511af75768aaa8f2be407d62993561a3c9381143e923ee1fc26; exit=0; EXPECT=matched; output-sha256=0a61b7b717e0101271057dee120127f7a6a485a80087878ace3034184e7b5a9f; output-bytes=50; shell=/bin/sh; cwd=/Users/lovelogic/mux-cockpit; path=1b21d6cfc206/25 entries

- [x] G3: Reproducible npm multiplatform packaging script builds all six packages
  CHECK: bash scripts/build-npm.sh && ls -1 dist/npm/ | sort
  EXPECT: mux-cockpit-win32-x64
  EVIDENCE: automatic-evidence=v1; definition-sha256=a60701c37b21b918d3a845505cf37c71c1f179bcb11b4689ec23a7d63eae8376; exit=0; EXPECT=matched; output-sha256=2257ea82f64538718c8d2a8f8001f1fe52137e5949b946de9f6ad29c0f4c6b7e; output-bytes=623; shell=/bin/sh; cwd=/Users/lovelogic/mux-cockpit; path=1b21d6cfc206/25 entries

- [x] G4: Dry-run publish validates all 6 package tarballs and metadata
  CHECK: bash scripts/publish-npm.sh --dry-run
  EXPECT: All 6 packages published successfully!
  EVIDENCE: automatic-evidence=v1; definition-sha256=e01df4454929a0631e2d4dbf5290a4d110503673eeac7f8e7a7f18e67ed6b29a; exit=0; EXPECT=matched; output-sha256=d9c8c0320c0679a5caed0df923e4c1395b90dc9cb30a7e8366f66ec9c283183c; output-bytes=4549; shell=/bin/sh; cwd=/Users/lovelogic/mux-cockpit; path=1b21d6cfc206/25 entries

- [x] G5: Git repository is synchronized with GitHub origin main and working tree clean
  CHECK: git status --porcelain && git log -1 --format="%s"
  EXPECT: feat(npm): add reproducible multi-platform build and publish scripts
  EVIDENCE: automatic-evidence=v1; definition-sha256=648f6df3ded36d0a00e74d141c7db1725cf5aaf4c6f1d7338775dbcb2194a17f; exit=0; EXPECT=matched; output-sha256=3ac4dca509bef4141f91aa455f2437a0c17125115c7a4aaba928313def64a63d; output-bytes=81; shell=/bin/sh; cwd=/Users/lovelogic/mux-cockpit; path=1b21d6cfc206/25 entries

- [ ] G6: Live npm registry publication
  EVIDENCE: pending (awaiting user-provided npm automation or granular access token with 2FA bypass)
