"""
controls.py -- the device-control catalog (openobd/data/controls.json) as
validated, typed data, and the admission rule for the v0.40.0 Active Tests
live set. INERT: nothing here talks to a vehicle, opens a transport, or imports
Qt. It decides which captured controls MAY be offered for firing; firing
itself belongs to the control core (decision D2, 0f5688ed).

Schema version 2 (decisions D1 d7fedb9f, D3 e2c962dd). Top level:
    {"schema_version": 2, "controls": [entry, ...]}
Each entry keeps the capture fields devctl writes (id, module, request, cpid,
name, bus, status, source, observed_*, max_seconds, ...) and adds:
    role              "command" | "release". A release hands control back to
                      the module (GMLAN $AE return-to-normal); it is never
                      itself paired.
    hazard_class      0 body outputs, 1 powertrain stationary, 2 mechanical
                      motion or a state that can be left indeterminate.
    tier              0 / 1 / 2 (D1 Phase 1 sequencing), or null when the
                      entry is excluded from the live set.
    exclusion_reason  required, non-empty, exactly when tier is null.
    release_pair      id of the release that undoes this command; required for
                      every non-excluded command, null for a release.
    recovery          optional list of ids to run if the release alone does
                      not restore the actuator (TCCM motor drive).
    interlock_profile range_required ("PARK"), range_signal ("tcm_prndl";
                      PRNDL is readable on this truck, so null is refused),
                      parking_brake_attested (true), speed_max_kph (0),
                      rpm_window [lo, hi] ([0, 0] = KOEO), voltage_window
                      [lo, hi]. rpm_window and voltage_window must equal the
                      module's D3 default (MODULE_RPM_DEFAULTS,
                      DEFAULT_VOLTAGE_WINDOW) unless the entry carries
    profile_justification  a non-empty reason for the deviation.
    drive_capable     false for every entry at v0.40.0 (D1 empty whitelist).
    admission         decoded_name (the physical output, once decoded; null
                      while it is still "output bit"/"data [...]" -- the
                      Autel-derived `name` is NOT a decode, and the loader
                      refuses a decoded_name on an entry whose `name` still
                      reads as undecoded) and bench_verified.

`live_eligible` is deliberately NOT stored in the JSON: it is computed by
is_live_eligible() so a hand edit can never claim eligibility the rule does not
grant. The loader rejects a catalog that carries the key.
"""
from __future__ import annotations

import json
import math
import os
import re
from dataclasses import dataclass
from typing import Any, Mapping, Optional

SCHEMA_VERSION = 2
DEFAULT_PATH = os.path.join(os.path.dirname(__file__), "data", "controls.json")

ROLES = ("command", "release")
HAZARD_CLASSES = (0, 1, 2)
TIERS = (0, 1, 2)
RANGES = ("PARK",)
RANGE_SIGNALS = ("tcm_prndl",)

#: D3 default RPM window per (bus, module): BCM controls are key-on engine-off,
#: TCM and TCCM controls run with the engine idling. A module not listed has
#: no default, so its entries must carry a profile_justification.
MODULE_RPM_DEFAULTS: dict[tuple[str, str], tuple[int, int]] = {
    ("hs", "241"): (0, 0),
    ("hs", "7E2"): (400, 1200),
    ("hs", "7E4"): (400, 1200),
}
DEFAULT_VOLTAGE_WINDOW = (12.0, 15.5)

#: Phrases capture names carry while the physical output is still unknown.
UNDECODED_NAME_MARKERS = ("output bit", "data [", "meaning not decoded")

_HEX = re.compile(r"(?:[0-9A-F]{2})+")


class ControlsCatalogError(ValueError):
    """controls.json is missing, malformed, or violates schema version 2."""


@dataclass(frozen=True)
class InterlockProfile:
    range_required: str
    range_signal: Optional[str]
    parking_brake_attested: bool
    speed_max_kph: int
    rpm_window: tuple[int, int]
    voltage_window: tuple[float, float]

    @property
    def koeo(self) -> bool:
        """Key on, engine off: the control requires RPM 0."""
        return self.rpm_window == (0, 0)


@dataclass(frozen=True)
class Admission:
    decoded_name: Optional[str]
    bench_verified: bool


@dataclass(frozen=True)
class ControlEntry:
    id: str
    module: str
    bus: str
    request: str
    cpid: str
    name: str
    max_seconds: float
    status: Optional[str]
    source: Optional[str]
    role: str
    hazard_class: int
    tier: Optional[int]
    release_pair: Optional[str]
    recovery: tuple[str, ...]
    exclusion_reason: Optional[str]
    interlock_profile: InterlockProfile
    profile_justification: Optional[str]
    drive_capable: bool
    admission: Admission

    @property
    def is_release(self) -> bool:
        return self.role == "release"

    @property
    def excluded(self) -> bool:
        return self.tier is None


@dataclass(frozen=True)
class ControlsCatalog:
    schema_version: int
    entries: tuple[ControlEntry, ...]

    def get(self, control_id: Optional[str]) -> Optional[ControlEntry]:
        if control_id is None:
            return None
        for e in self.entries:
            if e.id == control_id:
                return e
        return None

    def __getitem__(self, control_id: str) -> ControlEntry:
        e = self.get(control_id)
        if e is None:
            raise KeyError(control_id)
        return e

    def __iter__(self):
        return iter(self.entries)

    def __len__(self) -> int:
        return len(self.entries)


# --------------------------------------------------------------------------- #
# Field validation
# --------------------------------------------------------------------------- #
def _fail(where: str, msg: str) -> ControlsCatalogError:
    return ControlsCatalogError(f"{where}: {msg}")


def _req(obj: Mapping[str, Any], key: str, where: str) -> Any:
    if key not in obj:
        raise _fail(where, f"missing required field {key!r}")
    return obj[key]


def _is_int(v: Any) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def _is_num(v: Any) -> bool:
    return (isinstance(v, (int, float)) and not isinstance(v, bool)
            and math.isfinite(v))


def _text(v: Any, key: str, where: str, *, nullable: bool = False) -> Optional[str]:
    if v is None and nullable:
        return None
    if not isinstance(v, str) or not v.strip():
        raise _fail(where, f"{key!r} must be a non-empty string"
                    + (" or null" if nullable else ""))
    return v


def _window(v: Any, key: str, where: str, *, integer: bool) -> tuple:
    ok = _is_int if integer else _is_num
    if (not isinstance(v, list) or len(v) != 2 or not all(ok(x) for x in v)
            or v[0] < 0 or v[0] > v[1]):
        kind = "integers" if integer else "numbers"
        raise _fail(where, f"{key!r} must be [lo, hi] {kind} with 0 <= lo <= hi")
    return (v[0], v[1])


def _profile(raw: Any, where: str, drive_capable: bool) -> InterlockProfile:
    where = f"{where}.interlock_profile"
    if not isinstance(raw, dict):
        raise _fail(where, "must be an object")
    rng = _req(raw, "range_required", where)
    if rng not in RANGES:
        raise _fail(where, f"range_required must be one of {RANGES} (D3), got {rng!r}")
    sig = _req(raw, "range_signal", where)
    if sig not in RANGE_SIGNALS:
        raise _fail(where, f"range_signal must be one of {RANGE_SIGNALS} (D3: PRNDL is "
                    f"readable on this truck, so attestation alone is refused), got {sig!r}")
    brake = _req(raw, "parking_brake_attested", where)
    if brake is not True:
        raise _fail(where, "parking_brake_attested must be true (D3)")
    speed = _req(raw, "speed_max_kph", where)
    if not _is_int(speed) or speed < 0:
        raise _fail(where, "speed_max_kph must be a non-negative integer")
    if speed != 0 and not drive_capable:
        raise _fail(where, "speed_max_kph must be 0 unless the entry is drive_capable")
    rpm = _window(_req(raw, "rpm_window", where), "rpm_window", where, integer=True)
    volts = _window(_req(raw, "voltage_window", where), "voltage_window", where,
                    integer=False)
    if volts[0] == volts[1]:
        raise _fail(where, "voltage_window must have lo < hi")
    return InterlockProfile(rng, sig, True, speed, rpm, (float(volts[0]), float(volts[1])))


def _admission(raw: Any, where: str) -> Admission:
    where = f"{where}.admission"
    if not isinstance(raw, dict):
        raise _fail(where, "must be an object")
    if "live_eligible" in raw:
        raise _fail(where, "'live_eligible' is computed by is_live_eligible() "
                    "and must not be stored in the catalog")
    name = _text(_req(raw, "decoded_name", where), "decoded_name", where, nullable=True)
    verified = _req(raw, "bench_verified", where)
    if not isinstance(verified, bool):
        raise _fail(where, "bench_verified must be true or false")
    return Admission(name, verified)


def _entry(raw: Any, index: int) -> ControlEntry:
    where = f"controls[{index}]"
    if not isinstance(raw, dict):
        raise _fail(where, "must be an object")
    cid = _text(_req(raw, "id", where), "id", where)
    where = f"control {cid}"
    module = _text(_req(raw, "module", where), "module", where)
    bus = raw.get("bus", "hs")
    if bus not in ("hs", "sw"):
        raise _fail(where, f"bus must be 'hs' or 'sw', got {bus!r}")
    request = _text(_req(raw, "request", where), "request", where)
    if not _HEX.fullmatch(request) or not request.startswith("AE"):
        raise _fail(where, f"request must be uppercase hex bytes starting AE, got {request!r}")
    cpid = _text(_req(raw, "cpid", where), "cpid", where)
    name = _text(_req(raw, "name", where), "name", where)
    max_seconds = _req(raw, "max_seconds", where)
    if not _is_num(max_seconds) or max_seconds <= 0:
        raise _fail(where, "max_seconds must be a positive number")

    role = _req(raw, "role", where)
    if role not in ROLES:
        raise _fail(where, f"role must be one of {ROLES}, got {role!r}")
    hazard = _req(raw, "hazard_class", where)
    if not _is_int(hazard) or hazard not in HAZARD_CLASSES:
        raise _fail(where, f"hazard_class must be one of {HAZARD_CLASSES}, got {hazard!r}")
    tier = _req(raw, "tier", where)
    if tier is not None and (not _is_int(tier) or tier not in TIERS):
        raise _fail(where, f"tier must be one of {TIERS} or null, got {tier!r}")
    reason = raw.get("exclusion_reason")
    if tier is None:
        _text(reason, "exclusion_reason", where)
    elif reason is not None:
        raise _fail(where, "exclusion_reason is only allowed on an excluded entry (tier null)")

    pair = _text(_req(raw, "release_pair", where), "release_pair", where, nullable=True)
    recovery = raw.get("recovery", [])
    if not isinstance(recovery, list) or not all(
            isinstance(r, str) and r.strip() for r in recovery):
        raise _fail(where, "recovery must be a list of control ids")

    drive = _req(raw, "drive_capable", where)
    if not isinstance(drive, bool):
        raise _fail(where, "drive_capable must be true or false")
    profile = _profile(_req(raw, "interlock_profile", where), where, drive)
    admission = _admission(_req(raw, "admission", where), where)
    if admission.decoded_name is not None:
        lowered = name.lower()
        marker = next((m for m in UNDECODED_NAME_MARKERS if m in lowered), None)
        if marker is not None:
            raise _fail(where, f"admission.decoded_name is set but name still reads as "
                        f"undecoded ({marker!r}); rename the entry when the output is decoded")
    justification = raw.get("profile_justification")
    if justification is not None:
        _text(justification, "profile_justification", where)
    default_rpm = MODULE_RPM_DEFAULTS.get((bus, module.upper()))
    if justification is None:
        if default_rpm is None:
            raise _fail(where, f"module {module} on {bus} has no D3 default RPM window; "
                        "a profile_justification is required")
        if profile.rpm_window != default_rpm:
            raise _fail(where, f"interlock_profile.rpm_window {list(profile.rpm_window)} "
                        f"differs from the module default {list(default_rpm)} without a "
                        "profile_justification")
        if profile.voltage_window != DEFAULT_VOLTAGE_WINDOW:
            raise _fail(where, f"interlock_profile.voltage_window "
                        f"{list(profile.voltage_window)} differs from the default "
                        f"{list(DEFAULT_VOLTAGE_WINDOW)} without a profile_justification")
    if "live_eligible" in raw:
        raise _fail(where, "'live_eligible' is computed by is_live_eligible() "
                    "and must not be stored in the catalog")

    return ControlEntry(
        id=cid, module=module, bus=bus, request=request, cpid=cpid, name=name,
        max_seconds=max_seconds,
        status=raw.get("status") if isinstance(raw.get("status"), str) else None,
        source=raw.get("source") if isinstance(raw.get("source"), str) else None,
        role=role, hazard_class=hazard, tier=tier,
        release_pair=pair, recovery=tuple(recovery), exclusion_reason=reason,
        interlock_profile=profile, profile_justification=justification,
        drive_capable=drive, admission=admission)


def _cross_check(entries: list[ControlEntry]) -> None:
    by_id: dict[str, ControlEntry] = {}
    by_wire: dict[tuple[str, str, str], str] = {}
    for e in entries:
        if e.id in by_id:
            raise _fail(f"control {e.id}", "duplicate id")
        by_id[e.id] = e
        key = (e.bus, e.module.upper(), e.request)
        if key in by_wire:
            raise _fail(f"control {e.id}", f"duplicate (bus, module, request) {key}, "
                        f"same as {by_wire[key]}")
        by_wire[key] = e.id
    for e in entries:
        where = f"control {e.id}"
        if e.is_release:
            if e.release_pair is not None:
                raise _fail(where, "a release entry must not itself have a release_pair")
        elif e.tier is not None and e.release_pair is None:
            raise _fail(where, "a non-excluded command needs a release_pair (D1)")
        if e.release_pair is not None:
            target = by_id.get(e.release_pair)
            if target is None:
                raise _fail(where, f"release_pair {e.release_pair!r} is not in the catalog")
            if not target.is_release:
                raise _fail(where, f"release_pair {e.release_pair!r} is not a release entry")
            if (target.bus, target.module) != (e.bus, e.module):
                raise _fail(where, f"release_pair {e.release_pair!r} is on a different module")
        for r in e.recovery:
            target = by_id.get(r)
            if target is None:
                raise _fail(where, f"recovery id {r!r} is not in the catalog")
            if (target.bus, target.module) != (e.bus, e.module):
                raise _fail(where, f"recovery id {r!r} is on a different module")
            if r == e.id:
                raise _fail(where, "an entry cannot be its own recovery step")
            if target.recovery:
                raise _fail(where, f"recovery id {r!r} has recovery steps of its own; "
                            "recovery is one level deep")
        if e.is_release and e.recovery:
            raise _fail(where, "a release entry must not carry recovery steps")


def parse_controls(data: Any) -> ControlsCatalog:
    """Validate an already-decoded controls.json document."""
    if not isinstance(data, dict):
        raise ControlsCatalogError("catalog: top level must be an object")
    version = data.get("schema_version")
    if not _is_int(version) or version != SCHEMA_VERSION:
        raise ControlsCatalogError(
            f"catalog: schema_version must be {SCHEMA_VERSION}, got {version!r}")
    raw = data.get("controls")
    if not isinstance(raw, list):
        raise ControlsCatalogError("catalog: 'controls' must be a list")
    entries = [_entry(r, i) for i, r in enumerate(raw)]
    _cross_check(entries)
    return ControlsCatalog(version, tuple(entries))


def _no_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict:
    out: dict = {}
    for key, value in pairs:
        if key in out:
            raise ControlsCatalogError(f"catalog: duplicate JSON key {key!r}")
        out[key] = value
    return out


def load_controls(path: str = DEFAULT_PATH) -> ControlsCatalog:
    """Read and validate the catalog. Every failure is a ControlsCatalogError."""
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh, object_pairs_hook=_no_duplicate_keys)
    except ControlsCatalogError:
        raise
    except RecursionError as e:
        raise ControlsCatalogError(f"catalog nests too deeply to parse: {path}") from e
    except FileNotFoundError as e:
        raise ControlsCatalogError(f"catalog not found: {path}") from e
    except (OSError, UnicodeDecodeError) as e:
        raise ControlsCatalogError(f"catalog unreadable: {path}: {e}") from e
    except json.JSONDecodeError as e:
        raise ControlsCatalogError(f"catalog is not valid JSON: {path}: {e}") from e
    return parse_controls(data)


# --------------------------------------------------------------------------- #
# Admission (D1)
# --------------------------------------------------------------------------- #
def ineligibility_reasons(entry: ControlEntry, catalog: ControlsCatalog,
                          _seen: frozenset = frozenset()) -> list[str]:
    """Why `entry` may NOT be offered for live firing in v0.40.0; empty when it
    may. The rule (D1 d7fedb9f), all of:
      * not excluded (tier is not None);
      * not drive_capable (the v0.40.0 drive-capable whitelist is empty);
      * decoded to a named physical output (admission.decoded_name);
      * bench_verified;
      * a release with no pair, OR a command whose release_pair resolves in
        `catalog` to a release entry that is itself eligible (a command is
        never offered when its return-to-normal could not be sent);
      * every recovery id resolves to an entry that is itself eligible (a
        control is never offered when its recovery could not be run).
    """
    if entry.id in _seen:
        return [f"{entry.id} is part of a pairing/recovery cycle"]
    seen = _seen | {entry.id}
    why: list[str] = []
    if entry.tier is None:
        why.append(f"excluded (tier null): {entry.exclusion_reason}")
    if entry.drive_capable:
        why.append("drive-capable controls are not admitted in v0.40.0")
    if not entry.admission.decoded_name:
        why.append("not decoded to a named physical output")
    if not entry.admission.bench_verified:
        why.append("not bench-verified")
    if entry.is_release:
        if entry.release_pair is not None:
            why.append("a release must not itself be paired")
    else:
        release = catalog.get(entry.release_pair)
        if release is None or not release.is_release:
            why.append(f"release_pair {entry.release_pair!r} does not resolve to a release")
        elif ineligibility_reasons(release, catalog, seen):
            why.append(f"release {release.id} is not eligible")
    for rid in entry.recovery:
        step = catalog.get(rid)
        if step is None:
            why.append(f"recovery step {rid!r} is not in the catalog")
        elif ineligibility_reasons(step, catalog, seen):
            why.append(f"recovery step {rid} is not eligible")
    return why


def is_live_eligible(entry: ControlEntry, catalog: ControlsCatalog) -> bool:
    """True only when ineligibility_reasons() finds nothing."""
    return not ineligibility_reasons(entry, catalog)


def live_eligible(catalog: ControlsCatalog) -> tuple[ControlEntry, ...]:
    return tuple(e for e in catalog if is_live_eligible(e, catalog))


def phase1_tiers(catalog: ControlsCatalog, *,
                 eligible_only: bool = False) -> dict[int, tuple[ControlEntry, ...]]:
    """Non-excluded entries grouped by D1 tier, {0: (...), 1: (...), 2: (...)},
    each in catalog order. Every tier key is present, possibly empty. With
    eligible_only, only entries is_live_eligible() admits."""
    groups: dict[int, list[ControlEntry]] = {t: [] for t in TIERS}
    for e in catalog:
        if e.tier is None:
            continue
        if eligible_only and not is_live_eligible(e, catalog):
            continue
        groups[e.tier].append(e)
    return {t: tuple(v) for t, v in groups.items()}


def unclassified_fields(module: str, bus: str = "hs") -> dict:
    """Schema-v2 fields for a control newly imported from a capture (devctl
    --import): excluded until a person classifies, pairs and decodes it, with
    the most conservative hazard class and the module's D3 RPM default. A
    module with no default gets KOEO plus a profile_justification saying so."""
    default_rpm = MODULE_RPM_DEFAULTS.get((bus, module.upper()))
    fields = {
        "role": "command",
        "hazard_class": 2,
        "tier": None,
        "release_pair": None,
        "exclusion_reason": "imported from a capture; not yet classified, "
                            "paired or decoded (D1)",
        "interlock_profile": {
            "range_required": "PARK", "range_signal": "tcm_prndl",
            "parking_brake_attested": True, "speed_max_kph": 0,
            "rpm_window": list(default_rpm or (0, 0)),
            "voltage_window": list(DEFAULT_VOLTAGE_WINDOW)},
        "drive_capable": False,
        "admission": {"decoded_name": None, "bench_verified": False},
    }
    if default_rpm is None:
        fields["profile_justification"] = (
            f"imported: module {module} on {bus} has no D3 default RPM window; "
            "key-on engine-off until classified")
    return fields
