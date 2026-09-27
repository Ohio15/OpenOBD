"""Frame-structured DTC parsing (gt.reassemble_isotp / gt.parse_dtc_reply).

Reply format under test is what ObdxGt._prepare configures and confirms for
DTC reads: ATH1 + ATS0 + ATCAF1, so each flattened token is one CAN frame
'<3-hex id><PCI><data>'. No hardware involved.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from openobd.gt import parse_dtc_reply, reassemble_isotp  # noqa: E402

# Real reply from the truck, 2026-09-26 (ELM, ATH1, functional 03): first
# frame from 7E8 declaring 8 bytes, then one consecutive frame trimmed to the
# two bytes that remain. 43 03 | 03 05 | 06 06 | 07 00.
TRUCK_03 = "7E81008430303050606 7E8210700"


def test_truck_fixture_three_codes():
    r = parse_dtc_reply(TRUCK_03, "03")
    assert r["codes"] == ["P0305", "P0606", "P0700"]
    assert r["by_module"] == {"7E8": ["P0305", "P0606", "P0700"]}
    assert r["incomplete"] == {}


def test_truck_fixture_with_elm_line_breaks_and_prompt_residue():
    # _read_to_prompt flattens CR to spaces; blank lines collapse
    r = parse_dtc_reply("  7E81008430303050606   7E8210700  ", "03")
    assert r["codes"] == ["P0305", "P0606", "P0700"]


def test_0x43_inside_dtc_data_is_not_a_phantom_code():
    # 1 DTC 43 01 -> C0301. The old re-scanner restarted at the inner 43
    # and invented extra codes from the bytes after it.
    r = parse_dtc_reply("7E80443014301", "03")
    assert r["codes"] == ["C0301"]
    # multi-frame with 43 bytes spread through the data
    payload = "4304" + "4343" + "0143" + "4300" + "0043"
    frames = "7E8100A" + payload[:12] + " 7E821" + payload[12:]
    r = parse_dtc_reply(frames, "03")
    assert r["codes"] == ["C0343", "P0143", "C0300", "P0043"]
    assert r["incomplete"] == {}


def test_two_responders_interleaved():
    # ECM multi-frame and TCM single-frame, TCM's frame between ECM's FF/CF
    reply = ("7E81008430303050606 7EA0443010711 7E8210700")
    r = parse_dtc_reply(reply, "03")
    assert r["by_module"]["7E8"] == ["P0305", "P0606", "P0700"]
    assert r["by_module"]["7EA"] == ["P0711"]
    assert r["codes"] == ["P0305", "P0606", "P0700", "P0711"]
    # both multi-frame and fully interleaved
    ecm = "4303030506060700"
    tcm = "4304071107120713074A"
    reply = ("7E81008" + ecm[:12] + " 7EA100A" + tcm[:12]
             + " 7E821" + ecm[12:] + " 7EA21" + tcm[12:])
    r = parse_dtc_reply(reply, "03")
    assert r["by_module"]["7EA"] == ["P0711", "P0712", "P0713", "P074A"]
    assert r["by_module"]["7E8"] == ["P0305", "P0606", "P0700"]


def test_missing_consecutive_frame_is_incomplete_not_short():
    # 5 DTCs = 12 bytes: FF + CF1 needed; only FF arrives
    payload = "4305" + "0301" + "0302" + "0303" + "0304" + "0305"
    r = parse_dtc_reply("7E8100C" + payload[:12], "03")
    assert r["codes"] == []
    assert r["by_module"] == {}
    assert "truncated" in r["incomplete"]["7E8"]


def test_sequence_gap_is_incomplete():
    # 3 frames needed (FF 6 + CF1 7 + CF2 7 = 20 bytes); CF1 lost, CF2 seen
    payload = "4309" + "0301" * 9
    ff = "7E81014" + payload[:12]
    cf2 = "7E822" + payload[26:40]
    r = parse_dtc_reply(ff + " " + cf2, "03")
    assert r["by_module"] == {}
    assert "sequence gap" in r["incomplete"]["7E8"]


def test_gap_in_one_responder_does_not_hide_the_other():
    reply = "7E8100C430503010302 7EA0443010711"
    r = parse_dtc_reply(reply, "03")
    assert r["by_module"] == {"7EA": ["P0711"]}
    assert "7E8" in r["incomplete"]


def test_count_length_mismatch_is_incomplete():
    # count byte says 3 DTCs, message carries 2
    r = parse_dtc_reply("7E806430303010302", "03")
    assert r["by_module"] == {}
    assert "count says 3" in r["incomplete"]["7E8"]


def test_zero_dtcs_is_examined_clean():
    r = parse_dtc_reply("7E8024300", "03")
    assert r["by_module"] == {"7E8": []} and r["codes"] == []
    assert r["incomplete"] == {}


def test_padding_is_not_a_dtc():
    # SF shown with CAN padding beyond its declared length (AA/55/00)
    r = parse_dtc_reply("7E80443010300AAAA 7EA0243005555", "03")
    assert r["by_module"] == {"7E8": ["P0300"], "7EA": []}
    # a 00 00 pair inside the count is not a code either
    assert parse_dtc_reply("7E806430203000000", "03")["codes"] == ["P0300"]


def test_modes_07_and_0a():
    assert parse_dtc_reply("7E80447010301", "07")["codes"] == ["P0301"]
    assert parse_dtc_reply("7E8044A010420", "0A")["codes"] == ["P0420"]
    # a mode-03 answer is not a mode-07 answer
    assert parse_dtc_reply("7E80443010301", "07")["by_module"] == {}


def test_negative_response_is_incomplete():
    r = parse_dtc_reply("7E8037F0A11", "0A")
    assert r["by_module"] == {} and "7F 0A 11" in r["incomplete"]["7E8"]


def test_reassembly_sequence_wraps_past_f():
    n = 6 + 7 * 16                      # FF + CF 1..F then CF 0
    payload = "43" + "%02X" % ((n - 2) // 2) + "0301" * ((n - 2) // 2)
    frames = [f"7E81{n:03X}{payload[:12]}"]
    rest, sn = payload[12:], 1
    while rest:
        frames.append(f"7E82{sn & 0xF:X}{rest[:14]}")
        rest, sn = rest[14:], sn + 1
    m = reassemble_isotp(" ".join(frames))["7E8"]
    assert m["errors"] == [] and m["messages"] == [payload]


def test_non_frame_tokens_ignored():
    r = parse_dtc_reply("SEARCHING... 7E8024300 STOPPED", "03")
    assert r["by_module"] == {"7E8": []}


def test_other_mode_reply_containing_43_is_not_decoded_as_03():
    # a mode-07 reply (47 02 | 43 01 | 03 00) seen while parsing mode 03:
    # decoding from the embedded 43 would invent P0300 from the 07 data
    r = parse_dtc_reply("7E806470243010300", "03")
    assert r["codes"] == [] and r["by_module"] == {}
    assert r["incomplete"] == {}
