"""controls.json schema v2 and the v0.40.0 admission rule (D1 d7fedb9f,
D3 e2c962dd). Pure data: nothing here touches a vehicle."""
import copy
import json
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from openobd import controls as cc  # noqa: E402
from openobd import devctl as dc  # noqa: E402


@pytest.fixture(scope="module")
def raw():
    with open(cc.DEFAULT_PATH, encoding="utf-8") as fh:
        return json.load(fh)


@pytest.fixture(scope="module")
def cat():
    return cc.load_controls()


# -- the shipped catalog ---------------------------------------------------- #
def test_shipped_catalog_validates(cat, raw):
    assert raw["schema_version"] == 2
    assert cat.schema_version == cc.SCHEMA_VERSION == 2
    assert len(cat) == 27


def test_every_pair_and_recovery_resolves(cat):
    for e in cat:
        if e.release_pair is not None:
            target = cat[e.release_pair]
            assert target.is_release and target.release_pair is None
            assert target.module == e.module
        for r in e.recovery:
            assert cat.get(r) is not None
    for e in cat:
        if not e.is_release and not e.excluded:
            assert e.release_pair is not None, e.id


def test_release_pairs_match_d1(cat):
    want = {"241": "241-AE00-19", "7E4": "7E4-AE01-3"}
    for e in cat:
        if e.is_release or e.excluded:
            continue
        if e.module == "7E2":
            assert e.release_pair == ("7E2-AE30-13" if e.cpid == "30" else "7E2-AE38-16")
        else:
            assert e.release_pair == want[e.module]
    releases = {e.id for e in cat if e.is_release}
    assert releases == {"241-AE00-19", "7E2-AE30-13", "7E2-AE38-16", "7E4-AE01-3"}


def test_motor_drive_carries_recovery(cat):
    for cid in ("7E4-AE01-9", "7E4-AE01-10"):
        e = cat[cid]
        assert e.recovery == ("7E4-AE01-3", "7E4-AE03-1")
        assert e.hazard_class == 2 and e.tier == 2
        assert e.interlock_profile.rpm_window == (400, 1200)


def test_exactly_one_entry_excluded(cat):
    excluded = [e for e in cat if e.excluded]
    assert [e.id for e in excluded] == ["7E4-AE03-1"]
    assert excluded[0].exclusion_reason


def test_module_defaults(cat):
    for e in cat:
        p = e.interlock_profile
        assert p.range_required == "PARK" and p.range_signal == "tcm_prndl"
        assert p.parking_brake_attested is True and p.speed_max_kph == 0
        assert p.voltage_window == (12.0, 15.5)
        assert e.drive_capable is False
        if e.module == "241":
            assert (e.hazard_class, e.tier) == (0, 0) and p.koeo
        elif e.module == "7E2":
            assert (e.hazard_class, e.tier) == (1, 1)
            assert p.rpm_window == (400, 1200)
        elif not e.excluded:
            assert e.tier == 2 and p.rpm_window == (400, 1200)
            assert e.hazard_class == (2 if e.id in ("7E4-AE01-9", "7E4-AE01-10") else 1)


def test_undecoded_outputs_have_no_decoded_name(cat):
    for e in cat:
        if "output bit" in e.name or "data [" in e.name:
            assert e.admission.decoded_name is None, e.id


def test_nothing_is_live_eligible_today(cat, raw):
    assert all(not e.admission.bench_verified for e in cat)
    assert cc.live_eligible(cat) == ()
    assert all(v == () for v in cc.phase1_tiers(cat, eligible_only=True).values())
    for r in raw["controls"]:
        assert "live_eligible" not in r and "live_eligible" not in r["admission"]


def test_phase1_tiers_groups_non_excluded(cat):
    tiers = cc.phase1_tiers(cat)
    assert set(tiers) == {0, 1, 2}
    assert {e.module for e in tiers[0]} == {"241"} and len(tiers[0]) == 11
    assert {e.module for e in tiers[1]} == {"7E2"} and len(tiers[1]) == 6
    assert {e.module for e in tiers[2]} == {"7E4"} and len(tiers[2]) == 9
    assert sum(len(v) for v in tiers.values()) == 26


def test_controls_module_is_qt_free():
    import ast
    with open(os.path.join(ROOT, "openobd", "controls.py"), encoding="utf-8") as fh:
        tree = ast.parse(fh.read())
    mods = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            mods.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            mods.add((node.module or "").split(".")[0] if node.level == 0 else ".")
    assert mods <= {"__future__", "json", "math", "os", "re", "dataclasses", "typing"}


# -- the eligibility rule on synthetic entries ------------------------------ #
def _doc(**overrides):
    """A minimal valid v2 catalog: one release R and one command C paired to
    it, both decoded and bench-verified (so both are eligible). overrides
    patch C's fields; R_<field> patches R's."""
    def entry(cid, role, pair):
        return {"id": cid, "module": "241", "bus": "hs",
                "request": "AE00" if role == "release" else "AE100404000000",
                "cpid": "00" if role == "release" else "10", "name": cid,
                "max_seconds": 20, "role": role, "hazard_class": 0, "tier": 0,
                "release_pair": pair,
                "interlock_profile": {
                    "range_required": "PARK", "range_signal": "tcm_prndl",
                    "parking_brake_attested": True, "speed_max_kph": 0,
                    "rpm_window": [0, 0], "voltage_window": [12.0, 15.5]},
                "drive_capable": False,
                "admission": {"decoded_name": f"{cid} output",
                              "bench_verified": True}}
    r, c = entry("R", "release", None), entry("C", "command", "R")
    for k, v in overrides.items():
        target = r if k.startswith("R_") else c
        key = k[2:] if k.startswith("R_") else k
        if "." in key:
            outer, inner = key.split(".")
            target[outer][inner] = v
        elif v is _DROP:
            target.pop(key)
        else:
            target[key] = v
    return {"schema_version": 2, "controls": [r, c]}


_DROP = object()


def _eligible(**overrides):
    cat = cc.parse_controls(_doc(**overrides))
    return cc.is_live_eligible(cat["C"], cat)


def test_rule_admits_decoded_verified_paired_command():
    cat = cc.parse_controls(_doc())
    assert cc.is_live_eligible(cat["R"], cat)
    assert cc.is_live_eligible(cat["C"], cat)
    assert [e.id for e in cc.phase1_tiers(cat, eligible_only=True)[0]] == ["R", "C"]


@pytest.mark.parametrize("overrides", [
    {"admission.decoded_name": None},
    {"admission.bench_verified": False},
    {"drive_capable": True},
    {"tier": None, "exclusion_reason": "not in Phase 1", "release_pair": None},
    {"R_admission.bench_verified": False},      # its release is not verified
    {"R_admission.decoded_name": None},         # its release is not decoded
])
def test_rule_refuses(overrides):
    assert not _eligible(**overrides)


def test_drive_capable_never_eligible_even_with_speed_profile():
    assert not _eligible(**{"drive_capable": True,
                            "interlock_profile.speed_max_kph": 5})


def test_release_without_verification_is_not_eligible():
    cat = cc.parse_controls(_doc(**{"R_admission.bench_verified": False}))
    assert not cc.is_live_eligible(cat["R"], cat)


def test_rule_refuses_unresolved_pair_on_unvalidated_catalog():
    # a catalog built without the loader must still not admit a dangling pair
    good = cc.parse_controls(_doc())
    lone = cc.ControlsCatalog(2, (good["C"],))
    assert not cc.is_live_eligible(good["C"], lone)


# -- the loader rejects broken catalogs ------------------------------------- #
@pytest.mark.parametrize("mutate, message", [
    (lambda d: d.update(schema_version=1), "schema_version"),
    (lambda d: d.pop("schema_version"), "schema_version"),
    (lambda d: d.update(schema_version=True), "schema_version"),
    (lambda d: d.update(controls={}), "'controls' must be a list"),
    (lambda d: d["controls"][1].update(release_pair="NOPE"), "not in the catalog"),
    (lambda d: d["controls"][1].update(release_pair=None), "needs a release_pair"),
    (lambda d: d["controls"][0].update(release_pair="C"), "must not itself have a release_pair"),
    (lambda d: d["controls"][1].update(recovery=["GONE"]), "recovery id"),
    (lambda d: d["controls"][1].pop("hazard_class"), "hazard_class"),
    (lambda d: d["controls"][1].update(hazard_class=3), "hazard_class"),
    (lambda d: d["controls"][1].update(tier=None), "exclusion_reason"),
    (lambda d: d["controls"][1].update(exclusion_reason="x"), "only allowed"),
    (lambda d: d["controls"][1]["admission"].update(live_eligible=True), "computed"),
    (lambda d: d["controls"][1].update(live_eligible=True), "computed"),
    (lambda d: d["controls"][1]["interlock_profile"].update(range_required="NEUTRAL"), "range_required"),
    (lambda d: d["controls"][1]["interlock_profile"].update(parking_brake_attested=False), "parking_brake"),
    (lambda d: d["controls"][1]["interlock_profile"].update(speed_max_kph=5), "drive_capable"),
    (lambda d: d["controls"][1]["interlock_profile"].update(rpm_window=[1200, 400]), "rpm_window"),
    (lambda d: d["controls"][1]["interlock_profile"].update(voltage_window=[12.0]), "voltage_window"),
    (lambda d: d["controls"].append(copy.deepcopy(d["controls"][1])), "duplicate"),
    (lambda d: d["controls"][1].update(release_pair="C2") or d["controls"].append(
        dict(copy.deepcopy(d["controls"][1]), id="C2", release_pair="R")),
     "is not a release entry"),
    (lambda d: d["controls"][0].update(module="7E4"), "different module"),
    (lambda d: d["controls"][1].update(request="ae10"), "request"),
])
def test_loader_rejects(mutate, message):
    doc = _doc()
    mutate(doc)
    with pytest.raises(cc.ControlsCatalogError, match=message):
        cc.parse_controls(doc)


def test_load_controls_reports_missing_and_bad_json(tmp_path):
    with pytest.raises(cc.ControlsCatalogError, match="not found"):
        cc.load_controls(str(tmp_path / "absent.json"))
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    with pytest.raises(cc.ControlsCatalogError, match="not valid JSON"):
        cc.load_controls(str(bad))


def test_load_controls_rejects_broken_file(tmp_path, raw):
    doc = copy.deepcopy(raw)
    next(c for c in doc["controls"] if c["id"] == "241-AE10-26")["release_pair"] = "241-AE00-99"
    p = tmp_path / "controls.json"
    p.write_text(json.dumps(doc), encoding="utf-8")
    with pytest.raises(cc.ControlsCatalogError, match="241-AE00-99"):
        cc.load_controls(str(p))


# -- devctl --import keeps the catalog valid -------------------------------- #
def test_devctl_import_writes_schema_v2_entries(raw):
    doc = copy.deepcopy(raw)
    ex = [{"module": "7E0", "request": "AE2001010000", "response": "EE20",
           "outcome": "positive", "t": 1}]
    new = dc.merge_into_catalog(doc, ex, "x.jsonl")
    assert new == ["7E0-AE20-28"]
    cat = cc.parse_controls(doc)
    e = cat["7E0-AE20-28"]
    assert e.excluded and e.hazard_class == 2 and not cc.is_live_eligible(e, cat)
    assert len(cc.phase1_tiers(cat)[2]) == 9            # not admitted to a tier


def test_devctl_empty_catalog_is_schema_v2(tmp_path):
    doc = dc.load_catalog(str(tmp_path / "absent.json"))
    assert cc.parse_controls(doc).entries == ()
