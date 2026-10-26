"""Suite-wide guard: no test may open a real OBDX Pro GT.

The Dashboard looks for the GT's USB ID when it is built and every 2 s after
(auto-connect, v0.34.0). On a workstation with the GT plugged in, every test
that builds a Dashboard would otherwise start a real serial open. The default
ports provider (openobd.app.detect_gt_ports) reports no GT here; tests that
exercise auto-connect pass their own provider to Dashboard().
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


@pytest.fixture(autouse=True)
def _no_real_gt(monkeypatch):
    monkeypatch.setattr("openobd.app.detect_gt_ports", lambda: [])
