#!/usr/bin/env python3
"""Decode a captured J2534 pass-thru trace into OpenOBD-usable knowledge.

    python tools/j2534_decode.py <trace-file> [--format auto|jsonl|csv|hexlines]
                                 [--tsv candidate_dids.tsv]

Input is a trace from the Ron-run logging proxy (tools/j2534_trace_proxy) or any
tool, in JSONL / CSV / loose-hex form (see openobd/j2534log for the shapes).

Output:
  * a candidate mode-22 DID table (DID, module, raw response bytes) -- these are
    OBSERVED read DIDs; each still needs correlation against a known physical
    value before it goes into gt.DID_TABLE. With --tsv, the DID+bytes columns are
    written in the same shape as tools/ecm_dids.tsv so the existing correlation
    tools (analyze_dids.py) pick them up.
  * a per-service summary (requests, positive, negative + NRCs).
  * an account of any programming / security traffic: that it occurred and which
    identifiers it touched -- never the payloads or an ordered sequence. This
    decoder is a reader; reconstructing the flash/unlock path is out of scope
    (DR-011).
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from openobd.j2534log import (  # noqa: E402
    decode, parse_trace, service_name, _READ_SERVICES, _PROGRAMMING_SERVICES,
    _NRC_NAMES,
)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("trace", help="captured trace file")
    ap.add_argument("--format", default="auto",
                    choices=["auto", "jsonl", "csv", "hexlines"])
    ap.add_argument("--tsv", help="write candidate DIDs to this TSV "
                                  "(ecm_dids.tsv-compatible: did<TAB>hexdata)")
    args = ap.parse_args(argv)

    with open(args.trace, encoding="utf-8", errors="replace") as fh:
        frames = parse_trace(fh.read(), args.format)
    res = decode(frames)

    print(f"frames parsed : {len(frames)}")
    print(f"ISO-TP msgs   : {res.messages}")
    if res.errors:
        print(f"reassembly errs: {len(res.errors)} "
              f"(e.g. {res.errors[0][1]})")

    rows = res.candidate_did_rows()
    print(f"\ncandidate mode-22 read DIDs: {len(rows)} "
          f"(OBSERVED -- correlate before adding to gt.DID_TABLE)")
    print(f"  {'DID':<6} {'module':<7} response bytes")
    for did, mod, hexdata in rows:
        print(f"  {did:<6} {mod:<7} {hexdata}")

    print("\nservices on the bus:")
    for sid, s in res.services.items():
        tag = ("read" if sid in _READ_SERVICES else
               "PROGRAMMING/SECURITY" if sid in _PROGRAMMING_SERVICES else "other")
        line = (f"  0x{sid:02X} {s.name:<34} "
                f"req={s.requests} ok={s.positive} nak={s.negative} [{tag}]")
        print(line)
        if s.negative and s.nrcs:
            for nrc, c in sorted(s.nrcs.items()):
                print(f"        NRC 0x{nrc:02X} {_NRC_NAMES.get(nrc,'?')} x{c}")

    prog = {sid: s for sid, s in res.services.items()
            if sid in _PROGRAMMING_SERVICES}
    if prog:
        print("\nprogramming / security traffic observed (identifiers only, "
              "no payloads decoded):")
        for sid, s in prog.items():
            ids = ", ".join(sorted(s.identifiers)) if s.identifiers else "(none)"
            print(f"  0x{sid:02X} {s.name}: {s.requests}x  identifiers: {ids}")
        print("  NOTE: the flash/unlock sequence is deliberately NOT "
              "reconstructed here (DR-011).")

    if args.tsv:
        with open(args.tsv, "w", encoding="ascii") as out:
            out.write("# did\thexdata  (observed via J2534 capture; correlate "
                      "before trusting)\n")
            seen = set()
            for did, _mod, hexdata in rows:
                if did in seen:
                    continue
                seen.add(did)
                out.write(f"{did}\t{hexdata}\n")
        print(f"\nwrote {len(seen)} candidate DIDs -> {args.tsv}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
