#!/usr/bin/env bash
# Reject commits that introduce secrets, scanned with gitleaks against the
# staged diff only. Fast (diff-scoped), redacted output, no banner.
#
# Invoked by .githooks/pre-commit (the chained dispatcher).
#
# Requires: gitleaks (https://github.com/gitleaks/gitleaks)
#   macOS:    brew install gitleaks
#   Windows:  choco install gitleaks
#   Any:      go install github.com/zricethezav/gitleaks/v8@latest
#
# Config precedence (gitleaks default):
#   1. ./.gitleaks.toml at the repo root (consuming repo)
#   2. gitleaks built-in rules (used as a baseline if no .gitleaks.toml)
#
# Policy source: https://github.com/Ohio15/dev-standards

set -euo pipefail

# Locate gitleaks on PATH only. An executable is never resolved from a
# location an environment variable names (GOPATH, HOME): a hostile or
# inherited env block could point it at a substitute binary that exits 0, so
# the scan is bypassed and attacker code runs inside every commit. Absent from
# PATH, the hook fails closed.
gitleaks_bin=""
if command -v gitleaks >/dev/null 2>&1; then
  gitleaks_bin="$(command -v gitleaks)"
else
  {
    echo "ERROR: gitleaks not found on PATH."
    echo
    echo "Install one of:"
    echo "  macOS:    brew install gitleaks"
    echo "  Windows:  choco install gitleaks"
    echo "  Any:      go install github.com/zricethezav/gitleaks/v8@latest"
    echo "            (then put \"\$(go env GOPATH)/bin\" on PATH; the hook does not"
    echo "            look in GOPATH for you)"
    echo
    echo "Then retry the commit. Bypass (emergency only): git commit --no-verify"
  } >&2
  exit 1
fi

# Skip if there's nothing staged (e.g. amend with no changes). gitleaks would
# scan an empty diff and exit 0, but short-circuiting is cleaner.
if [ -z "$(git diff --cached --name-only --diff-filter=ACMR)" ]; then
  exit 0
fi

repo_root="$(git rev-parse --show-toplevel)"
report="$(mktemp -t gitleaks-report.XXXXXX.json 2>/dev/null || echo "${TMPDIR:-/tmp}/gitleaks-report.$$.json")"
trap 'rm -f "$report"' EXIT

config_args=()
if [ -f "$repo_root/.gitleaks.toml" ]; then
  config_args=(--config "$repo_root/.gitleaks.toml")
fi

# Pick the modern `gitleaks git --staged` if available; older v8 builds only
# expose `gitleaks protect --staged`. Both scan only the staged diff, keeping
# the hook fast (sub-second on small diffs).
gitleaks_args=()
if "$gitleaks_bin" git --help >/dev/null 2>&1; then
  gitleaks_args=(git --staged "$repo_root")
else
  gitleaks_args=(protect --staged --source "$repo_root")
fi

set +e
"$gitleaks_bin" "${gitleaks_args[@]}" \
  --redact \
  --no-banner \
  --report-format json \
  --report-path "$report" \
  "${config_args[@]}" \
  >/dev/null 2>&1
rc=$?
set -e

if [ "$rc" -eq 0 ]; then
  exit 0
fi

# Non-zero from gitleaks: either findings (exit 1) or a real error.
{
  echo "ERROR: gitleaks detected potential secrets in your staged changes."
  echo
  if [ -s "$report" ]; then
    # Re-run in human-readable mode so the developer sees redacted findings
    # in the terminal. Exit code is ignored — we already know it failed.
    "$gitleaks_bin" "${gitleaks_args[@]}" \
      --redact \
      --no-banner \
      "${config_args[@]}" \
      2>&1 | sed 's/^/  /' || true
    echo
    echo "Full machine-readable report (redacted): $report"
  else
    echo "  gitleaks exited $rc but produced no report — likely a config or runtime error."
    echo "  Re-run manually: $gitleaks_bin ${gitleaks_args[*]} --redact --no-banner"
  fi
  echo
  echo "Options:"
  echo "  False positive?    Add a narrow rule allowlist to .gitleaks.toml,"
  echo "                     or annotate the line with: # gitleaks:allow"
  echo "  Real secret?       Remove from staged content, rotate the credential,"
  echo "                     follow dev-standards/credential-rotation/PROTOCOL.md."
  echo
  echo "Bypass (emergency only, leaves a trail in reflog): git commit --no-verify"
} >&2

exit 1
