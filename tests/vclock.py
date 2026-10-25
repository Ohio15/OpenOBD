"""A virtual monotonic clock for tests: time moves only when the test says.

Shared by tests/test_sample_timing.py (patched over time.monotonic for the
simulated ELM327) and tests/smoke_gui.py (injected into GtDataSource), so
reading ages and freshness are evaluated on test time, never on how fast the
machine happens to run.
"""


class Clock:
    def __init__(self, start=100.0):
        self.now = start

    def __call__(self):
        return self.now
