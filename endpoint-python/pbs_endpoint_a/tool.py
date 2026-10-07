"""Endpoint A command-line tools.

    tool vectors     run docs/test-vectors.json through Endpoint A (S7)
    tool build       build one envelope from explicit header inputs
    tool parse       parse envelopes (hex) with the rover's receive procedure
    tool crosscheck  compare Endpoint A's parse of another endpoint's logged
                     envelopes with that endpoint's own log fields (S7, T002)

Run ``endpoint-python/bin/tool <command> --help`` for options.
"""

import argparse
import json
import sys

from . import conformance
from . import envelope as E
from . import jsonlog
from .mux import MuxError, parse_container

HEADER_KEYS = ("source_id_hex", "sequence", "priority", "flags", "timestamp_us", "ttl_s", "size",
               "crc32", "header_hex", "envelope_len", "envelope_sha256")


def cmd_vectors(args):
    results, produced = conformance.run(conformance.load_vectors(args.vectors), args.keys,
                                        args.policy)
    s = conformance.summary(results)
    out = {"tool": "endpoint-python vectors", "vectors": args.vectors,
           "vectors_sha256": E.sha256_hex(open(args.vectors, "rb").read()),
           "summary": {k: s[k] for k in ("checks", "passed", "failed")},
           "failures": s["failures"], "results": results, "produced": produced}
    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            json.dump(out, fh, indent=1)
            fh.write("\n")
    print("vectors: %d checks, %d passed, %d failed" % (s["checks"], s["passed"], s["failed"]))
    for f in s["failures"]:
        print("FAIL %s %s %s" % (f["id"], f["check"], f["detail"]))
    return 0 if s["failed"] == 0 else 1


def cmd_build(args):
    env = E.build_envelope(args.source_id, args.priority, bytes.fromhex(args.payload_hex),
                           args.ttl, args.sequence, args.timestamp_us, ack=args.ack)
    print(env.hex())
    return 0


def _disposition_dict(data, d):
    rec = jsonlog.envelope_fields(data, d.header, d.frame_types)
    rec.update(result=d.result, reason=d.reason,
               commands=[c.decode("ascii", "replace") for c in d.commands]
               if d.commands is not None else None,
               suite_id="0x%04X" % d.sec.suite if d.sec else None,
               key_id=d.sec.key_id.decode("ascii", "replace") if d.sec else None,
               security_sequence=d.sec.security_sequence if d.sec else None,
               role=d.auth.role.decode("ascii", "replace") if d.auth else None)
    return rec


def cmd_parse(args):
    rx = conformance.rover_receiver(args.keys, args.policy)
    lines = sys.stdin.read().split() if args.hex == ["-"] else args.hex
    for h in lines:
        data = bytes.fromhex(h)
        now = args.now_us if args.now_us is not None else E.now_us()
        if not args.shared_state:
            rx = conformance.rover_receiver(args.keys, args.policy)
        print(json.dumps(_disposition_dict(data, rx.receive(data, now)), separators=(",", ":")))
    return 0


def _read_jsonl(path):
    with open(path, encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


def _a_fields(data):
    """Endpoint A's decode of ``data``: header fields and MUX frame types."""
    rec = jsonlog.envelope_fields(data)
    if rec["size"]:
        try:
            frames, _ = parse_container(data[E.HEADER_SIZE:E.HEADER_SIZE + rec["size"]])
            rec["frame_types"] = [f.type for f in frames]
        except MuxError:
            rec["frame_types"] = None
    elif rec["size"] == 0:
        rec["frame_types"] = []
    return rec


def cmd_crosscheck(args):
    """For every ``send`` record of the peer log that carries ``envelope_hex``:
    decode it with Endpoint A and compare with the peer's own fields. With
    ``--own-log``: for every envelope Endpoint A sent, find the peer's ``recv``
    record with the same identity and compare the bytes (SHA-256, header).

    Exit status 0 only when every peer send record was decoded (a record
    without ``envelope_hex`` counts as a failure) with no field mismatch and,
    with ``--own-log``, every envelope Endpoint A sent was delivered
    byte-identical."""
    peer = _read_jsonl(args.peer_log)
    report = {"peer_log": args.peer_log, "peer_send_records": 0, "decoded": 0,
              "decode_mismatch": [], "no_hex": 0}
    for r in peer:
        if r.get("event") != "send":
            continue
        report["peer_send_records"] += 1
        if not r.get("envelope_hex"):
            report["no_hex"] += 1
            continue
        mine = _a_fields(bytes.fromhex(r["envelope_hex"]))
        diffs = {k: (r.get(k), mine.get(k)) for k in HEADER_KEYS + ("frame_types",)
                 if r.get(k) != mine.get(k)}
        report["decoded"] += 1
        if diffs:
            report["decode_mismatch"].append({"sequence": r.get("sequence"), "diffs": diffs})
    if args.own_log:
        own = [r for r in _read_jsonl(args.own_log)
               if r.get("event") == "send" and r.get("result") == "SENT"]
        recv = {}
        for r in peer:
            if r.get("event") == "recv" and r.get("source_id_hex") is not None:
                recv.setdefault((r["source_id_hex"], r["sequence"], r["timestamp_us"]), []).append(r)
        report.update(own_sent=len(own), delivered=0, byte_identical=0, byte_mismatch=[],
                      peer_duplicates=0)
        for s in own:
            hits = recv.get((s["source_id_hex"], s["sequence"], s["timestamp_us"]), [])
            if not hits:
                continue
            report["delivered"] += 1
            report["peer_duplicates"] += len(hits) - 1
            h = hits[0]
            if (h["envelope_sha256"], h["header_hex"]) == (s["envelope_sha256"], s["header_hex"]):
                report["byte_identical"] += 1
            else:
                report["byte_mismatch"].append({"sequence": s["sequence"]})
    # Every peer send record decoded (none without envelope_hex), no field
    # differs, and with --own-log every envelope Endpoint A sent was found in
    # the peer's recv records with the same bytes.
    ok = (not report["decode_mismatch"] and report["no_hex"] == 0
          and report["decoded"] == report["peer_send_records"])
    if args.own_log:
        ok = ok and not report["byte_mismatch"] and report["delivered"] == report["own_sent"]
    report["ok"] = ok
    text = json.dumps(report, indent=1)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            fh.write(text + "\n")
    print(text)
    return 0 if ok else 1


def main(argv=None):
    ap = argparse.ArgumentParser(prog="tool", description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("vectors", help="run docs/test-vectors.json through Endpoint A")
    p.add_argument("--vectors", default=conformance.DEFAULT_VECTORS)
    p.add_argument("--keys", default=conformance.DEFAULT_KEYS)
    p.add_argument("--policy", default=conformance.DEFAULT_POLICY)
    p.add_argument("--out", help="write per-check results and produced bytes as JSON")
    p.set_defaults(fn=cmd_vectors)

    p = sub.add_parser("build", help="build one envelope (prints hex)")
    p.add_argument("--source-id", required=True)
    p.add_argument("--priority", type=int, required=True)
    p.add_argument("--sequence", type=int, required=True)
    p.add_argument("--timestamp-us", type=int, required=True)
    p.add_argument("--ttl", type=int, required=True)
    p.add_argument("--payload-hex", default="")
    p.add_argument("--ack", action="store_true")
    p.set_defaults(fn=cmd_build)

    p = sub.add_parser("parse", help="receive envelopes given as hex ('-' reads stdin)")
    p.add_argument("hex", nargs="+")
    p.add_argument("--now-us", type=int, default=None, help="receiver clock (default: host clock)")
    p.add_argument("--shared-state", action="store_true",
                   help="one replay state for all inputs (default: fresh state per input)")
    p.add_argument("--keys", default=conformance.DEFAULT_KEYS)
    p.add_argument("--policy", default=conformance.DEFAULT_POLICY)
    p.set_defaults(fn=cmd_parse)

    p = sub.add_parser("crosscheck", help="decode another endpoint's logged envelopes")
    p.add_argument("peer_log", help="JSONL log of the other endpoint")
    p.add_argument("--own-log", help="Endpoint A's JSONL log (checks byte-identical delivery)")
    p.add_argument("--out", help="write the report as JSON")
    p.set_defaults(fn=cmd_crosscheck)

    args = ap.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
