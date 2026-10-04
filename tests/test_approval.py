"""Phone approve/deny gate (openobd.approval).

The agent is the party being gated, so the central property under test is that
it CANNOT approve its own action: only a decision signed with the key (held by
the trusted approver endpoint, not the agent) is accepted, and each approval is
bound to one request, fresh, and single-use. No hardware, no network.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from openobd.approval import (  # noqa: E402
    ApprovalGate, ApprovalError, Risk, RecordingNotifier, sign_decision,
    verify_decision, ApprovalDecision,
)

KEY = b"test-approver-key-32-bytes-long!!"


class MemSource:
    """In-memory decision source the TRUSTED side writes to (signs with KEY)."""
    def __init__(self):
        self._d = {}

    def approve(self, request, approver="ron", at=None, decision="approve",
                key=KEY, sig=None):
        import time
        at = time.time() if at is None else at
        signature = sig if sig is not None else sign_decision(key, request, decision, at)
        self._d[request.request_id] = ApprovalDecision(
            request_id=request.request_id, action=request.action,
            risk=request.risk.value, decision=decision, decided_at=at,
            approver=approver, signature=signature)

    def get(self, request_id):
        return self._d.get(request_id)


def gate(source=None, ttl=180.0):
    return ApprovalGate(RecordingNotifier(), source or MemSource(), KEY,
                        "sess-1", default_ttl_s=ttl)


# --------------------------------------------------------------------------- #
def test_request_sends_a_prompt_and_mints_a_nonce():
    n = RecordingNotifier()
    g = ApprovalGate(n, MemSource(), KEY, "sess-1")
    req = g.request("ecm.read", "Full ECM read, 2 MiB", Risk.READ)
    assert len(n.sent) == 1 and n.sent[0] is req
    assert len(req.request_id) == 32            # 16 random bytes hex
    assert g.check(req) is None                 # nothing decided yet


def test_approved_decision_is_accepted():
    src = MemSource()
    g = gate(src)
    req = g.request("ecm.read", "Full ECM read", Risk.READ)
    src.approve(req, approver="ron")
    d = g.check(req)
    assert d is not None and d.decision == "approve" and d.approver == "ron"


def test_denied_decision_raises():
    src = MemSource()
    g = gate(src)
    req = g.request("ecm.read", "Full ECM read", Risk.READ)
    src.approve(req, decision="deny", approver="ron")
    with pytest.raises(ApprovalError, match="DENIED"):
        g.check(req)


def test_forged_decision_rejected_wrong_key():
    # the AGENT (or anyone without the key) signs with the wrong key -> rejected
    src = MemSource()
    g = gate(src)
    req = g.request("ecm.read", "Full ECM read", Risk.READ)
    src.approve(req, key=b"attacker-key-not-the-real-one!!!")
    with pytest.raises(ApprovalError, match="signature"):
        g.check(req)


def test_tampered_decision_rejected():
    # a correctly-signed approve, then the signature is kept but a field flipped
    src = MemSource()
    g = gate(src)
    req = g.request("ecm.read", "Full ECM read", Risk.READ)
    src.approve(req)
    good = src.get(req.request_id)
    src._d[req.request_id] = ApprovalDecision(
        request_id=good.request_id, action=good.action, risk=good.risk,
        decision="approve", decided_at=good.decided_at + 5.0,  # changed
        approver=good.approver, signature=good.signature)       # old sig
    with pytest.raises(ApprovalError, match="signature"):
        g.check(req)


def test_decision_for_a_different_request_is_not_accepted():
    src = MemSource()
    g = gate(src)
    req1 = g.request("ecm.read", "read", Risk.READ)
    req2 = g.request("ecm.read", "read", Risk.READ)
    src.approve(req1)                            # only req1 approved
    assert g.check(req2) is None                 # req2 has no decision
    assert g.check(req1).decision == "approve"


def test_approval_is_single_use():
    src = MemSource()
    g = gate(src)
    req = g.request("ecm.read", "read", Risk.READ)
    src.approve(req)
    assert g.check(req).decision == "approve"    # first use ok
    with pytest.raises(ApprovalError, match="replay"):
        g.check(req)                             # second use refused


def test_expired_window_raises():
    import time
    src = MemSource()
    g = gate(src, ttl=0.0)
    req = g.request("ecm.read", "read", Risk.READ)
    time.sleep(0.01)
    with pytest.raises(ApprovalError, match="expired"):
        g.check(req)


def test_stale_approval_timestamp_rejected():
    # a decision whose timestamp predates the request window (a replayed old
    # approval reused for a new request id would also fail the id check; this
    # guards the timestamp bound directly).
    src = MemSource()
    g = gate(src)
    req = g.request("ecm.read", "read", Risk.READ)
    src.approve(req, at=req.created_at - 100.0)   # decided before the request
    with pytest.raises(ApprovalError, match="window"):
        g.check(req)


def test_writes_are_never_gate_eligible():
    g = gate()
    with pytest.raises(ApprovalError, match="hard-gated"):
        g.request("ecm.write", "flash cal", Risk.WRITE)


def test_gate_refuses_to_run_without_a_key():
    with pytest.raises(ApprovalError, match="no approval key"):
        ApprovalGate(RecordingNotifier(), MemSource(), b"", "sess-1")


def test_await_decision_returns_on_approve(monkeypatch):
    src = MemSource()
    g = gate(src)
    req = g.request("ecm.read", "read", Risk.READ)
    # simulate the approval arriving on the second poll
    t = [req.created_at]
    def clock():
        return t[0]
    def sleep(_):
        t[0] += 0.5
        src.approve(req, at=t[0])
    d = g.await_decision(req, poll_s=0.5, _clock=clock, _sleep=sleep)
    assert d.decision == "approve"


def test_verify_decision_is_pure():
    src = MemSource()
    g = gate(src)
    req = g.request("ecm.read", "read", Risk.READ)
    import time
    at = time.time()
    sig = sign_decision(KEY, req, "approve", at)
    d = ApprovalDecision(req.request_id, req.action, req.risk.value,
                         "approve", at, "ron", sig)
    assert verify_decision(KEY, req, d) is True
    assert verify_decision(b"wrong-key-wrong-key-wrong-key!!!", req, d) is False
