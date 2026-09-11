#!/usr/bin/env bash
# Refuse a commit whose test suite does not pass.
#
# WHY THIS EXISTS. On 2026-08-05 a commit landed on a red suite because the
# command was chained as `pytest | tail -3 && git commit` — `&&` saw tail's
# exit status, not pytest's, so a failing run reported green and the gate never
# fired. The defect that shipped was trivial; committing over a failing suite is
# not. The lesson is that "remember to check the tests" is not a mechanism, and
# this hook is: with it installed, committing on red is impossible rather than
# merely wrong.
#
# The same class is already documented one file over: the chained dispatcher
# captures `rc=$?` with `set +e` around the sub-hook call precisely because
# `if ! cmd; then rc=$?` records the TEST's exit code rather than the command's.
# This hook does the same, for the same reason.
#
# NO AUTODETECTION, BY DESIGN. A pre-commit hook executes on every commit, so a
# hook that guesses `npm test` or `pytest` in an unfamiliar repo is an
# auto-execution surface: clone a repo, commit once, run its code. This hook
# runs ONLY the command a repo explicitly declares, and when nothing is declared
# it skips loudly instead of guessing.
#
# CONFIG — .pre-commit-tests at the repo root, KEY=value, one per line:
#
#     TEST_CMD=python -m pytest -q          # required; nothing runs without it
#     TEST_TIMEOUT=300                      # optional, seconds (default 300)
#     TEST_PATHS=src/**|tests/**            # optional, |-separated globs;
#                                           # when set, the suite runs only if a
#                                           # staged path matches one of them
#
# The file is PARSED, never sourced. Sourcing would execute everything in it at
# read time; parsing means the only thing that runs is the declared TEST_CMD,
# at the moment this hook chooses to run it.
#
# A TIMEOUT IS A FAILURE, NOT A PASS. A suite that did not finish did not tell
# you it was green. Unknown is never clean.
#
# Invoked by .githooks/pre-commit (the chained dispatcher).
#
# Bypass (emergency only): git commit --no-verify
#
# Policy source: https://github.com/Ohio15/dev-standards

set -euo pipefail

repo_root="$(git rev-parse --show-toplevel 2>/dev/null || true)"
[ -z "$repo_root" ] && exit 0

config="$repo_root/.pre-commit-tests"

# ---------------------------------------------------------------------------
# Stage 1 — opt in, or skip loudly.
#
# Silence here would be the worst outcome: a repo with no config would look
# gated when it is not, which is the "clean means the check ran" failure this
# hook exists to prevent. Say plainly that nothing ran.
# ---------------------------------------------------------------------------

if [ ! -f "$config" ]; then
  echo "pre-commit-tests: no .pre-commit-tests config — TESTS DID NOT RUN." >&2
  echo "  Add one to gate commits on your suite:" >&2
  echo "    echo 'TEST_CMD=<your test command>' > .pre-commit-tests" >&2
  exit 0
fi

# ---------------------------------------------------------------------------
# Stage 2 — parse (never source) the config.
# ---------------------------------------------------------------------------

test_cmd=""
test_timeout="300"
test_paths=""

while IFS= read -r line || [ -n "$line" ]; do
  case "$line" in
    ''|'#'*) continue ;;
  esac
  key="${line%%=*}"
  value="${line#*=}"
  # Trim surrounding whitespace from the key only; the value is a command and
  # its internal spacing is significant.
  key="$(printf '%s' "$key" | tr -d '[:space:]')"
  case "$key" in
    TEST_CMD)     test_cmd="$value" ;;
    TEST_TIMEOUT) test_timeout="$(printf '%s' "$value" | tr -d '[:space:]')" ;;
    TEST_PATHS)   test_paths="$(printf '%s' "$value" | tr -d '[:space:]')" ;;
    *)
      echo "pre-commit-tests: ignoring unknown key '$key' in .pre-commit-tests" >&2
      ;;
  esac
done < "$config"

if [ -z "$test_cmd" ]; then
  {
    echo "ERROR: .pre-commit-tests exists but declares no TEST_CMD."
    echo "  A config with no command gates nothing while looking like it does."
    echo "  Add:  TEST_CMD=<your test command>"
    echo
    echo "Bypass (emergency only): git commit --no-verify"
  } >&2
  exit 1
fi

case "$test_timeout" in
  ''|*[!0-9]*)
    echo "ERROR: TEST_TIMEOUT must be a whole number of seconds, got '$test_timeout'" >&2
    exit 1 ;;
esac

# ---------------------------------------------------------------------------
# Stage 3 — path filter, when the repo asked for one.
#
# Opt-in only. Deciding for a repo which staged files "count as code" would be
# a guess, and a wrong guess here skips the suite silently — the exact failure
# mode this hook is against.
# ---------------------------------------------------------------------------

if [ -n "$test_paths" ]; then
  mapfile -t staged < <(git diff --cached --name-only --diff-filter=ACMR)
  if [ ${#staged[@]} -eq 0 ]; then
    exit 0
  fi
  matched=0
  IFS='|' read -r -a patterns <<<"$test_paths"
  for f in "${staged[@]}"; do
    for pat in "${patterns[@]}"; do
      [ -z "$pat" ] && continue
      # shellcheck disable=SC2053  # glob match is intended
      if [[ "$f" == $pat ]]; then matched=1; break 2; fi
    done
  done
  if [ "$matched" -eq 0 ]; then
    echo "pre-commit-tests: no staged path matched TEST_PATHS — suite skipped." >&2
    exit 0
  fi
fi

# ---------------------------------------------------------------------------
# Stage 4 — run it, and capture the REAL exit code.
#
# `set +e` around the call: `if ! cmd; then rc=$?` would record the inverted
# test's status rather than the command's, which is the precise mistake that
# put a red commit on main and motivated this hook.
# ---------------------------------------------------------------------------

echo "pre-commit-tests: running \`$test_cmd\` (timeout ${test_timeout}s)" >&2

timed_out=0
set +e
if command -v timeout >/dev/null 2>&1; then
  timeout "$test_timeout" bash -c "$test_cmd"
  rc=$?
  [ "$rc" -eq 124 ] && timed_out=1
else
  # No `timeout` available. Run without one rather than skip the suite — an
  # ungated commit is worse than a slow one — and say that the bound is absent
  # so a hung suite is recognised as hung rather than as thorough.
  echo "pre-commit-tests: no \`timeout\` binary; running without a time bound" >&2
  bash -c "$test_cmd"
  rc=$?
fi
set -e

if [ "$timed_out" -eq 1 ]; then
  {
    echo
    echo "ERROR: test suite exceeded ${test_timeout}s and was killed."
    echo "  A suite that did not finish did not report green. Treated as a"
    echo "  failure on purpose: unknown is never clean."
    echo
    echo "  Raise the bound in .pre-commit-tests:  TEST_TIMEOUT=<seconds>"
    echo "  Or point TEST_CMD at a faster subset and keep the full suite in CI."
    echo
    echo "Bypass (emergency only): git commit --no-verify"
  } >&2
  exit 1
fi

if [ "$rc" -ne 0 ]; then
  {
    echo
    echo "ERROR: test suite failed (exit $rc) — commit refused."
    echo
    echo "  Command: $test_cmd"
    echo
    echo "Bypass (emergency only): git commit --no-verify"
  } >&2
  exit "$rc"
fi

exit 0
