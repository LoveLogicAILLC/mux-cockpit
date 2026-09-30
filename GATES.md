# Gates: MUX Cockpit Release and Credentials Verification

OWNS: GATES.md, scripts/build-npm.sh, scripts/publish-npm.sh, Makefile

Scope: Complete all pending project work including Doppler credentials, persistent npm packaging, test suite verification, and git synchronization.

- [ ] G1: NPM 2FA recovery codes are stored in Doppler project lovelogic dev
  CHECK: doppler secrets get NPM_RECOVERY_CODE_1 NPM_RECOVERY_CODE_2 NPM_RECOVERY_CODE_3 NPM_RECOVERY_CODE_4 NPM_RECOVERY_CODE_5 --project lovelogic --config dev --plain
  EXPECT: dd14777c247d0f55e67c23a25c6190e7f3032056be5c70c810a8048e5dab37be
  EVIDENCE: pending

- [ ] G2: Full test suite passes across Python and Go
  CHECK: python3 -m unittest discover -s tests -v 2>&1 | grep "Ran 30 tests" && go test ./...
  EXPECT: Ran 30 tests in
  EVIDENCE: pending

- [ ] G3: Reproducible npm multiplatform packaging script builds all six packages
  CHECK: bash scripts/build-npm.sh && ls -1 dist/npm/ | sort
  EXPECT: mux-cockpit-win32-x64
  EVIDENCE: pending

- [ ] G4: Git repository is synchronized with GitHub origin main and working tree clean
  CHECK: git status --porcelain && git log -1 --format="%s"
  EXPECT: docs: correct test count (30 Python + 3 Go = 33) after review-phase fixes
  EVIDENCE: pending

- [ ] G5: NPM publication completion via authentication token
  EVIDENCE: pending
