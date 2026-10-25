"""
pollplan -- which Mode 01 PIDs the GT asks for on each poll cycle, and how.

Sample rate is the ceiling on every measurement OpenOBD takes from the GT. The
2026-10-25 drive (gt_log_20261025_141431.csv) asked for 22 PIDs one request at
a time, ~63 ms each: 0.69 Hz, too slow to see a shift or a WOT pull. Two things
move that number, and this module owns both (ported from truck-mcp
src/truck_mcp/sweep.py SweepPlan, which reached 6.6-7.2 Hz on the same GT):

  batching  One Mode 01 request carries up to MAX_PIDS_PER_REQUEST PIDs
            ("01" + the PID bytes). The request must fit one CAN single
            frame: PCI + service byte + N PID bytes <= 8, so N <= 6 (truck-mcp
            obd.py MAX_PIDS_PER_REQUEST). The ECM answers with ONE (usually
            multi-frame) ISO-TP response, so six values cost one round trip.
            Needs physical addressing (ATSH 7E0) so only the ECM answers.
  tiers     Coolant temperature does not change between two 250 ms cycles;
            RPM changes by thousands. FAST channels are asked for on every
            cycle, MEDIUM every 4th, SLOW every 20th, so the fast ones get
            the link.

Tiering means a cycle no longer reads every channel. The contract that keeps
that honest lives in the transport and the recorder: a cycle's Sample holds
ONLY the readings obtained in that cycle, so a not-due channel is a blank cell
in the recording, never a carried-forward value.

Pure: no serial, no Qt. The cost model is the measured truck-mcp figure for
this GT (cassette no-accel-1000rpm, 2026-09-18), used only to PREDICT a cycle
time for display; nothing here waits on it.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, Mapping, Optional

#: Mode 01 PIDs per request: one CAN single frame (truck-mcp obd.py).
MAX_PIDS_PER_REQUEST = 6

#: Tier name -> refresh divisor (asked for on cycles where cycle % div == 0).
FAST, MEDIUM, SLOW = "fast", "medium", "slow"
TIER_DIVISORS = {FAST: 1, MEDIUM: 4, SLOW: 20}
TIER_ORDER = (FAST, MEDIUM, SLOW)

#: Canonical key -> tier. Every key gt.PID_TABLE can produce must appear here
#: (tests/test_pollplan.py checks it), so a new PID cannot silently default.
KEY_TIERS: dict[str, str] = {
    # FAST: what shift analysis and WOT pulls are made of.
    "rpm": FAST, "vss": FAST, "map": FAST, "maf": FAST, "tps": FAST,
    "app": FAST, "app_e": FAST, "app_f": FAST, "spark": FAST, "load": FAST,
    "abs_load": FAST, "stft": FAST, "stft_b2": FAST, "rel_tps": FAST,
    "cmd_throttle": FAST, "eq_ratio": FAST, "fuel_press": FAST,
    # MEDIUM: slower closed-loop and thermal signals.
    "ltft": MEDIUM, "ltft_b2": MEDIUM, "o2_b1s1": MEDIUM, "o2_b1s2": MEDIUM,
    "o2_b2s1": MEDIUM, "o2_b2s2": MEDIUM, "fuel_system": MEDIUM,
    "iat": MEDIUM, "ect": MEDIUM,
    # SLOW: near-constant over a drive.
    "baro": SLOW, "ambient": SLOW, "fuel_level": SLOW, "ethanol": SLOW,
    "voltage": SLOW, "run_time": SLOW, "cat_b1s1": SLOW, "cat_b2s1": SLOW,
}

#: Mode 22 DIDs (gt.DID_TABLE) are asked for on MEDIUM cycles, one request
#: each (a DID cannot be batched with Mode 01).
DID_TIER = MEDIUM

#: Measured round trip per Mode 01 request on this GT, by PIDs in the request
#: (truck-mcp cassette no-accel-1000rpm 2026-09-18): 1 PID 55.7 ms, 3 PIDs
#: 55.0 ms, 6 PIDs 79.9 ms. Linear between the points.
REQUEST_COST_POINTS = ((1, 0.0557), (3, 0.0550), (6, 0.0799))
#: ATRV is not in the cassette. Assumed: one single-PID round trip plus the
#: 50 ms settle wait ObdxGt.poll_timed() puts inside the exchange.
ATRV_COST_S = 0.0557 + 0.05


def request_cost_s(n_pids: int) -> float:
    """Predicted round trip for one Mode 01 request carrying n_pids PIDs."""
    pts = REQUEST_COST_POINTS
    if n_pids <= pts[0][0]:
        return pts[0][1]
    for (n0, c0), (n1, c1) in zip(pts, pts[1:]):
        if n_pids <= n1:
            return c0 + (c1 - c0) * (n_pids - n0) / (n1 - n0)
    return pts[-1][1]


def _balanced_chunks(items: list, size: int) -> list[list]:
    """Split items, order kept, into the fewest chunks of at most `size`,
    as even as possible: 17 -> 6,6,5 and 26 -> 6,5,5,5,5. A request's cost
    rises with its PID count, so 5+5 beats 6+4 for the same round trips."""
    if not items:
        return []
    n = -(-len(items) // size)
    base, extra = divmod(len(items), n)
    out, i = [], 0
    for k in range(n):
        step = base + (1 if k < extra else 0)
        out.append(items[i:i + step])
        i += step
    return out


@dataclass
class PollPlan:
    """What the GT asks for on each cycle.

    pids:       Mode 01 PIDs to poll, in tier then table order.
    key_of:     PID -> canonical key.
    tiers:      PID -> tier name.
    batch_size: PIDs per request (1 after the ECM refused a batch).
    atrv:       True when the ECM does not support PID 42, so module voltage
                comes from the GT's own ATRV on SLOW cycles instead.
    n_dids:     Mode 22 DIDs polled on DID_TIER cycles."""
    pids: list[str]
    key_of: dict[str, str]
    tiers: dict[str, str]
    batch_size: int = MAX_PIDS_PER_REQUEST
    atrv: bool = False
    n_dids: int = 0
    _cache: dict = field(default_factory=dict, repr=False, compare=False)

    @classmethod
    def build(cls, pid_keys: Mapping[str, str], supported: Iterable[str] = (),
              batch_size: int = MAX_PIDS_PER_REQUEST, n_dids: int = 0
              ) -> "PollPlan":
        """pid_keys: PID -> canonical key (gt.PID_TABLE order). supported:
        the PIDs the ECM said it supports; empty (no probe answer) plans the
        whole table, as the sequential sweep did."""
        if not 1 <= batch_size <= MAX_PIDS_PER_REQUEST:
            raise ValueError(f"batch_size must be 1..{MAX_PIDS_PER_REQUEST}")
        sup = {p.upper() for p in supported}
        missing = sorted({k for k in pid_keys.values() if k not in KEY_TIERS})
        if missing:
            raise ValueError(f"no tier for channel(s) {missing}")
        chosen = [p for p in pid_keys if not sup or p in sup]
        tiers = {p: KEY_TIERS[pid_keys[p]] for p in chosen}
        # Tier order, then KEY_TIERS order within a tier: the first FAST
        # batch is rpm, vss, map, maf, tps, app, so the channels shift and
        # WOT analysis joins come from ONE exchange.
        rank = {k: i for i, k in enumerate(KEY_TIERS)}
        ordered = sorted(chosen, key=lambda p: (
            TIER_ORDER.index(tiers[p]), rank[pid_keys[p]]))
        return cls(pids=ordered, key_of={p: pid_keys[p] for p in chosen},
                   tiers=tiers, batch_size=batch_size,
                   atrv="42" not in chosen, n_dids=n_dids)

    # -- per cycle -------------------------------------------------------- #
    @staticmethod
    def tier_due(tier: str, cycle: int) -> bool:
        """Cycle 0 asks for everything, so no channel waits a whole SLOW
        period for its first reading."""
        return cycle % TIER_DIVISORS[tier] == 0

    def due_pids(self, cycle: int) -> list[str]:
        return [p for p in self.pids if self.tier_due(self.tiers[p], cycle)]

    def batches_for_cycle(self, cycle: int) -> list[list[str]]:
        """The requests for `cycle`, each a list of at most batch_size PIDs.
        Due PIDs are packed together across tiers (fast first), so a MEDIUM
        PID can fill a FAST batch's spare slot."""
        # The schedule repeats every SLOW period (cycle 0 = all tiers due).
        key = cycle % TIER_DIVISORS[SLOW]
        if key not in self._cache:
            self._cache[key] = _balanced_chunks(self.due_pids(cycle),
                                                self.batch_size)
        return [list(b) for b in self._cache[key]]

    def atrv_due(self, cycle: int) -> bool:
        return self.atrv and self.tier_due(SLOW, cycle)

    def dids_due(self, cycle: int) -> bool:
        return self.n_dids > 0 and self.tier_due(DID_TIER, cycle)

    def predicted_cycle_s(self, cycle: int) -> float:
        t = sum(request_cost_s(len(b)) for b in self.batches_for_cycle(cycle))
        if self.atrv_due(cycle):
            t += ATRV_COST_S
        if self.dids_due(cycle):
            t += self.n_dids * request_cost_s(1)
        return t

    def requests_for_cycle(self, cycle: int) -> int:
        return (len(self.batches_for_cycle(cycle))
                + (1 if self.atrv_due(cycle) else 0)
                + (self.n_dids if self.dids_due(cycle) else 0))

    # -- whole plan --------------------------------------------------------- #
    def _period(self) -> range:
        return range(1, TIER_DIVISORS[SLOW] + 1)    # one steady-state period

    def mean_requests_per_cycle(self) -> float:
        cycles = self._period()
        return sum(self.requests_for_cycle(c) for c in cycles) / len(cycles)

    def mean_cycle_s(self) -> float:
        cycles = self._period()
        return sum(self.predicted_cycle_s(c) for c in cycles) / len(cycles)

    def fast_cycle_s(self) -> float:
        """A cycle with only the FAST tier due (cycle 1)."""
        return self.predicted_cycle_s(1)

    def divisor_of(self, key: str) -> int:
        """Refresh divisor of a channel; a key outside the plan (a DID) gets
        its tier's, an unknown key the FAST one."""
        tier = KEY_TIERS.get(key)
        if tier is None:
            return TIER_DIVISORS[DID_TIER] if self.n_dids else 1
        return TIER_DIVISORS[tier]

    def keys_in(self, tier: str) -> list[str]:
        out = [self.key_of[p] for p in self.pids if self.tiers[p] == tier]
        if tier == SLOW and self.atrv:
            out.append("voltage")
        return out

    def summary(self) -> dict:
        """The plan as data, for the Dashboard and tests."""
        def keyed(batches):
            return [[self.key_of[p] for p in b] for b in batches]
        cycles = {"fast": 1, "medium": TIER_DIVISORS[MEDIUM],
                  "slow": TIER_DIVISORS[SLOW]}
        return {
            "batch_size": self.batch_size,
            "tiers": {t: self.keys_in(t) for t in TIER_ORDER},
            "divisors": dict(TIER_DIVISORS),
            "batches": {n: keyed(self.batches_for_cycle(c))
                        for n, c in cycles.items()},
            "requests": {n: self.requests_for_cycle(c)
                         for n, c in cycles.items()},
            "predicted_s": {n: self.predicted_cycle_s(c)
                            for n, c in cycles.items()},
            "mean_requests_per_cycle": self.mean_requests_per_cycle(),
            "mean_cycle_s": self.mean_cycle_s(),
            "atrv_voltage": self.atrv,
        }

    def describe(self) -> str:
        """One line for the Dashboard status bar and the connect log."""
        s = self.summary()
        fast = s["predicted_s"]["fast"]
        mean = s["mean_cycle_s"]
        n = {t: len(v) for t, v in s["tiers"].items()}
        how = (f"{self.batch_size} PIDs/request" if self.batch_size > 1
               else "1 PID/request (ECM refused batching)")
        return (f"poll plan: {len(self.pids)} PIDs, {how}; "
                f"fast {n[FAST]} every cycle ({s['requests']['fast']} req, "
                f"~{fast * 1000:.0f} ms = {1 / fast:.1f} Hz), "
                f"medium {n[MEDIUM]} every {TIER_DIVISORS[MEDIUM]}th, "
                f"slow {n[SLOW]} every {TIER_DIVISORS[SLOW]}th; "
                f"mean {s['mean_requests_per_cycle']:.2f} req/cycle, "
                f"~{mean * 1000:.0f} ms (predicted)")


def fresh_window_s(divisor: int, cycle_s: Optional[float]) -> Optional[float]:
    """How old a channel's last reading may be and still count as fresh:
    twice its tier period (divisor x the cycle time). None when there is no
    cycle time yet."""
    if cycle_s is None or cycle_s <= 0:
        return None
    return 2.0 * divisor * cycle_s
