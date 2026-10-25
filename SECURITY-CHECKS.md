# SECURITY-CHECKS — the per-session security register

**What this is.** The enumerated list of defect SHAPES that the per-session
security gate (`/security-gate`) checks a change against. It is the first
tier of the two-tier review model described in `REVIEW-MODEL.md`: the gate
answers "does this change introduce a shape we already know?"; the routine
audit answers "what shapes do we not know yet?" and feeds new ones back here.

**The rule for adding a rule.** Every entry must be expressible as a grep or a
mechanical test. If it cannot be, it is not a gate item; it belongs to the
audit. A rule whose check cannot be written down is an opinion.

**Provenance.** Every rule below was paid for by a finding that reached a
review round on a Ron-owned repo. Sources: shared-brain `5cd9c6a6` (SEC-4 (c)
S9-1, 18 rounds, ~47 findings), `def54bd7`/`9364075b` (AIBrowser session
lending, 12 rounds), `89c287fd`/round-18 A1 (AIBrowser control plane),
cortex-hooks R38–R50. Dates are when the rule was written down, not when it
was first learned.

**Canonical copy:** `dev-standards/security/SECURITY-CHECKS.md`. Each repo
carries a copy at its root as `SECURITY-CHECKS.md` (install with
`dev-standards/install.sh`). The gate reads the repo copy and falls back to the
canonical one, and reports which it used — a repo running on the fallback has
not installed its register, and that is finding #1.

---

## How the gate uses this file

For each rule the reviewer answers one of three things, and says which:

- **CLEAR** — the check ran against the diff and found no instance.
- **FINDING** — an instance, with `file:line`, the concrete exploitation
  scenario, the sibling enumeration (grep output, not memory), and severity.
- **NOT EXAMINED** — the check could not be run (diff truncated, file
  unreadable, surface not in the bundle). Unknown is never clean; this is
  reported, never folded into CLEAR.

Severity: CRITICAL = exploitable now by the default population; HIGH =
exploitable with one precondition an attacker controls; MEDIUM = defence-in-
depth gap; LOW = hygiene. The gate is CAPPED at two passes per branch (see
`REVIEW-MODEL.md`); the second pass covers only the fixes to the first.

---

## The register

### SC-01 · Partial coverage of a surface family (the sibling defect)
**Shape.** A guard, fence, projection, or constructor is applied to the site a
finding NAMED and not to its siblings: the other routes on the same resource,
the other walkers over the same tree, the other write sinks for the same
table, the other model-facing emitters. Ten rounds running this was the
governing pattern; every family was larger than the finding said (3 walkers,
4 sinks, 4 routers, 5+ hand-built replies, 8+5 literals reported as 2).
**Check.** Grep the CONSTRUCTION SHAPE (the route registration form, the
`content: [{ type: 'text'` literal, the `INSERT INTO <table>` form), not the
symptom. Every member must route through ONE blessed constructor. The diff
must include a MECHANICAL test that fails on any future member (a source scan
that derives the site list from the tree, never a hard-coded list).
**Fix form.** One chokepoint; site list derived, not enumerated in prose.
**Since.** 2026-09-02.

### SC-02 · Authorization on every route of a resource, per action
**Shape.** A resource with N routes where fewer than N carry the authorization
check; a mutation surface guarded by "any valid token" with no scope,
self-versus-other, or last-principal check; a DELETE that removes the
precondition of a deliberate bootstrap hole. Round-18 A1: 1 of 4 principal
routes guarded, exploitable from a zero-scope principal, which the migration
made the DEFAULT post-upgrade population.
**Check.** Grep the router registration form for the resource; list every
route; each must call the guard BEFORE the store call. Identity-management
routes must additionally refuse self-escalation and refuse to disable or
delete the last enabled principal.
**Fix form.** Guard by default at the router, opt-out explicitly per route
with a comment naming why.
**Since.** 2026-09-03.

### SC-03 · A guard whose result does not gate the action
**Shape.** A check that is computed and discarded; a validation whose return
value is not consulted; a constraint stated in a comment or a constant
("it is one constant so there cannot be a fourth sink") with a regression test
scoped to ONE file. R16-5 → R17-5 found the fourth sink in another file.
**Check.** For every new guard, follow its return value to the action it is
supposed to gate. For every "there cannot be another X" claim, grep for X
across the whole tree, not the file the comment is in.
**Fix form.** Move the constraint to the STORAGE or EMIT chokepoint and make
it FAIL SHUT (the store throws on an unstamped write).
**Since.** 2026-09-02.

### SC-04 · Fail closed on I/O and parse errors
**Shape.** "Could not read the alert store" renders as "there are no alerts";
a missing or corrupt policy file makes the guard allow everything; a catch
block that swallows and returns the empty/benign value.
**Check.** ENOENT is the ONLY benign read failure. Every other error must keep
the last known state, log, and raise a counter. Grep every `catch` in the
diff: a catch that returns `[]`, `{}`, `null`, `false`, or `0` on a security
path is a finding unless it is the ENOENT branch.
**Fix form.** Distinguish ENOENT from everything else; coerce toward LOUDER
(bad severity → warning, not info).
**Since.** 2026-09-02.

### SC-05 · A trust field a caller can write is not a trust field
**Shape.** Provenance, role, or "internal" flags carried in a JSON body, an
MCP parameter, or a WAL envelope; a closed set enforced by inspecting BYTES
("contains a valid span") rather than by the CONSTRUCTION PATH. R17-1: a
caller concatenated raw bytes around a span the server had just minted for
them.
**Check.** For every trust-bearing field, find where it is set. If any path
from request/parameter/file to that field exists without a server-side stamp,
it is a finding. A predicate derived from content is not a control.
**Fix form.** Module-private `Symbol()` stamps that `JSON.parse` cannot
produce; branded frozen classes; branch on TYPE, not on shape.
**Since.** 2026-09-02.

### SC-06 · Neutralise-and-clamp does not confer provenance; inheritance never does
**Shape.** A sanitised value treated as trusted because it was sanitised; a
child inheriting "internal" from a parent; a declaration removing an explicit
external signal; a strict mode that is weaker than the legacy mode for some
input.
**Check.** Provenance must be MONOTONE: any merge can only ADD external-ness.
A legacy floor (`if (legacyIsExternal(o)) return true;`) must precede any
declaration check, so strict ≥ legacy pointwise. Grep the metadata merge
sites; each must be a `metadata || existing` merge that cannot clear a flag.
**Fix form.** Monotone-by-construction merge; fence bodies from a provenance
constant, never from a bare `{content}`.
**Since.** 2026-09-02.

### SC-07 · Unknown origin is never clean
**Shape.** A report, a health verdict, or a gate that says "clean" because the
check did not run; a prompt-facing surface that fences only when it can prove
the content is external (it must fence unless it can prove it is
server-authored).
**Check.** Every verdict-producing function must return a THREE-valued result
(clean / finding / not-examined) or carry an explicit "examined" flag. Grep
for boolean "ok" returns on verification paths.
**Fix form.** Track unexamined separately; on model-facing prompts, fence
unless provably server-authored.
**Since.** 2026-09-02.

### SC-08 · Counters must not lie at any discard site
**Shape.** A counter that compares deduplicated ids to a slot count, so a
complete set reports incomplete (and mints a never-silence alert any key
holder can flood); a metric that answers a different question than its name.
**Check.** For every counter in the diff, state the question it answers and
verify the thing counted is that thing. Every discard site must increment the
matching discard counter.
**Fix form.** Count the thing the question is about, keep the other honest
number alongside it.
**Since.** 2026-09-02.

### SC-09 · Truncate then fence; any transform of a fenced body re-mints its MAC
**Shape.** Clipping a fenced body after fencing (slices the closing marker; the
attacker chooses the cut with padding); a helper that alters a fenced body
without re-minting; code outside the fence module emitting a marker pair.
**Check.** Grep for marker-pair emission outside the fence module. Every
helper that alters a body must call the re-mint; every helper that must not
alter one must use the outside-own-spans mapper.
**Fix form.** One fence module owns emission; `remintSpan` /
`mapOutsideOwnSpans` are the only two shapes.
**Since.** 2026-09-02.

### SC-10 · Bound every walk over attacker-sized input
**Shape.** A g-flagged regex with a backreference over `[\s\S]*?`; recursion or
nested loops over request-sized data with no cap; a scan that fails toward
"allow" when it gives up. R17-3+6: one construct was both a fence-deletion
primitive and a quadratic blow-up (1012 ms → 5.96 ms after the fix).
**Check.** Grep the diff for regexes with `[\s\S]*`, `.*?` with backreferences,
and unbounded `while`/recursion over external input. Each needs a hard cap
whose overflow branch FAILS TO THE SAFE SIDE (neutralise unconditionally).
**Fix form.** Monotone `indexOf` discovery, anchored fixed-shape header
regex, hard caps on input size and candidate count.
**Since.** 2026-09-02.

### SC-11 · No secret in a log, alert, error body, command line, or exfil sink
**Shape.** A bearer token in a watcher log line; a credential URL whose host
can be overridden by an environment variable (the exfil sink); a secret
echoed in an exception message; a secret written to a file by a tool call.
**Check.** Grep the diff for every log/alert/throw/argument site that
interpolates a variable named like a token/key/secret/password. Every
outbound credential must go to an ALLOW-LISTED host literal.
**Fix form.** Allow-list the host; redact at the emit chokepoint; never pass
secrets on a command line.
**Since.** 2026-08-21.

### SC-12 · The environment is never a control channel
**Shape.** A limit, an allow-list extension, a bypass, a home directory, or a
model choice read from `process.env` / `$env:`. Claude Code honours a
repo-committed `.claude/settings.json` env block, so a hostile repository sets
the variable for exactly the sessions working in it.
**Check.** Grep the diff for `process.env.` and `$env:`. Each read must be on
the BENIGN list (tuning, labelling, the credential itself, escalate-only
flags) or journal-and-ignore. Limits and overrides come from an operator-owned
file outside every repo (`~/.cortex/hooks.json`) resolved through the OS
account database, or from tokens under the protected floor.
**Fix form.** Operator file + protected-floor tokens; env may only ESCALATE.
**Since.** 2026-08-05.

### SC-13 · A comment asserting a property the code lacks
**Shape.** "This is fenced", "the run aborts here", "there cannot be a fourth
sink" — written in good faith by the person fixing the previous round, and
false. The single most common defect across 18 rounds; it appeared in three
of one session's own fixes.
**Check.** For every claim in a comment or commit message in the diff, name
the observable boundary (the wire, the DB row, the journal, the child
process) and verify there. Assert the invariant AT THE RETURN, not by
re-deriving the arithmetic that is supposed to guarantee it. A high-quality
commit message is not evidence of coverage.
**Fix form.** Boundary verification in a test; invariant assertion at return.
**Since.** 2026-09-02.

### SC-14 · Test quality: helper tests, fixture tests, decorative tests
**Shape.** A test that exercises a helper instead of the surface; a fixture
that supplies the state it then measures; a test no mutation can falsify; a
source-scan guard a COMMENT satisfies; an inherited mutation quietly deleted
when the code it anchored on was rewritten.
**Check.** For every new test, revert the behaviour AT THE REAL CALL SITE and
confirm the test goes red. A skipped/missed mutation anchor is a FAILURE,
never a kill. Batteries carry at least one deletion AND one retention mutant.
A verifier or guard whose tests include no mutation battery (revert the
guard, confirm the test fails) is NOT EXAMINED, not CLEAR.
**Fix form.** Drive the real handler; anchor by enclosing function; re-anchor
inherited mutations, never delete them.
**Since.** 2026-09-02.

### SC-15 · A finding's proposed fix validated against the real system
**Shape.** Applying a reviewer's suggested fix verbatim when it would have
deleted exactly the never-silence alerts it protects (FL-6), or asserting on a
key the storage layer remaps so the probe can never go true (FL-3).
**Check.** Before applying a proposed fix, probe the REAL dependency for the
semantics relied on (the allow-list contents, the key map, the SQL predicate).
A test double mirrors the assumption, not reality.
**Fix form.** Probe first; the fix names what it probed.
**Since.** 2026-09-02.

### SC-16 · A helper family with a strong and a weak spelling; the enforcement
site uses the weak one
**Shape.** The module ships `programName()` and `verbForm()`, `commandClauses()`
and `splitClauses(lexShell())`, a shared lexer and a private tokenizer — and
the tier-3 consumer calls the one that does less. The comment beside the
strong spelling says it is "the ONLY function that answers…", and the grep
says otherwise.
**Check.** For every exported pair where one function is documented as the
blessed answer, `grep -n` every call site of the OTHER; each hit outside the
defining module is a finding unless a comment at the site states why the weak
form is correct there. A source-derived test enumerates the roster.
**Fix form.** Delete or un-export the weak spelling; derived call-site test.
**Since.** 2026-09-09.
**Origin.** audit-cortex-hooks-2026-09-09 (findings: verbForm/programName,
splitClauses/commandClauses, workspace-guard tokenize).

### SC-17 · Quoting treated as semantics
**Shape.** A detector skips a word because `quoted === true`, in a position
(argv[0], subcommand, flag, operand) where the shell hands the program the
identical argv either way. Written to fix a false positive on
`git commit -m "…"` that the POSITION check already prevented.
**Check.** `grep -n "\.quoted" hooks/*.ts`; every hit must be in a
switch/wrapper/value position. Fixture pairs (`X`, `'X'`, `"X"`, `X'Y'`) for
every flag and verb in every detector must classify identically; a test that
asserts that pairwise.
**Fix form.** Gate on argv position; `quoted` only where it distinguishes an
option's VALUE.
**Since.** 2026-09-09.
**Origin.** audit-cortex-hooks-2026-09-09.

### SC-18 · A guard's own inputs at a weaker protection level than the guard
**Shape.** A guard reads a runtime config file, spawns a script, or renders a
cache into the model — and that file sits in a warn-only tree, an uncovered
directory, or off the floor entirely. The env channel for the same value was
carefully closed; the file it resolved to was never checked against the
policy.
**Check.** Derive, from the source, every path a hook reads at runtime
(`resolve(here`, `readFileSync`, `spawn(`, `homePath(`), and assert each is
covered by `floorGlobs()` or `enforced_file_path_globs`, with a reasoned
allow-list whose criterion includes "reaches no model-facing prompt" as well
as "gates no enforcement decision".
**Fix form.** Runtime inputs on the enforced side; executed scripts on the
code floor; policy parity test between canon and mirror.
**Since.** 2026-09-09.
**Origin.** audit-cortex-hooks-2026-09-09 (findings: workspace-layout.json,
canonical-hostnames.json, scripts/**, governance/check.mjs, conformance.json,
hooks-win mirror).

### SC-19 · A stand-in parser models a subset of the real grammar and treats
the unmodelled construct as data
**Shape.** The lexer that stands in for bash has no backslash case, no `>|`,
matches `-c` but not `-lc`, drops here-strings and heredocs fed to an
interpreter, and does not know `eval`. Each unmodelled construct silently
becomes an opaque word, i.e. an allow.
**Check.** A fixture table of shell grammar constructs — backslash escape,
every redirection operator in the bash manual, combined short flags,
long-option prefixes, `eval`, here-string, heredoc into
`bash`/`sh`/`node`/`python`/`psql`, path-qualified and wrapper-prefixed
programs — each asserted to classify the same as its plain spelling in every
detector. Any construct absent from the table is a finding when added to the
lexer.
**Fix form.** Model the construct, or treat its presence as an evasion signal
that fails closed.
**Since.** 2026-09-09.
**Origin.** audit-cortex-hooks-2026-09-09.

### SC-20 · A source-scan guard anchored on one spelling of the construct it
checks for
**Shape.** The SC-12 enforcer matches `process.env.NAME` and is blind to
`env: NodeJS.ProcessEnv = process.env`; the audit verifier inspects report
bytes rather than construction path. The mechanical check exists, is green,
and does not see the shape.
**Check.** For every source-scan test, write the guarded construct in each
alternate spelling the language allows (parameter form, destructuring,
indexed access, alias import) and confirm the scan is red for each; a
self-test that the scan found ≥ N real sites.
**Fix form.** Anchor the scan on the READ (a typed value, a single accessor)
rather than the token; route the construct through one chokepoint the scan can
name.
**Since.** 2026-09-09.
**Origin.** audit-cortex-hooks-2026-09-09.

### SC-21 · An append-only enforcement journal with no cap, justified by a
benign-traffic rate
**Shape.** "Rows are rare (27 in 19 days), so no rotation." The rate is a
property of an agent that is not trying; the guarded party writes a row per
matched call at tool-call rate, and a full volume silently stops every other
best-effort journal on it.
**Check.** `grep -n "appendFileSync" hooks/*.ts`; every enforcement writer has
a byte cap or a ring AND a named overflow condition that is itself never
silent.
**Fix form.** Cap that refuses new rows and raises `<journal>_full`; never
drop-oldest on evidence.
**Since.** 2026-09-09.
**Origin.** audit-cortex-hooks-2026-09-09.

### SC-22 · A plain-object map indexed by attacker-chosen text
**Shape.** A lookup table (`{}` literal, `Record<K, …>` under any key type,
or `x in obj`) keyed by a program name, verb, subcommand, tool name or any
other string the guarded party supplies. `__proto__`, `constructor`,
`toString` and every other `Object.prototype` name resolve to a truthy
non-handler; the code then calls it (throw → every fail-open consumer allows)
or interpolates it (a native-function body reaches a message). The canon
lexer had two, and one word took the whole shell-side plane down.
**Check.** `grep -nE "\[(program|programName\(|verb|sub|name|key|tool)[A-Za-z]*\]" hooks/*.ts`
and `grep -nE "\bin [A-Z_]+\b" hooks/*.ts`; every hit keyed by external text
must be a `Map`, an `Object.create(null)` table, or guarded by
`Object.hasOwn` on the read. A fixture that feeds `__proto__`, `constructor`,
`toString`, `hasOwnProperty` as the program/verb/sub to every detector and
asserts the plain-command verdict, plus a derived scan over every
module-level table read with a variable key, in every spelling of the
declaration and in every module that reads it (SC-20 applies to the scan
itself). Reference fixture: cortex-hooks `hooks/hostile-program-names.test.ts`.
**Fix form.** Null-prototype tables or a `Map`; `hasOwn` at the lookup; the
lexer asserts the resolved handler is a function.
**Since.** 2026-09-16.
**Origin.** audit-cortex-hooks-2026-09-15 (CRITICAL; approved by Ron
2026-09-16).

### SC-23 · A hook's registration or tool family is narrower than the
predicate it implements
**Shape.** The detector matches every MCP alias, every file tool, every
process channel — and the settings matcher, the `CONTENT_TOOLS` set, or the
`SHELL_TOOLS` set the entry point consults names fewer. The conformance check
confirms the matcher is present, not that it covers the predicate. One added
alias or one added tool channel and a block-tier rule enforces nothing, while
the CHANGELOG says "every row enforced".
**Check.** For every PreToolUse guard, derive the predicate's tool set from
source (the regex or set the core exports) and the registered matcher from
the live settings block the generator emits; assert matcher ⊇ predicate. One
exported tool-family constant per channel (shell, content, MCP file/process)
consumed by every guard and by the resource layer, with a derived membership
test that fails when a member is added to one set and not the others.
**Fix form.** Generate the settings matcher from the predicate; one family
constant; conformance asserts inclusion, not presence.
**Since.** 2026-09-16.
**Origin.** audit-cortex-hooks-2026-09-15 (HIGH; approved by Ron 2026-09-16).

### SC-24 · A transport exemption that never checks the destination
**Shape.** "Under ssh it is remote, so it is allowed"; "`-H ssh://` means
another machine". The exemption is keyed on the transport's presence and
never on where it goes, so `localhost`, `127.0.0.1`, `::1`, the machine's
own hostname and `ssh://localhost` all earn the remote exemption while acting
on this host. The refusal text advertises the exemption as the intended
alternative.
**Check.** `grep -n "remote = true\|ssh://\|isRemote\|transport" hooks/*.ts`;
every exemption must classify the destination against a loopback/own-host set
before it applies. Fixture pairs (`ssh <host> X` vs `ssh localhost X`,
`-H ssh://<host>` vs `-H ssh://127.0.0.1`) for every rule that carries a
remote exemption, asserted to differ.
**Fix form.** Resolve the destination first; loopback and own-hostname are
local; only then apply the transport exemption.
**Since.** 2026-09-16.
**Origin.** audit-cortex-hooks-2026-09-15 (HIGH; approved by Ron 2026-09-16).

### SC-25 · A third-party CI Action referenced by a floating tag or branch
rather than a commit SHA
**Shape.** `uses: someone/action@v1` (or `@main`) in a workflow. A tag is a
mutable pointer the action's owner — or whoever takes over the account or the
repo — can move to any code, which then runs with the workflow's token,
secrets and the runner's network on the next scheduled or push-triggered run.
The tj-actions/changed-files compromise (2025-03) moved every version tag to
a credential-dumping payload; every consumer pinned to a tag ran it, no
consumer pinned to a SHA did. The register has no rule for this: the OpenOS
gate pass 1 reviewer (2026-09-16) noticed `jlumbroso/free-disk-space@54081f13…
# v1.3.1` as good practice that nothing required, in a workflow that also
carries `actions/checkout@v4` and `actions/upload-artifact@v4` unpinned.
**Check.** `grep -rnE "uses:\s*[^ ]+@[^ ]+" .github/workflows/` and any
reusable-workflow `uses:`. For every reference NOT under `actions/`,
`github/` or the repository's own org: the ref after `@` is a 40-hex commit
SHA followed by a `# vX.Y.Z` comment naming the version it pins. For
first-party (`actions/*`, `github/*`) a SHA is preferred and a major tag is
LOW; the failure is the third-party floating ref. Dependabot or Renovate
configured for `github-actions` is the evidence that pinned SHAs will not
rot; its absence is a MEDIUM sibling finding, not a reason to float.
**Fix form.** Pin to the SHA with the version comment; enable the
`github-actions` ecosystem in `dependabot.yml` so the pin is bumped by PR,
never by hand. Severity: HIGH for a third-party action that receives a
token or secret or runs on a self-hosted runner; MEDIUM otherwise.
**Since.** 2026-10-05.
**Origin.** gate-openos-2026-09-16 (session git-20260916-112947-da1f1cc6bb25;
approved by Ron 2026-10-05).
**Standard.** OpenSSF Scorecard `Pinned-Dependencies`; GitHub docs
"Security hardening for GitHub Actions — Using third-party actions".

### SC-29 · A CI step that uses a privileged secret is gated on event type, not
on ref and environment
**Shape.** A signing key, deploy key, or registry-write credential is consumed
by steps whose only gate is `github.event_name != 'pull_request'`, so
`workflow_dispatch` (or a same-repo branch push or PR) on an unreviewed ref
uses the production secret; or the secret is repository-level rather than an
environment secret restricted to the protected branch; or the job that
materialises it also runs third-party actions. A dispatch runs the dispatched
ref's OWN copy of the workflow, so a condition written in the workflow is no
control against a write-scoped token; where the plan offers no environments
(GitHub Free, private repo) the secret does not belong in CI at all.
**Check.** `grep -nE "secrets\.[A-Z_]*(KEY|TOKEN|PASS|SECRET)" .github/workflows/*.yml`;
for every hit the job must declare `environment:` bound to the protected
branch with a required reviewer, and must contain no `uses:` outside
`actions/*`; a hit whose job has neither is a finding. On a plan without
environments, any signing or registry-write secret in CI is a finding.
**Fix form.** Environment-scoped secret on the protected branch with a
required reviewer; signing in a separate job with no third-party actions; or
move signing off CI to a human-gated signer that builds from the commit.
**Since.** 2026-10-05.
**Origin.** audit-openos-2026-10-05 (HIGH; approved by Ron 2026-10-05).

### SC-30 · An artifact verified by one reference and consumed by another
**Shape.** A signature or checksum is verified against a mutable reference (a
tag), and the artifact is then consumed, copied, printed, or baked by that
same mutable reference — or by a reference never verified at all — so a
registry writer can swap the digest between check and use. Includes signing
or trusting an artifact whose only provenance is metadata (labels) the
artifact's writer controls.
**Check.** For every verify step (`--signature-policy`, `cosign verify`,
`sha256sum -c`), the next consumer must reference the verified DIGEST
(`@sha256:`); `grep -nE "(skopeo copy|podman (pull|run|push)|bootc|image-builder).*:[^@]*\$\{?[A-Z_]*(TAG|VERSION|STAGE)"`
over build and deploy scripts — every hit is a finding unless the line uses a
digest captured from the verify step. A privileged consumer of a signed
artifact with no verify step at all is a finding.
**Fix form.** Capture the digest at verification; every later use pins
`@digest`; provenance comes from building the artifact, not from its labels.
**Since.** 2026-10-05.
**Origin.** audit-openos-2026-10-05 (HIGH; approved by Ron 2026-10-05).

### SC-31 · A signature policy that binds the repository, with signed
non-release artifacts in it
**Shape.** The verifying policy accepts any signed digest of the repository
under any tag (`matchRepository`, or a cosign identity with no tag or version
constraint) while the publisher signs non-release artifacts (staging, branch,
CI previews) into the same repository, so every such artifact is a valid
downgrade target for whoever can move a tag; and the consumer has no version
floor.
**Check.** `grep -rn "matchRepository" system_files/` (or the policy source);
if present, the publish path must sign only release versions of protected
builds (`grep -n "sign-by\|cosign sign"`), no staging or preview push may
target the trusted repository, and the update path must refuse a version not
strictly newer than the running one (a test asserts the refusal).
**Fix form.** Sign only releases; separate repository or identity for
anything else; a monotonic version floor in the updater.
**Since.** 2026-10-05.
**Origin.** audit-openos-2026-10-05 (MEDIUM; approved by Ron 2026-10-05).

### SC-32 · A privilege policy written as a deny-list of dangerous values
**Shape.** A capability, syscall, device, or mount policy refuses a
hand-picked set of "host-escape" values and admits everything else, so an
omitted member passes (DAC_READ_SEARCH -> `open_by_handle_at`, the "shocker"
container escape); and a drop-side field (`cap_drop`) is type-checked but not
required, so omitting it silently restores the runtime's default set. The
refusal message ("host-escape capability refused") asserts a completeness the
list lacks (SC-13).
**Check.** `grep -nE "in \((\"[A-Z_]+\", ?)+\"[A-Z_]+\"\)"` over policy code
that handles `cap_add`, `capabilities`, `devices`, `sysctls` or `security_opt`;
every hit on a privilege dimension is a finding unless it is an allow-list.
`grep -n "cap_drop"`: the policy must require `cap_drop` to contain `ALL`, and
the post-deploy audit must read `CapDrop` and the effective set, not only
`CapAdd`. A test feeds every name in `capabilities(7)` that is not on the
allow-list and asserts refusal, plus a document with `cap_drop` omitted.
**Fix form.** Allow-list of known-safe values, each justified; require
drop-ALL; audit the effective set at runtime.
**Since.** 2026-10-08.
**Origin.** audit-infra-2026-10-08 (HIGH; approved by Ron 2026-10-08).

### SC-33 · An allow-list enforced only when the governed key is present
**Shape.** A policy validates a key's value against an allow-list only inside
`if key in doc` / `elif attr == ...`, while the consuming system gives the
ABSENT key a broader default: a Traefik router with no `entrypoints` binds
every entrypoint, one with no `rule` gets the default Host rule. Omission is
the bypass, and the tests only ever feed present values.
**Check.** For every allow-list predicate in policy code, find the consumer's
default for the missing key in its documentation or source; if that default
is broader than the allow-list, the policy must REQUIRE the key. One test per
predicate feeds the document with the key omitted and asserts refusal.
**Fix form.** Require the key wherever the consumer's default is wider than
the allow-list; never validate only what is present.
**Since.** 2026-10-08.
**Origin.** audit-infra-2026-10-08 (MEDIUM; approved by Ron 2026-10-08).

### SC-34 · Untrusted content written to GITHUB_ENV or GITHUB_OUTPUT with a
fixed heredoc delimiter
**Shape.** A workflow step appends `NAME<<EOF` ... `EOF` to `$GITHUB_ENV` or
`$GITHUB_OUTPUT` with content fetched from outside the step (a gist, an API
body, a PR title, a file the job did not write). A newline plus the fixed
delimiter in that content ends the block early and sets arbitrary environment
variables or outputs for every later step (`GH_REPO`, `BASH_ENV`, ...).
**Check.** `grep -nE "<<-?['\"]?[A-Za-z_]*EOF" .github/workflows/*.yml` near
`GITHUB_ENV|GITHUB_OUTPUT`; every hit whose body interpolates a value not
computed in-step from trusted input is a finding unless the delimiter is
random per run (`delim="ghadelim_$(openssl rand -hex 16)"` or equivalent).
**Fix form.** Random per-run delimiter, or validate the content down to a
fixed shape (a timestamp, a SHA) before writing it.
**Since.** 2026-10-08.
**Origin.** audit-infra-2026-10-08 (LOW; approved by Ron 2026-10-08).

### SC-35 · Removal by unlinking one edge while persistence is
reachability-based (replace-by-add)
**Shape.** A "replace", "redact" or "remove" operation registers a NEW object
(or deletes one inbound reference) and rewrites the consumer it was told
about, but leaves the original bound through another name, index, resource
dictionary, parent pointer, structure tree, calculation order or reply chain.
Persistence is decided by a reachability walk (garbage collection,
`removeUnreachableObjects`, `collectGarbage`), so the original is still
reachable and is written out, while every verifier looks only at what is DRAWN
or LISTED, not at what is PRESENT. pdfmanager 2026-10-08: redacted image/form
XObjects kept under the original resource name (CRITICAL); removed annotations
and widgets resurrected via `/OBJR`, `/IRT`, `/CO`, `/Parent`, XFA (HIGH);
deleted pages written because pdf-lib writes every indirect object (HIGH).
**Check.** Grep the add-only registration form (`addXObject(`, `.clone(`,
`set(PDFName.of(...` on a resource dict, `push(` on a kids/fields array) and
the single-edge removal form (`filter(` on one array, `delete` on one key) in
any code that claims to remove content; for each, name every other inbound
edge the object type can have in the format's object model. The diff must
carry an OUTPUT-BOUNDARY test: serialise, re-parse, and assert the original
object's bytes (or ref) are ABSENT from the saved file, not merely that the
drawn result is correct.
**Fix form.** Remove by object identity across every inbound reference, or
rebuild the index from the names the rewritten consumer actually uses; make
the verifier fail on any reachable-but-unreferenced object of the redacted
kind.
**Since.** 2026-10-09.
**Origin.** audit-pdfmanager-2026-10-08, proposed there as SC-32
(CRITICAL; approved by Ron 2026-10-09).

### SC-36 · Index-space mismatch between where a position is computed and
where it is applied
**Shape.** Positions, offsets or lengths are computed in one unit (Unicode
code points via `for..of`, graphemes, pre-case-fold characters, bytes) and
consumed in another (`String.length`, `.slice`, `.substring`, `indexOf` on
UTF-16 units, or on a case-folded string whose length changed). Every
occurrence after the first astral character or length-changing fold is
shifted. When the consumer is a redaction, a highlight, a patch or an access
check, the shift lands the action on the wrong target and a fallback that
re-rasterises or re-copies the original preserves the content the action was
meant to remove. pdfmanager 2026-10-08 `textSearch.ts:61-78` (HIGH).
**Check.** Grep `for (const .* of ` over a string paired with `.length`,
`.slice(`, `.substring(` or `indexOf(` on the same or a derived string; grep
`.toLowerCase()`/`.toUpperCase()`/`.normalize(` whose result is indexed with
offsets from the un-folded string. The diff must carry a test using an astral
character (`𝐀`, an emoji) and a length-changing fold (`İ`) BEFORE the target
occurrence.
**Fix form.** One index space end to end (build the map per UTF-16 unit on the
already-folded string), and verify the OUTPUT (the term must be absent from
the rendered or re-extracted result), never only the input positions.
**Since.** 2026-10-09.
**Origin.** audit-pdfmanager-2026-10-08, proposed there as SC-33
(HIGH; approved by Ron 2026-10-09).

### SC-37 · A pull_request-reachable job whose runner is chosen by a
repository variable
**Shape.** `runs-on: ${{ vars.X || 'ubuntu-latest' }}` (or any expression over
`vars.`/`inputs.`) on a job reachable from `pull_request` in a public
repository. The default is hosted, so the shape is inert until the variable is
set; then every fork PR's install-time code runs on whatever the variable
names. pdfmanager 2026-10-08: 7 sites across 3 workflows (MEDIUM, latent).
**Check.** `grep -n "runs-on:.*\${{" .github/workflows/*.yml` intersected with
workflows that declare `pull_request:` or `pull_request_target:`; for each
hit, the repo visibility and the fork-approval policy are part of the finding.
Any hit in a public repo is a finding regardless of the variable's current
value.
**Fix form.** Literal GitHub-hosted labels on every PR-reachable job;
variables may select runners only for `schedule`, `workflow_dispatch` and
`push` on protected branches.
**Since.** 2026-10-09.
**Origin.** audit-pdfmanager-2026-10-08, proposed there as SC-34
(MEDIUM, latent; approved by Ron 2026-10-09).

### SC-38 · A guard that passes vacuously for inputs below its own tolerance
**Shape.** A geometric, range or threshold guard applies an inset, epsilon,
rounding or minimum to its input and, for inputs smaller than that tolerance,
produces an empty, inverted or degenerate test region. Every subsequent
membership test is then false, and "nothing found" reads as "clean". The
acceptance filter upstream admits inputs smaller than the guard's tolerance.
pdfmanager 2026-10-08 `redactionVerifier.ts:66` (0.5 pt inset; marks over 0.01
pt accepted) (LOW); the zero-size-text finding has the same degenerate shape
at `contentRedactor.ts:302`.
**Check.** For every guard that subtracts, insets or divides by a tolerance,
grep the upstream acceptance filter's minimum and compare; the diff must carry
a test at an input strictly between the acceptance minimum and the guard
tolerance, asserting the guard REJECTS or reports NOT EXAMINED rather than
passing.
**Fix form.** Clamp the tolerance to a fraction of the input, or reject inputs
below the tolerance at the acceptance filter.
**Since.** 2026-10-09.
**Origin.** audit-pdfmanager-2026-10-08, proposed there as SC-35
(LOW; approved by Ron 2026-10-09).

### SC-39 · Code fetched over an unauthenticated channel and executed
**Shape.** A script or binary is downloaded over HTTP (or over HTTPS with
verification disabled) and run without a pinned hash or signature check:
`iex (irm http://...)`, `DownloadString('http://...')`,
`[scriptblock]::Create($downloaded)`, `Invoke-Expression` of a web response.
This is worst when the run is elevated, and when an elevated child re-downloads
instead of running the bytes already obtained. patriot-provisioning 2026-10-10:
`setup-bootstrap.ps1` and `triage-workstation.ps1` (HIGH).
**Check.** `grep -rnE "(iex|Invoke-Expression|ScriptBlock\]::Create|DownloadString)\b.*"
--include=*.ps1` intersected with `http://` or a variable assigned from
`Invoke-RestMethod` / `Invoke-WebRequest`. Every hit must verify a pinned
SHA-256 or an Authenticode signer of the exact bytes before execution. Docs
that tell an operator to paste such a one-liner count as siblings.
**Fix form.** HTTPS with a pinned certificate, plus a pinned hash or signature
of the executed bytes; elevated children run the verified bytes and never
re-fetch.
**Since.** 2026-10-10.
**Origin.** audit-patriot-provisioning-2026-10-10 (HIGH; approved by Ron 2026-10-10).

### SC-40 · Secret material embedded in generated script text
**Shape.** A secret (decrypted password, token, key) is interpolated into the
source text of a script that is then written to disk or compiled
(`Set-Content *.ps1`, `[scriptblock]::Create`, `-replace '__PLACEHOLDER__'`).
The file is a readable copy. On Windows PowerShell 5.1, a script block that also
contains `Add-Type`, `DllImport` or other "suspicious" terms is auto-logged as
event 4104 even with logging disabled, so the secret lands in the event log.
patriot-provisioning 2026-10-10: `chrome-cred-migrate.ps1` Restore (HIGH).
**Check.** `grep -nE "(-replace|\.Replace\(|-f ).*(__[A-Z0-9_]+__|\{[0-9]\})"
--include=*.ps1`, then follow each result into `Set-Content`, `Out-File`,
`ScriptBlock]::Create` or an Invoke-AsUser-style writer. Any hit whose inserted
value derives from a secret is a finding. A test asserts the generated script
text contains no secret value.
**Fix form.** Pass secrets over an in-memory channel (WinRM argument, stdin,
named pipe) or as an encrypted blob decrypted in-process; never in script
source.
**Since.** 2026-10-10.
**Origin.** audit-patriot-provisioning-2026-10-10 (HIGH; approved by Ron 2026-10-10).

### SC-41 · Privileged file or ACL operation on a path a lower-privileged principal can create or replace
**Shape.** Admin or SYSTEM code writes, deletes, re-ACLs or takes ownership of a
path inside a folder a standard user can create, own or modify: ProgramData
subfolders created with `New-Item -Force`, user-profile subfolders, `/tmp` with
a fixed name, or paths replayed from a collected baseline. There is no
reparse-point check, so junctions and symlinks redirect the privileged
operation. patriot-provisioning 2026-10-10: `parity-restore-acl.ps1` (HIGH),
`lib.ps1` PatriotPrep and `/tmp/.cc_job.sh`, `chrome-reimport.ps1` Stage (MEDIUM).
**Check.**
- `grep -nE "icacls .*(/setowner|/grant)"` without `/L`;
- `grep -nE "New-Item .*(ProgramData|Users\\\\).*-Force"` in privileged code;
- `grep -nE "/tmp/[A-Za-z._-]+\b"` with a fixed name;
- `grep -nE "Remove-Item .*-Recurse|WriteAllText|Set-Acl"` in code run as admin over WinRM.

Each hit must refuse a pre-existing or reparse-point target, or operate by
handle with `FILE_FLAG_OPEN_REPARSE_POINT`. A test plants a junction and asserts
refusal.
**Fix form.** Per-run, randomly named folders created with an explicit owner and
DACL; refuse if the path exists; `/L` or handle-based operations;
`mktemp` / `sudo sh -s`.
**Since.** 2026-10-10.
**Origin.** audit-patriot-provisioning-2026-10-10 (HIGH; approved by Ron 2026-10-10).

### SC-42 · Credential authentication to a peer that is never authenticated
**Shape.** A credential is presented to an endpoint whose identity is not
verified: Negotiate/NTLM to a bare IP literal (no Kerberos, no server
authentication), `-SkipCertificateCheck`, `-AcceptKey` / paramiko
`AutoAddPolicy`, `verify=False`, or an http URL that carries a password.
"Pinning" the address is treated as pinning the host. patriot-provisioning
2026-10-10: WinRM to `192.168.0.180` with the fleet-shared PatriotUSA password,
and DSM/SSH to the NAS (HIGH).
**Check.** `grep -rnE "SkipCertificateCheck|AcceptKey|AutoAddPolicy|verify\s*=\s*False|-ComputerName\s+['\"]?[0-9]+(\.[0-9]+){3}"`.
Every hit that carries a credential must pin a certificate fingerprint or host
key and fail closed on a mismatch. Where a strong form exists elsewhere in the
repo, the weak form is also SC-16.
**Fix form.** Pinned certificate or host key (WinRM HTTPS, known_hosts with a
reject policy); per-device credentials so that one capture does not open the
fleet.
**Since.** 2026-10-10.
**Origin.** audit-patriot-provisioning-2026-10-10 (HIGH; approved by Ron 2026-10-10).

---

## Proposed additions (from the routine audit; operator approval required)

_None pending._ Audits append proposals here in the format above with a
`**Proposed by.** audit-<repo>-<date>` line; the operator moves an approved
proposal into the register and bumps its `Since` date. Graduated so far:
SC-22..SC-24 (audit-cortex-hooks-2026-09-15, approved 2026-09-16), SC-29..SC-31
(audit-openos-2026-10-05) and SC-25 (gate-openos-2026-09-16), approved
2026-10-05. SC-32..SC-34 (audit-infra-2026-10-08), approved 2026-10-08.
SC-35..SC-38 (audit-pdfmanager-2026-10-08, proposed there as SC-32..SC-35
and renumbered because those numbers were already graduated), approved
2026-10-09. SC-39..SC-42 (audit-patriot-provisioning-2026-10-10), approved
2026-10-10. SC-26..SC-28 were numbered by audit-openos-2026-09-16 but never
written as proposals; the numbers stay reserved.
