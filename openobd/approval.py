"""
approval.py -- a phone approve/deny gate for hardware actions.

WHY (Ron, 2026-10-03). The closed truck loop should let the agent run ECM READS
without a terminal hand-off: the agent asks, a phone prompt offers approve/deny,
and on approve the read proceeds. Writes stay hard-gated. This module is the
MECHANISM. Wiring the hazard-guard (DR-011) to consult it is a separate,
operator-owned change -- this file never touches the guard.

THE SECURITY HINGE. The agent is the party being gated, so it must not be able
to approve its own action. Therefore:

  * The agent only REQUESTS and VERIFIES. It never writes a decision.
  * A decision is written by a TRUSTED approver path (the phone tap -> a
    callback endpoint the agent does not control), to a store the agent cannot
    write, and is SIGNED with a key the agent does not hold. The agent reads the
    decision and checks the signature; a forged or self-written decision fails
    verification.
  * An approval is bound to ONE request (a random nonce the agent did not
    choose alone -- see request_id), is FRESH (short TTL), and is SINGLE-USE
    (consumed on acceptance), so a stale or replayed approval cannot authorise a
    later action. Same shape as the cortex gate-reviewer-record binding.

This module ships the pure core (request/decision model, signing, verification,
freshness, single-use) plus a transport seam (Notifier) and a store seam
(DecisionSource). The real ntfy-button transport and the trusted callback
endpoint that signs decisions are a separate increment; a key the agent cannot
read is the precondition for the whole thing and is placed by the operator.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional, Protocol


class Risk(str, Enum):
    """What an action can do. Only READ (and the read-only post-read DTC CLEAR)
    is ever eligible for the phone gate; WRITE is hard-gated elsewhere and is
    here only so a request can declare itself and be refused."""
    READ = "read"
    CLEAR = "clear"
    WRITE = "write"


GATE_ELIGIBLE = frozenset({Risk.READ, Risk.CLEAR})


class ApprovalError(Exception):
    pass


@dataclass(frozen=True)
class ApprovalRequest:
    """One action awaiting a human decision. `request_id` is a random nonce the
    gate mints per request; a decision is valid only for the exact id."""
    action: str                 # e.g. "ecm.read"
    detail: str                 # human-readable, shown on the phone
    risk: Risk
    requested_by: str           # session id, for the audit line
    created_at: float
    ttl_s: float
    request_id: str

    def signing_bytes(self, decision: str, decided_at: float) -> bytes:
        """The exact bytes a decision is signed over. Canonical and stable:
        changing any field invalidates the signature."""
        payload = {
            "request_id": self.request_id,
            "action": self.action,
            "risk": self.risk.value,
            "decision": decision,
            "decided_at": round(decided_at, 3),
        }
        return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()

    def is_expired(self, now: Optional[float] = None) -> bool:
        return (now or time.time()) > self.created_at + self.ttl_s


@dataclass(frozen=True)
class ApprovalDecision:
    request_id: str
    action: str
    risk: str
    decision: str               # "approve" | "deny"
    decided_at: float
    approver: str               # who tapped (from the trusted endpoint)
    signature: str              # hex HMAC over request.signing_bytes(...)


def sign_decision(key: bytes, request: ApprovalRequest, decision: str,
                  decided_at: float) -> str:
    """Produce the signature a trusted approver endpoint attaches to a decision.
    Lives here so the endpoint and the verifier share ONE definition. The AGENT
    never calls this in production -- it holds no key."""
    return hmac.new(key, request.signing_bytes(decision, decided_at),
                    hashlib.sha256).hexdigest()


def verify_decision(key: bytes, request: ApprovalRequest,
                    decision: ApprovalDecision) -> bool:
    """True iff `decision` is a correctly-signed, matching decision for
    `request`. Constant-time compare. Does NOT check freshness/single-use --
    ApprovalGate does, so verification stays a pure signature check."""
    if decision.request_id != request.request_id:
        return False
    if decision.action != request.action or decision.risk != request.risk.value:
        return False
    if decision.decision not in ("approve", "deny"):
        return False
    expected = sign_decision(key, request, decision.decision, decision.decided_at)
    return hmac.compare_digest(expected, decision.signature)


class Notifier(Protocol):
    """Sends the approve/deny prompt to the human. The real one pushes an ntfy
    message with Approve/Deny action buttons; a test one records the call."""
    def send(self, request: ApprovalRequest) -> None: ...


class DecisionSource(Protocol):
    """Where signed decisions arrive. The real one reads the store the trusted
    callback endpoint appends to (a path the agent cannot write); a test one is
    in-memory. Returns the decision for a request_id, or None if not yet there."""
    def get(self, request_id: str) -> Optional[ApprovalDecision]: ...


class ApprovalGate:
    """Ask, wait, verify. The agent-side entry point.

    key: the shared HMAC key used to VERIFY decisions. In production the agent
    gets only what it needs to verify; the ability to SIGN lives at the trusted
    endpoint. (They are the same symmetric key here; a later increment can split
    to asymmetric so the agent holds only a public key. The seam is
    sign_decision/verify_decision.)"""

    def __init__(self, notifier: Notifier, source: DecisionSource, key: bytes,
                 session_id: str, default_ttl_s: float = 180.0):
        if not key:
            raise ApprovalError("no approval key: the gate cannot verify "
                                "decisions, so it refuses to run open")
        self._notifier = notifier
        self._source = source
        self._key = key
        self._session = session_id
        self._ttl = default_ttl_s
        self._consumed: set[str] = set()

    def request(self, action: str, detail: str, risk: Risk,
                ttl_s: Optional[float] = None) -> ApprovalRequest:
        if risk not in GATE_ELIGIBLE:
            raise ApprovalError(
                f"{risk.value} is not gate-eligible: only reads go through the "
                f"phone gate; writes are hard-gated elsewhere")
        req = ApprovalRequest(
            action=action, detail=detail, risk=risk,
            requested_by=self._session, created_at=time.time(),
            ttl_s=ttl_s if ttl_s is not None else self._ttl,
            request_id=secrets.token_hex(16),
        )
        self._notifier.send(req)
        return req

    def check(self, request: ApprovalRequest,
              now: Optional[float] = None) -> Optional[ApprovalDecision]:
        """One non-blocking look. Returns a VERIFIED, fresh, unconsumed decision,
        or None if none has arrived yet. Raises on expiry, a denial, a bad
        signature, or a replay -- the caller must not proceed in those cases."""
        now = now or time.time()
        if request.is_expired(now):
            raise ApprovalError(f"approval window expired for {request.action}")
        d = self._source.get(request.request_id)
        if d is None:
            return None
        if not verify_decision(self._key, request, d):
            raise ApprovalError("approval decision failed signature "
                                "verification -- forged or corrupt")
        if request.request_id in self._consumed:
            raise ApprovalError("approval already consumed (replay)")
        # the decision itself must not predate the request or arrive after expiry
        if d.decided_at < request.created_at - 1.0 or d.decided_at > request.created_at + request.ttl_s + 1.0:
            raise ApprovalError("approval timestamp outside the request window")
        if d.decision == "deny":
            self._consumed.add(request.request_id)
            raise ApprovalError(f"action DENIED by {d.approver}")
        self._consumed.add(request.request_id)
        return d

    def await_decision(self, request: ApprovalRequest, poll_s: float = 1.0,
                       _clock=time.time, _sleep=time.sleep) -> ApprovalDecision:
        """Block until approved, denied, or expired. Returns the verified
        approval or raises (deny/expiry/forgery)."""
        while True:
            got = self.check(request, now=_clock())
            if got is not None:
                return got
            # stop sleeping past the window
            remaining = request.created_at + request.ttl_s - _clock()
            if remaining <= 0:
                raise ApprovalError(
                    f"approval window expired for {request.action}")
            _sleep(min(poll_s, remaining))


# --------------------------------------------------------------------------- #
# Reference seams for the next increment (not the production transport).
# --------------------------------------------------------------------------- #
class RecordingNotifier:
    """Test/dev notifier: records every prompt instead of sending it."""
    def __init__(self):
        self.sent: list[ApprovalRequest] = []

    def send(self, request: ApprovalRequest) -> None:
        self.sent.append(request)


class JsonlDecisionSource:
    """Reads signed decisions from a JSONL file the trusted callback endpoint
    appends to. The agent opens it READ-ONLY; it must live on a path the agent
    cannot write (the hazard-guard floor), exactly like the cortex gate records.
    A malformed line is skipped, never trusted."""
    def __init__(self, path: str):
        self._path = path

    def get(self, request_id: str) -> Optional[ApprovalDecision]:
        if not os.path.exists(self._path):
            return None
        found: Optional[ApprovalDecision] = None
        with open(self._path, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    o = json.loads(line)
                    if o.get("request_id") != request_id:
                        continue
                    found = ApprovalDecision(
                        request_id=o["request_id"], action=o["action"],
                        risk=o["risk"], decision=o["decision"],
                        decided_at=float(o["decided_at"]),
                        approver=o.get("approver", "?"),
                        signature=o["signature"],
                    )
                except (ValueError, KeyError):
                    continue
        return found
