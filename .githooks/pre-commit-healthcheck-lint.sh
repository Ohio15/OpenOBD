#!/usr/bin/env bash
# Reject commits whose staged release-config.yml ships a non-single-shot
# health_check.command. STANDARDS section 7 mandates a single request — Docker's
# `retries`/`interval` and the workflow's outer 90s loop already do retry. A
# nested `for $(seq ...); do curl ...; sleep 5; done` inside the command burns
# the outer budget (the ToolVault + APM scar that motivated IMPL-9).
#
# Invoked by .githooks/pre-commit (the chained dispatcher).
#
# Tool requirements (best-effort, in order):
#   1. yq   — preferred for accurate YAML AST traversal
#   2. python (with PyYAML) — fallback; ships with most Python distros
#
# If neither is available, the hook fails closed with an actionable install
# hint rather than silently passing.
#
# Bypass (emergency only): git commit --no-verify
#
# Policy source: https://github.com/Ohio15/dev-standards
# Standards rule: section 7 — "Healthcheck command rules" #1 (single request,
# no shell loop)

set -euo pipefail

# ---------------------------------------------------------------------------
# Stage 1 — collect candidate files (added/copied/modified/renamed only).
# ---------------------------------------------------------------------------

mapfile -t staged < <(git diff --cached --name-only --diff-filter=ACMR)
[ ${#staged[@]} -eq 0 ] && exit 0

candidates=()
for f in "${staged[@]}"; do
  [ -z "$f" ] && continue
  case "$f" in
    release-config.yml|release-config.yaml) candidates+=("$f") ;;
    */release-config.yml|*/release-config.yaml) candidates+=("$f") ;;
  esac
done

[ ${#candidates[@]} -eq 0 ] && exit 0

# ---------------------------------------------------------------------------
# Stage 2 — pick a YAML parser. Prefer yq; fall back to python+PyYAML.
# We need to read .docker.deploy.health_check.command as a string (returns
# empty if the path doesn't exist) without crashing on non-docker surfaces.
# ---------------------------------------------------------------------------

parser=""
python_bin=""

if command -v yq >/dev/null 2>&1; then
  parser="yq"
elif command -v python3 >/dev/null 2>&1 && python3 -c "import yaml" >/dev/null 2>&1; then
  parser="python"
  python_bin="python3"
elif command -v python >/dev/null 2>&1 && python -c "import yaml" >/dev/null 2>&1; then
  parser="python"
  python_bin="python"
else
  {
    echo "ERROR: pre-commit-healthcheck-lint needs yq or Python+PyYAML."
    echo
    echo "Install one of:"
    echo "  yq:       https://github.com/mikefarah/yq (brew install yq / choco install yq)"
    echo "  Python:   pip install pyyaml   (PyYAML must import as 'yaml')"
    echo
    echo "Bypass (emergency only): git commit --no-verify"
  } >&2
  exit 1
fi

# read_health_check_command <file>
# Print the value of .docker.deploy.health_check.command on stdout.
# If the key is absent, print the literal token __ABSENT__ instead — that lets
# us distinguish absent from explicit empty string (both are valid cases that
# should pass the lint, but knowing which keeps debug output honest).
read_health_check_command() {
  local file="$1"
  if [ "$parser" = "yq" ]; then
    # yq prints "null" for missing keys; the // alternative coerces that to
    # __ABSENT__ with exit 0. A NON-ZERO exit is a parse failure (corrupt or
    # unreadable YAML) and must fail closed, exactly like the Python backend
    # below — never fold it into the benign "absent" token.
    local out rc=0
    out="$(yq -r '.docker.deploy.health_check.command // "__ABSENT__"' "$file" 2>&1)" || rc=$?
    if [ "$rc" -ne 0 ]; then
      printf 'healthcheck-lint: failed to parse %s with yq (exit %s): %s\n' "$file" "$rc" "$out" >&2
      exit 2
    fi
    printf '%s' "$out"
  else
    "$python_bin" - "$file" <<'PYEOF'
import sys, yaml
path = sys.argv[1]
try:
    with open(path, "r", encoding="utf-8") as fh:
        doc = yaml.safe_load(fh)
except Exception as exc:
    sys.stderr.write("healthcheck-lint: failed to parse %s: %s\n" % (path, exc))
    sys.exit(2)
if not isinstance(doc, dict):
    print("__ABSENT__", end="")
    sys.exit(0)
node = doc
for key in ("docker", "deploy", "health_check", "command"):
    if not isinstance(node, dict) or key not in node:
        print("__ABSENT__", end="")
        sys.exit(0)
    node = node[key]
# command must be a string (or coerce-able). Anything else is a violation we
# surface by printing the raw repr so the developer can see what's wrong.
if not isinstance(node, str):
    print("__NONSTRING__:%r" % (node,), end="")
    sys.exit(0)
# Preserve embedded newlines exactly so the bash side can detect block scalars.
print(node, end="")
PYEOF
  fi
}

# Find the (1-based) line number of the first occurrence of `health_check:` in
# the staged file content, then report the line index of the `command:` value
# directly under it. Used purely for human-readable error output. Best-effort:
# falls back to the file's first line if we can't pin it down.
locate_command_line() {
  local file="$1"
  local hc_line cmd_line
  hc_line="$(grep -n -E '^[[:space:]]*health_check:[[:space:]]*$' "$file" | head -n1 | cut -d: -f1 || true)"
  if [ -z "$hc_line" ]; then
    echo 1
    return
  fi
  # Scan up to 20 lines after health_check: for the first command: key.
  cmd_line="$(awk -v start="$hc_line" 'NR>start && NR<=start+20 && /^[[:space:]]*command:/ {print NR; exit}' "$file" || true)"
  if [ -n "$cmd_line" ]; then
    echo "$cmd_line"
  else
    echo "$hc_line"
  fi
}

# ---------------------------------------------------------------------------
# Stage 3 — apply the rule set to each candidate.
# Reject conditions (any one is enough):
#   R1. Multi-line block scalar (the YAML scalar contains a newline). Block
#       scalars are how `for ... do ... done` constructs get embedded.
#   R2. Contains any shell-loop keyword as a whole word: `for `, `while `,
#       `seq `, `until `, `done`, `do `.
#   R3. Does NOT begin (after optional leading whitespace) with one of the
#       allowed single-shot invocation prefixes:
#         curl, wget, bash -c "curl, bash -c 'curl, sh -c "curl, sh -c 'curl
#       Allow lines that are entirely comments (start with #) — useful when a
#       repo intentionally disables healthcheck via a commented stub.
# Pass conditions:
#   P1. Key absent (lib/electron/mcp surfaces have no .docker.deploy).
#   P2. Single-line value matching R3's allow-list.
# ---------------------------------------------------------------------------

violations=()

for f in "${candidates[@]}"; do
  cmd="$(read_health_check_command "$f")"

  # P1: absent — pass cleanly.
  if [ "$cmd" = "__ABSENT__" ]; then
    continue
  fi

  # Reject: command exists but is not a string scalar.
  if [[ "$cmd" == __NONSTRING__:* ]]; then
    raw="${cmd#__NONSTRING__:}"
    line="$(locate_command_line "$f")"
    violations+=("$f|$line|R-type|.docker.deploy.health_check.command must be a string, got: $raw")
    continue
  fi

  # R1: multi-line scalar.
  if [ "$(printf '%s' "$cmd" | wc -l | tr -d ' ')" -gt 0 ]; then
    line="$(locate_command_line "$f")"
    # Show the first line of the offending block for context.
    first_line="$(printf '%s' "$cmd" | head -n1)"
    violations+=("$f|$line|R1|multi-line block scalar found (first line: $first_line)")
    continue
  fi

  # Strip leading whitespace once for keyword + prefix checks.
  trimmed="${cmd#"${cmd%%[![:space:]]*}"}"

  # If the command is a comment-only line, treat as pass (intentional disable).
  if [[ "$trimmed" =~ ^# ]]; then
    continue
  fi

  # R2: shell-loop keywords. Match as whole tokens to avoid false positives
  # like "do_something" or "fortune". Order matters slightly (longer first).
  bad_kw=""
  for kw in 'for ' 'while ' 'until ' 'seq ' 'do ' 'done'; do
    # Use a regex with leading start-or-space and trailing space-or-semicolon-or-end.
    # `done` is a reserved word in shell, almost always followed by EOL/`;`/space.
    case " $trimmed " in
      *" $kw"*) bad_kw="$kw" ; break ;;
    esac
    # Special handling for `done` — also catch `;done` and bare-end `done`.
    if [ "$kw" = "done" ]; then
      case "$trimmed" in
        *';done'*|*'; done'*|*' done') bad_kw="done" ; break ;;
      esac
    fi
  done

  if [ -n "$bad_kw" ]; then
    line="$(locate_command_line "$f")"
    violations+=("$f|$line|R2|shell-loop keyword '${bad_kw% }' present in single-line command")
    continue
  fi

  # R3: single-shot prefix allow-list.
  ok=0
  case "$trimmed" in
    curl\ *|wget\ *) ok=1 ;;
    'bash -c "curl '*|"bash -c 'curl "*) ok=1 ;;
    'sh -c "curl '*|"sh -c 'curl "*) ok=1 ;;
    'bash -c "wget '*|"bash -c 'wget "*) ok=1 ;;
    'sh -c "wget '*|"sh -c 'wget "*) ok=1 ;;
  esac

  if [ "$ok" -ne 1 ]; then
    line="$(locate_command_line "$f")"
    violations+=("$f|$line|R3|command must start with curl/wget (single-shot). Got: $trimmed")
    continue
  fi
done

# ---------------------------------------------------------------------------
# Stage 4 — report or exit clean.
# ---------------------------------------------------------------------------

if [ ${#violations[@]} -eq 0 ]; then
  exit 0
fi

{
  echo "ERROR: release-config.yml health_check.command failed shape lint."
  echo
  echo "STANDARDS section 7, Healthcheck command rules:"
  echo "  #1  Single request, no shell loop — let Docker's retries +"
  echo "      interval (and the workflow's outer wait) handle retry."
  echo
  for v in "${violations[@]}"; do
    IFS='|' read -r vf vl vr vmsg <<<"$v"
    echo "  $vf:$vl  [$vr]  $vmsg"
  done
  echo
  echo "Fix template (drop in to .docker.deploy.health_check.command):"
  echo "  command: curl -fsS -m 5 http://localhost:<port>/<path>"
  echo
  echo "The reusable workflow already retries on .docker.deploy.health_check.interval_seconds"
  echo "for up to .docker.deploy.health_check.timeout_seconds. Nested retry inside"
  echo "the command itself eats that budget — exactly the ToolVault/APM bug class."
  echo
  echo "Bypass (emergency only): git commit --no-verify"
} >&2

exit 1
