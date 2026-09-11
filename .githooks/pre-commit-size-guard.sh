#!/usr/bin/env bash
# Reject staged files larger than MAX_BYTES unless listed in .large-files-allowlist.
#
# Invoked by .githooks/pre-commit (the chained dispatcher). Not intended to be
# wired in directly as core.hooksPath/pre-commit; use the dispatcher so future
# sub-hooks (secret scan, lint, etc.) compose cleanly.
#
# Policy source: https://github.com/Ohio15/dev-standards

set -euo pipefail

MAX_BYTES=$((10 * 1024 * 1024))
ALLOWLIST=".large-files-allowlist"

# core.quotepath=off: git would otherwise octal-escape non-ASCII paths, and the
# escaped name would not resolve in the index — the guard must see real paths.
mapfile -t staged < <(git -c core.quotepath=off diff --cached --name-only --diff-filter=ACMR)
[ ${#staged[@]} -eq 0 ] && exit 0

violations=()
for f in "${staged[@]}"; do
  [ -z "$f" ] && continue
  # Submodule gitlinks (mode 160000) are commit pointers, not blobs: there is
  # no object to size in this repository. Skip them explicitly rather than
  # letting cat-file fail on them.
  mode=$(git ls-files -s -- "$f" | awk 'NR==1{print $1}')
  [ "$mode" = "160000" ] && continue
  allow=0
  if [ -f "$ALLOWLIST" ]; then
    while IFS= read -r pat; do
      [[ "$pat" =~ ^[[:space:]]*# ]] && continue
      [[ -z "$pat" ]] && continue
      if [[ "$f" == $pat || "$f" == ${pat%/}/* ]]; then allow=1; break; fi
    done <"$ALLOWLIST"
  fi
  [ "$allow" -eq 1 ] && continue
  # Fail closed: a guard that cannot measure must not pass. Only "key absent"
  # style outcomes are benign; an I/O or index error here is a hook failure.
  if ! sz=$(git cat-file -s ":$f" 2>&1); then
    {
      echo "ERROR: size guard could not read the staged object for '$f':"
      echo "  $sz"
      echo "Refusing to assume 0 bytes. Fix the index state and retry."
    } >&2
    exit 1
  fi
  if [ "$sz" -gt "$MAX_BYTES" ]; then
    hr=$(awk -v b="$sz" 'BEGIN{s="BKMGT";v=b;i=1;while(v>=1024&&i<length(s)){v/=1024;i++}printf "%.1f%s",v,substr(s,i,1)}')
    violations+=("  $f ($hr)")
  fi
done

if [ ${#violations[@]} -gt 0 ]; then
  {
    echo "ERROR: staged files exceed 10 MB. Binaries don't belong in git."
    echo
    printf '%s\n' "${violations[@]}"
    echo
    echo "Options:"
    echo "  Release artifact?   Ship via GitHub Releases (gh release upload)."
    echo "  Build output?       Add to .gitignore."
    echo "  Vendored dep?       Add to .gitignore, restore via package manifest."
    echo "  Source asset?       Add to .large-files-allowlist with a # reason: comment."
    echo
    echo "Bypass (use only with written justification): git commit --no-verify"
  } >&2
  exit 1
fi

exit 0
