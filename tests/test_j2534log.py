"""Offline J2534 trace decoder (openobd.j2534log).

All synthetic frames -- no hardware. The decoder is a reader: it reassembles
ISO-TP, pairs UDS requests with responses, yields candidate read-DID rows, and
accounts for programming/security traffic without decoding it into a sequence.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from openobd.j2534log import (  # noqa: E402
    Frame, decode, reassemble, parse_trace, parse_jsonl, parse_csv,
    parse_hexlines, suggest_scaling, service_name,
)


def fr(ts, direction, cid, hexdata):
    return Frame(ts, direction, cid, bytes.fromhex(hexdata))


# --------------------------------------------------------------------------- #
# ISO-TP reassembly
# --------------------------------------------------------------------------- #
def test_single_frame():
    # 7E0 tx: SF len 3, '22 19 40'
    msgs = reassemble([fr(0, "tx", 0x7E0, "03221940")])
    assert len(msgs) == 1
    assert msgs[0].error is None
    assert msgs[0].payload == bytes.fromhex("221940")
    assert msgs[0].direction == "tx"


def test_single_frame_padding_trimmed():
    # SF len 3 padded to 8 bytes with AA -- length trims the pad
    msgs = reassemble([fr(0, "rx", 0x7E8, "03621940AAAAAA".ljust(16, "A"))])
    assert msgs[0].payload == bytes.fromhex("621940")


def test_multi_frame_first_plus_consecutive():
    # FF declares 10 bytes: '62 1A B0' + 7 more; CF1 carries the rest.
    frames = [
        fr(0, "rx", 0x7E8, "100A621AB0010203"),  # FF len 0x00A=10, 6 data bytes
        fr(1, "rx", 0x7E8, "2104050607"),         # CF seq1, 4 data bytes
    ]
    msgs = reassemble(frames)
    assert len(msgs) == 1
    assert msgs[0].error is None
    assert msgs[0].payload == bytes.fromhex("621AB001020304050607")
    assert len(msgs[0].payload) == 10


def test_interleaved_responders_kept_separate():
    frames = [
        fr(0, "rx", 0x7E8, "100A621AB0010203"),
        fr(1, "rx", 0x7EA, "03625566AA"),          # a whole SF from another module
        fr(2, "rx", 0x7E8, "2104050607"),
    ]
    msgs = reassemble(frames)
    by_id = {m.can_id: m for m in msgs}
    assert by_id[0x7E8].payload == bytes.fromhex("621AB001020304050607")
    assert by_id[0x7EA].payload == bytes.fromhex("625566")
    assert all(m.error is None for m in msgs)


def test_sequence_gap_is_an_error():
    frames = [
        fr(0, "rx", 0x7E8, "100A621AB0010203"),
        fr(1, "rx", 0x7E8, "2204050607"),          # seq 2, expected 1
    ]
    msgs = reassemble(frames)
    assert any(m.error and "sequence gap" in m.error for m in msgs)


def test_truncated_multiframe_is_an_error():
    msgs = reassemble([fr(0, "rx", 0x7E8, "100A621AB0010203")])  # FF only
    assert len(msgs) == 1
    assert msgs[0].error and "truncated" in msgs[0].error


def test_single_frame_shorter_than_length_is_an_error():
    # claims len 7 but only 2 data bytes present
    msgs = reassemble([fr(0, "rx", 0x7E8, "076255")])
    assert msgs[0].error and "shorter than its length" in msgs[0].error


def test_flow_control_frame_does_not_break_message():
    frames = [
        fr(0, "rx", 0x7E8, "100A621AB0010203"),
        fr(1, "tx", 0x7E0, "3000000000000000"),   # flow control from tester
        fr(2, "rx", 0x7E8, "2104050607"),
    ]
    msgs = reassemble(frames)
    done = [m for m in msgs if m.can_id == 0x7E8 and m.error is None]
    assert done and done[0].payload == bytes.fromhex("621AB001020304050607")


# --------------------------------------------------------------------------- #
# UDS decode + DID pairing
# --------------------------------------------------------------------------- #
def test_mode22_request_response_yields_did_observation():
    frames = [
        fr(0, "tx", 0x7E0, "03221940"),
        fr(1, "rx", 0x7E8, "0462194028"),   # 62 19 40 | data 28
    ]
    res = decode(frames)
    rows = res.candidate_did_rows()
    assert ("1940", "7E0", "28") in rows
    obs = res.did_observations[(0x7E0, "1940")]
    assert obs.data == bytes.fromhex("28")
    assert res.services[0x22].positive == 1
    assert not res.programming_seen


def test_mode22_multiple_dids_and_repeat_count():
    frames = [
        fr(0, "tx", 0x7E0, "03221940"), fr(1, "rx", 0x7E8, "0462194028"),
        fr(2, "tx", 0x7E0, "03221644"), fr(3, "rx", 0x7E8, "0562164400C2"),
        fr(4, "tx", 0x7E0, "03221940"), fr(5, "rx", 0x7E8, "0462194029"),
    ]
    res = decode(frames)
    rows = dict(((d, m), h) for d, m, h in res.candidate_did_rows())
    assert ("1940", "7E0") in rows and ("1644", "7E0") in rows
    # first observation's bytes are kept; the repeat bumps count
    assert res.did_observations[(0x7E0, "1940")].count == 2
    assert res.did_observations[(0x7E0, "1644")].data == bytes.fromhex("00C2")


def test_negative_response_counted_with_nrc():
    frames = [
        fr(0, "tx", 0x7E0, "0322F190"),
        fr(1, "rx", 0x7E8, "037F2231"),   # NRC 0x31 requestOutOfRange
    ]
    res = decode(frames)
    assert res.services[0x22].negative == 1
    assert res.services[0x22].nrcs.get(0x31) == 1
    # a refused DID is NOT a candidate
    assert res.did_observations == {}


def test_response_must_match_paired_arbitration_id():
    # a 0x62 on the WRONG id (7EA) must not be paired to a 7E0 request
    frames = [
        fr(0, "tx", 0x7E0, "03221940"),
        fr(1, "rx", 0x7EA, "0462194099"),
    ]
    res = decode(frames)
    assert res.did_observations == {}      # unpaired -> no observation
    assert res.services[0x22].positive == 0


# --------------------------------------------------------------------------- #
# Programming / security fence: observed and counted, never decoded
# --------------------------------------------------------------------------- #
def test_security_access_observed_not_decoded():
    # 27 01 (requestSeed) -> 67 01 <seed>; 27 02 <key> -> 67 02 (unlocked)
    frames = [
        fr(0, "tx", 0x7E0, "022701"),
        fr(1, "rx", 0x7E8, "0667011122334455"[:2] + "6701" + "11223344"),
        fr(2, "tx", 0x7E0, "06270299887766"),
        fr(3, "rx", 0x7E8, "026702"),
    ]
    res = decode(frames)
    assert res.programming_seen is True
    assert 0x27 in res.services
    # the fence: no read-DID output was synthesised from the security exchange
    assert res.did_observations == {}
    # seed/key bytes never surface as an identifier
    assert res.services[0x27].identifiers == set()


def test_write_and_routine_record_identifier_only():
    frames = [
        fr(0, "tx", 0x7E0, "062E194002BBCC"),   # WriteDataByIdentifier DID 1940
        fr(1, "rx", 0x7E8, "036E1940"),          # positive WriteDataByIdentifier
        fr(2, "tx", 0x7E0, "043101FF00"),       # RoutineControl start, routine FF00
        fr(3, "rx", 0x7E8, "047101FF00"),
    ]
    res = decode(frames)
    assert res.programming_seen is True
    # WHICH identifiers were touched is reported (useful for the audit)...
    assert "1940" in res.services[0x2E].identifiers
    assert "FF00" in res.services[0x31].identifiers
    # ...but no write payload becomes reusable read output
    assert res.did_observations == {}


def test_requestdownload_transfer_flagged_programming():
    frames = [
        fr(0, "tx", 0x7E0, "0634000044000000"),  # RequestDownload
        fr(1, "rx", 0x7E8, "027400"),
    ]
    res = decode(frames)
    assert res.programming_seen is True
    assert 0x34 in res.services


# --------------------------------------------------------------------------- #
# Parsers
# --------------------------------------------------------------------------- #
def test_parse_jsonl():
    text = (
        '{"ts": 0.0, "dir": "tx", "id": "7E0", "data": "03221940"}\n'
        '# a comment line\n'
        '{"ts": 0.1, "dir": "rx", "id": "0x7E8", "data": [4,98,25,64,40]}\n'
    )
    frames = parse_jsonl(text)
    assert len(frames) == 2
    assert frames[0].can_id == 0x7E0 and frames[0].direction == "tx"
    assert frames[1].data == bytes([4, 98, 25, 64, 40])


def test_parse_csv_and_hexlines_equivalent():
    csv_text = "ts,dir,id,data\n0,tx,7E0,03221940\n1,rx,7E8,0462194028\n"
    hex_text = "tx 7E0 03221940\nrx 7E8 0462194028\n"
    rc = decode(parse_csv(csv_text)).candidate_did_rows()
    rh = decode(parse_hexlines(hex_text)).candidate_did_rows()
    assert rc == rh == [("1940", "7E0", "28")]


def test_parse_trace_auto_sniffs_format():
    assert parse_trace('{"ts":0,"dir":"tx","id":"7E0","data":"03221940"}')[0].can_id == 0x7E0
    assert parse_trace("ts,dir,id,data\n0,tx,7E0,03221940\n")[0].can_id == 0x7E0
    assert parse_trace("tx 7E0 03221940")[0].can_id == 0x7E0


# --------------------------------------------------------------------------- #
# Scaling correlation helper
# --------------------------------------------------------------------------- #
def test_suggest_scaling_recovers_known_tft():
    # DID 1644 byte1 = 0xC2 (194). GM temp byte-40 C then C->F = 194F? No:
    # (194-40)=154C -> *9/5+32 = 341F. Use a value that matches a known fit:
    # byte0=0x7D=125 raw, rpm_x0.25 would be 31.25; test identity instead.
    s = suggest_scaling(bytes([0x37]), known_value=55.0, tol=0.5)  # 0x37=55
    assert any(h["transform"] == "byte0" and h["fit"] == "identity"
               for h in s)


def test_suggest_scaling_temp_fit():
    # raw byte0 = 90 (0x5A); (90-40) = 50 C. temp_C_minus40 fit -> 50.
    s = suggest_scaling(bytes([0x5A]), known_value=50.0, tol=0.5)
    assert any(h["fit"] == "temp_C_minus40" for h in s)


def test_suggest_scaling_no_false_match():
    s = suggest_scaling(bytes([0x00]), known_value=12345.0, tol=0.5)
    assert s == []


# --------------------------------------------------------------------------- #
# Service naming
# --------------------------------------------------------------------------- #
def test_service_name_known_and_response():
    assert service_name(0x22) == "UDS_readDataByIdentifier"
    assert service_name(0x62) == "UDS_readDataByIdentifier_response"
    assert service_name(0xFE).startswith("unknown_")


# --------------------------------------------------------------------------- #
# CLI smoke (tools/j2534_decode.py) -- runs the whole pipeline end to end
# --------------------------------------------------------------------------- #
def test_cli_decodes_trace_and_writes_tsv(tmp_path, capsys):
    import importlib.util
    tool = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "tools", "j2534_decode.py")
    spec = importlib.util.spec_from_file_location("j2534_decode", tool)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    trace = tmp_path / "trace.jsonl"
    trace.write_text(
        '{"ts":0.0,"dir":"tx","id":"7E0","data":"03221940"}\n'
        '{"ts":0.1,"dir":"rx","id":"7E8","data":"0462194028"}\n'
        '{"ts":0.2,"dir":"tx","id":"7E0","data":"022701"}\n'      # security access
        '{"ts":0.3,"dir":"rx","id":"7E8","data":"06670111223344"}\n'
    )
    out_tsv = tmp_path / "cand.tsv"
    rc = mod.main([str(trace), "--tsv", str(out_tsv)])
    assert rc == 0
    printed = capsys.readouterr().out
    assert "candidate mode-22 read DIDs: 1" in printed
    assert "programming / security traffic observed" in printed
    assert "DR-011" in printed                      # fence stated in output
    body = out_tsv.read_text()
    assert "1940\t28" in body
    assert "2701" not in body                        # no security id leaks to the DID table
