# UAC-style approval popup for elevated-risk actions

`tools/approve-dialog.ps1` is a desktop popup — topmost, like UAC — that shows a
pending elevated-risk agent action and offers **Allow / Reject**. It is the UI
half of Ron's 2026-10-03 decision: an elevated action must not hard-stop; it
raises an approve/deny prompt instead.

## Security model (why the guard raises it, not the agent)

UAC is trustworthy because the OS raises the prompt, so the app can't skip it.
Same here: the popup must be raised by the **hazard-guard** (the trusted
PreToolUse layer the agent cannot bypass), not by the agent. The guard runs this
script **synchronously** and gates on its **exit code**:

| exit | meaning | guard action |
|---|---|---|
| 0 | ALLOW (Ron clicked Allow) | permit the action |
| 2 | REJECT (Ron clicked Reject) | block |
| 3 | TIMEOUT (no click in `-TimeoutSeconds`) | block |
| 4 | CLOSED (window closed, no choice) | block |
| other | error | block |

The default is always deny: the script traps every error and exits `2`, and the
guard treats any non-zero exit as deny. No signing key is needed on this path
because the trusted guard both raises the prompt and reads the result from its
own child process — the agent never touches the decision.

## Invocation (what the guard runs)

```powershell
powershell.exe -NoProfile -STA -ExecutionPolicy Bypass `
  -File <repo>\tools\approve-dialog.ps1 `
  -Action "ecm.read" `
  -Detail "Full ECM read (2 MiB) -> images/truck-read-stock-20261004.bin" `
  -Risk read -TimeoutSeconds 120 -RequestId <nonce>
```

`-STA` is required (WPF). `-Risk` is `read` | `clear` | `write` and only tints
the accent — the guard decides eligibility (reads/clears gate here; **writes stay
hard-gated and must not be routed through this popup**). `-DecisionFile <path>`
optionally appends a JSON decision record for an async/audit trail; the exit code
is the authoritative signal.

## What is built vs. what is the operator's

- **Built (OpenOBD):** this dialog, `-SelfTest` (no-UI validation), and
  `openobd/approval.py` (the async/verifiable variant: signed, single-use,
  TTL-bound decisions for a phone/ntfy path, where the agent only verifies).
- **Operator's (DR-011):** wiring the hazard-guard to, on an elevated-risk READ,
  run this dialog and allow on exit 0 instead of hard-blocking. That edit is
  Ron's — drafting a DR-011 change was classifier-stopped twice. This doc is the
  contract that edit implements; the guard change itself is not made here.

## Async variant

For approvals that should reach a phone (not just the desktop), `approval.py`'s
`ApprovalGate` sends a prompt through a `Notifier` (e.g. an ntfy message with
Allow/Reject action buttons) and accepts only a decision **signed** by a trusted
callback endpoint — the agent holds no signing key, and each approval is bound to
one request, fresh, and single-use. The desktop popup above is the synchronous,
guard-raised path; the signed gate is the asynchronous, verify-only path. Both
keep the gated agent from approving itself.
