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

---

## Proposed additions (from the routine audit; operator approval required)

_None pending._ Audits append proposals here in the format above with a
`**Proposed by.** audit-<repo>-<date>` line; the operator moves an approved
proposal into the register and bumps its `Since` date.
