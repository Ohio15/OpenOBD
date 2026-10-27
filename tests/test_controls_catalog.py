"""controls.json schema v2 and the v0.40.0 admission rule (D1 d7fedb9f,
D3 e2c962dd). Pure data: nothing here touches a vehicle."""
import copy
import dataclasses
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


def test_motor_drive_is_not_eligible_while_its_recovery_step_is_not(cat):
    # D5: Tier 2 is not admitted at ship, and independently the motor drive
    # cannot be live while the excluded TCCM learn is in its recovery.
    for cid in ("7E4-AE01-9", "7E4-AE01-10"):
        e = cat[cid]
        assert e.hazard_class == 2 and e.tier == 2
        assert "7E4-AE03-1" in e.recovery
        why = cc.ineligibility_reasons(e, cat)
        assert "recovery step 7E4-AE03-1 is not eligible" in why
        assert any("tier 2 is not admitted" in w for w in why)


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


def test_nothing_is_decoded_today(cat):
    # D5: Autel labels and observed actuator travel are not decodes.
    assert [e.id for e in cat if e.admission.decoded_name] == []
    assert all(e.dialect == "gmlan" for e in cat)
    assert all(e.admission.observed_result is None for e in cat)


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
                "dialect": "gmlan", "observed_outcome": "positive",
                "max_seconds": 20, "role": role, "hazard_class": 0, "tier": 0,
                "release_pair": pair,
                "interlock_profile": {
                    "range_required": "PARK", "range_signal": "tcm_prndl",
                    "parking_brake_attested": True, "speed_max_kph": 0,
                    "rpm_window": [0, 0], "voltage_window": [12.0, 15.5]},
                "drive_capable": False,
                "admission": {"decoded_name": f"{cid} output",
                              "bench_verified": True,
                              "observed_result": f"{cid} seen on the bench"}}
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
    {"tier": 1},                                # D5: only Tier 0 admitted
    {"tier": 2},
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
        dict(copy.deepcopy(d["controls"][1]), id="C2", release_pair="R",
             request="AE100808000000")),
     "is not a release entry"),
    (lambda d: d["controls"][0].update(module="7E4", profile_justification="test"),
     "different module"),
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


# -- review round 1: tightened loader and rule ------------------------------ #
def test_recovery_step_must_be_eligible():
    doc = _doc()
    learn = copy.deepcopy(doc["controls"][1])
    learn.update(id="L", request="AE0302020000", cpid="03", tier=None,
                 release_pair=None, exclusion_reason="conditions undecoded")
    doc["controls"].append(learn)
    doc["controls"][1]["recovery"] = ["L"]
    cat = cc.parse_controls(doc)
    assert not cc.is_live_eligible(cat["C"], cat)
    assert "recovery step L is not eligible" in cc.ineligibility_reasons(cat["C"], cat)
    doc["controls"][2].update(tier=0, release_pair="R")
    doc["controls"][2].pop("exclusion_reason")
    cat = cc.parse_controls(doc)
    assert cc.is_live_eligible(cat["C"], cat)


@pytest.mark.parametrize("name", [
    "body control module control 13: output bit 01 commanded ON",
    "transmission module control 30: data [0600000000]",
    "something (meaning not decoded yet)",
])
def test_decoded_name_refused_on_an_undecoded_name(name):
    doc = _doc()
    doc["controls"][1]["name"] = name
    with pytest.raises(cc.ControlsCatalogError, match="still reads as undecoded"):
        cc.parse_controls(doc)
    doc["controls"][1]["admission"]["decoded_name"] = None
    cc.parse_controls(doc)                       # null is fine on such a name


def _bcm_without_justification():
    doc = _doc()
    for c in doc["controls"]:
        assert "profile_justification" not in c
    return doc


def test_range_signal_null_needs_justification():
    doc = _bcm_without_justification()
    doc["controls"][1]["interlock_profile"]["range_signal"] = None
    with pytest.raises(cc.ControlsCatalogError, match="range_signal is null"):
        cc.parse_controls(doc)
    doc["controls"][1]["profile_justification"] = "OEM procedure: no range needed"
    cat = cc.parse_controls(doc)
    assert cat["C"].interlock_profile.range_signal is None


def test_silent_rpm_downgrade_refused_justified_accepted():
    doc = _bcm_without_justification()
    doc["controls"][1]["interlock_profile"]["rpm_window"] = [400, 1200]
    with pytest.raises(cc.ControlsCatalogError, match="profile_justification"):
        cc.parse_controls(doc)
    doc["controls"][1]["profile_justification"] = "OEM procedure: engine running"
    cat = cc.parse_controls(doc)
    assert cat["C"].interlock_profile.rpm_window == (400, 1200)
    doc["controls"][1]["profile_justification"] = "  "
    with pytest.raises(cc.ControlsCatalogError, match="profile_justification"):
        cc.parse_controls(doc)


@pytest.mark.parametrize("window", [[11.0, 15.5], [12.0, 16.0], [12.5, 15.0]])
def test_voltage_window_is_fixed_even_with_justification(window):
    doc = _doc()
    doc["controls"][1]["interlock_profile"]["voltage_window"] = window
    doc["controls"][1]["profile_justification"] = "any reason"
    with pytest.raises(cc.ControlsCatalogError, match="voltage_window must be exactly"):
        cc.parse_controls(doc)


def test_module_defaults_bind_tcm_and_tccm():
    doc = _doc()
    for c in doc["controls"]:
        c["module"] = "7E2"
    with pytest.raises(cc.ControlsCatalogError, match="module default"):
        cc.parse_controls(doc)                   # [0,0] is not the TCM default
    for c in doc["controls"]:
        c["interlock_profile"]["rpm_window"] = [400, 1200]
    cc.parse_controls(doc)


def test_unknown_module_needs_justification():
    doc = _doc()
    for c in doc["controls"]:
        c["module"] = "7E0"
    with pytest.raises(cc.ControlsCatalogError, match="no D3 default"):
        cc.parse_controls(doc)


def test_duplicate_json_keys_refused(tmp_path):
    p = tmp_path / "controls.json"
    p.write_text('{"schema_version": 2, "schema_version": 2, "controls": []}',
                 encoding="utf-8")
    with pytest.raises(cc.ControlsCatalogError, match="duplicate JSON key"):
        cc.load_controls(str(p))
    body = json.dumps(_doc()["controls"][1])
    dup = body[:-1] + ', "tier": 0}'
    p.write_text('{"schema_version": 2, "controls": [%s, %s]}'
                 % (json.dumps(_doc()["controls"][0]), dup), encoding="utf-8")
    with pytest.raises(cc.ControlsCatalogError, match="duplicate JSON key 'tier'"):
        cc.load_controls(str(p))


def test_duplicate_wire_request_refused():
    doc = _doc()
    twin = copy.deepcopy(doc["controls"][1])
    twin["id"] = "C-TWIN"
    doc["controls"].append(twin)
    with pytest.raises(cc.ControlsCatalogError, match=r"duplicate \(bus, module, request\)"):
        cc.parse_controls(doc)
    twin["bus"] = "sw"
    twin["profile_justification"] = "body bus twin"
    twin["release_pair"] = None
    twin["tier"] = None
    twin["exclusion_reason"] = "test"
    cc.parse_controls(doc)                       # same bytes on another bus is fine


def test_recovery_is_one_level_and_never_on_a_release():
    doc = _doc()
    doc["controls"][0]["recovery"] = ["C"]
    with pytest.raises(cc.ControlsCatalogError):
        cc.parse_controls(doc)


def test_deep_nesting_is_a_catalog_error(tmp_path):
    p = tmp_path / "controls.json"
    p.write_text("[" * 200000 + "]" * 200000, encoding="utf-8")
    with pytest.raises(cc.ControlsCatalogError):
        cc.load_controls(str(p))


@pytest.mark.parametrize("module, rpm", [("241", [0, 0]), ("7E2", [400, 1200]),
                                         ("7E4", [400, 1200])])
def test_unclassified_fields_use_the_module_default(module, rpm):
    f = cc.unclassified_fields(module)
    assert f["interlock_profile"]["rpm_window"] == rpm
    assert "profile_justification" not in f


def test_unclassified_fields_for_unknown_module_are_justified_koeo():
    f = cc.unclassified_fields("7E0")
    assert f["interlock_profile"]["rpm_window"] == [0, 0]
    assert f["profile_justification"]


# -- D5 amendments ------------------------------------------------------------ #
def test_admitted_tiers_is_tier_zero_only():
    assert cc.ADMITTED_TIERS == (0,)


def test_fully_decoded_verified_tier1_entry_is_not_eligible():
    doc = _doc()
    for c in doc["controls"]:
        c["module"] = "7E2"
        c["tier"] = 1
        c["hazard_class"] = 1
        c["interlock_profile"]["rpm_window"] = [400, 1200]
    doc["controls"][1].update(request="AE300600000000", cpid="30")
    cat = cc.parse_controls(doc)
    assert not cc.is_live_eligible(cat["C"], cat)
    assert cc.ineligibility_reasons(cat["C"], cat)[0].startswith("tier 1 is not admitted")


@pytest.mark.parametrize("value", [_DROP, "kwp2000", None, "GMLAN"])
def test_dialect_required_and_enumerated(value):
    doc = _doc()
    if value is _DROP:
        doc["controls"][1].pop("dialect")
    else:
        doc["controls"][1]["dialect"] = value
    with pytest.raises(cc.ControlsCatalogError, match="dialect"):
        cc.parse_controls(doc)


def test_uds_dialect_accepted():
    doc = _doc()
    doc["controls"][1]["dialect"] = "uds"
    assert cc.parse_controls(doc)["C"].dialect == "uds"


@pytest.mark.parametrize("result", [None, "", "   "])
def test_bench_verified_needs_observed_result(result):
    doc = _doc()
    doc["controls"][1]["admission"]["observed_result"] = result
    with pytest.raises(cc.ControlsCatalogError, match="observed_result"):
        cc.parse_controls(doc)


def test_observed_result_is_required_key_and_rule_checks_it():
    doc = _doc()
    doc["controls"][1]["admission"].pop("observed_result")
    with pytest.raises(cc.ControlsCatalogError, match="observed_result"):
        cc.parse_controls(doc)
    # a catalog built without the loader still cannot claim eligibility
    good = cc.parse_controls(_doc())
    hollow = dataclasses.replace(
        good["C"], admission=dataclasses.replace(good["C"].admission,
                                                 observed_result=None))
    assert "bench-verified without an observed_result" in \
        cc.ineligibility_reasons(hollow, good)


@pytest.mark.parametrize("where", ["admission", "interlock_profile", "entry", "top"])
def test_closed_key_set_rejects_a_typo(where):
    doc = _doc()
    c = doc["controls"][1]
    if where == "admission":
        c["admission"]["observed_resutl"] = "lamp lit"
    elif where == "interlock_profile":
        c["interlock_profile"]["rpm_windwo"] = [0, 0]
    elif where == "entry":
        c["releas_pair"] = "R"
    else:
        doc["schema_versoin"] = 2
    with pytest.raises(cc.ControlsCatalogError, match="unknown key"):
        cc.parse_controls(doc)


def test_misspelled_observed_result_fails_load(tmp_path):
    doc = _doc()
    adm = doc["controls"][1]["admission"]
    adm["observed_resutl"] = adm.pop("observed_result")
    p = tmp_path / "controls.json"
    p.write_text(json.dumps(doc), encoding="utf-8")
    with pytest.raises(cc.ControlsCatalogError):
        cc.load_controls(str(p))


def test_commanded_off_can_never_be_a_release(raw):
    doc = copy.deepcopy(raw)
    off = next(c for c in doc["controls"] if c["id"] == "7E4-AE01-5")
    assert off["request"] == "AE018000000000" and off["observed_outcome"] == "positive"
    off["role"] = "release"
    off["release_pair"] = None
    with pytest.raises(cc.ControlsCatalogError, match="cannot be a release"):
        cc.parse_controls(doc)


def test_release_must_be_captured_positive(raw):
    doc = copy.deepcopy(raw)
    rel = next(c for c in doc["controls"] if c["id"] == "7E4-AE01-3")
    rel["observed_outcome"] = "negative:E3"
    with pytest.raises(cc.ControlsCatalogError, match="captured positive"):
        cc.parse_controls(doc)


def test_cpid_specific_release_only_releases_its_own_cpid(raw):
    doc = copy.deepcopy(raw)
    cmd = next(c for c in doc["controls"] if c["id"] == "7E2-AE38-14")
    cmd["release_pair"] = "7E2-AE30-13"
    with pytest.raises(cc.ControlsCatalogError, match="only AE00 releases any CPID"):
        cc.parse_controls(doc)


def test_cpid_must_match_request():
    doc = _doc()
    doc["controls"][1]["cpid"] = "12"
    with pytest.raises(cc.ControlsCatalogError, match="does not match"):
        cc.parse_controls(doc)


def test_shipped_releases_pass_release_validation(cat):
    for e in cat:
        if e.is_release:
            assert e.observed_outcome == "positive"
            assert e.request == "AE00" or set(e.request[4:]) == {"0"}
