"""Test harness: an Earth-side stand-in used to test Endpoint A over ION.

This is NOT Endpoint B. It exists so that Endpoint A can be tested end to
end on the three ION nodes without Endpoint B, and so that a scenario can
inject fault traffic (replayed, tampered, wrongly signed or expired
commands) from any node. It signs with TEST KEYS derived from public labels
(docs/SPEC-DIGEST.md 6.3).

    harness inject --node earth --eid ipn:23.1 --dest ipn:99.1 \\
        --command "DRIVE 10" --key TEST-ED25519-01 --secseq 1 --log h.jsonl
    harness inject --node earth --eid ipn:23.1 --dest ipn:99.1 --hex <envelope hex>
    harness listen --node earth --eid ipn:23.1 --duration 20 --log h.jsonl

``inject`` opens the EID with ``bp_open_source`` (send only), so it works
while another process (Endpoint B) holds the endpoint open. Records are
written in the docs/LOG-SCHEMA.md format with ``endpoint`` = ``H``.
"""

import argparse
import sys
import threading
import time

from . import envelope as E
from . import ion, jsonlog, secb
from .mux import FT_COMMAND, FT_SEC, MuxError, parse_container
from .rover import Rover

EARTH_SOURCE_ID = "Earth-MCC"
TAMPER = ("command", "priority", "ttl", "secseq")


def recrc(env):
    """Envelope with the header CRC32 recomputed (what an attacker can do)."""
    hdr = bytearray(env[:E.HEADER_SIZE])
    hdr[40:44] = b"\0\0\0\0"
    hdr[40:44] = E.PBS_LINK.calculate_crc32(bytes(hdr)).to_bytes(4, "big")
    return bytes(hdr) + bytes(env[E.HEADER_SIZE:])


def frame_offsets(env):
    """(type, offset of the value in ``env``, value length) for each MUX frame."""
    frames, _ = parse_container(env[E.HEADER_SIZE:])
    out, off = [], E.HEADER_SIZE + 2
    for f in frames:
        out.append((f.type, off + 3, len(f.value)))
        off += 3 + len(f.value)
    return out


def tamper(env, what):
    """One of the SPEC-DIGEST tamper cases (CMD-03 to CMD-06 style)."""
    b = bytearray(env)
    offs = frame_offsets(bytes(b))
    if what == "command":            # change the last byte of the Command frame value
        _, off, ln = [o for o in offs if o[0] == FT_COMMAND][0]
        b[off + ln - 1] ^= 0x01
        return bytes(b)
    if what == "priority":           # raise priority, recompute CRC32
        b[1] = 0 if b[1] != 0 else 1
        return recrc(bytes(b))
    if what == "ttl":                # extend TTL by 3570 s, recompute CRC32
        b[36:40] = (int.from_bytes(b[36:40], "big") + 3570).to_bytes(4, "big")
        return recrc(bytes(b))
    if what == "secseq":             # security_sequence + 1
        _, off, _ = [o for o in offs if o[0] == FT_SEC][-1]
        s = int.from_bytes(b[off + 8:off + 16], "big") + 1
        b[off + 8:off + 16] = s.to_bytes(8, "big")
        return bytes(b)
    raise ValueError(what)


def describe(env, hdr):
    """Log fields of an injected envelope (frames decoded when possible)."""
    out = jsonlog.envelope_fields(env, hdr)
    if hdr is None or len(env) != E.HEADER_SIZE + hdr.size:
        return out
    try:
        frames, _ = parse_container(env[E.HEADER_SIZE:]) if hdr.size else ([], 0)
    except MuxError:
        return out
    out["frame_types"] = [f.type for f in frames]
    cmds = [f.value.decode("ascii", "replace") for f in frames if f.type == FT_COMMAND]
    out["commands"] = cmds or None
    secs = [secb.parse_sec_value(f.value) for f in frames if f.type == FT_SEC]
    if secs and secs[-1] is not None:
        out.update(suite_id=secb.suite_str(secs[-1].suite),
                   key_id=secs[-1].key_id.decode("ascii", "replace"),
                   security_sequence=secs[-1].security_sequence)
    return out


def make_command(args, builder):
    key = secb.test_signing_key(args.key, label=args.sign_with or args.key)
    ts = E.now_us() + int(args.timestamp_offset_s * E.SEC)
    env = secb.build_command(builder, args.command, key, args.secseq, role=args.role.encode(),
                             epoch=args.epoch, priority=args.priority, ttl_s=args.ttl,
                             timestamp_us=ts)
    if args.tamper:
        env = tamper(env, args.tamper)
    return env


def cmd_inject(args):
    ion.node_environment(args.node_dir or ion.default_node_dir(args.node))
    ep = ion.BpEndpoint(args.eid, send_only=True)
    try:
        builder = E.EnvelopeBuilder(args.source_id, args.sequence_start)
        for i in range(args.repeat):
            if args.hex:
                env = bytes.fromhex(args.hex)
            else:
                env = make_command(args, builder)
            hdr = E.decode_header(env) if len(env) >= E.HEADER_SIZE else None
            prio = hdr.priority if hdr and hdr.priority <= 4 else 1
            cos = E.class_of_service(prio) if args.cos is None else args.cos
            if args.lifespan is not None:
                lifespan, rule = args.lifespan, "OVERRIDE"
            else:
                lifespan, _ = E.ion_lifespan(hdr.timestamp_us, hdr.ttl_s, E.now_us())
                rule = "DTN-MAP-01-6.1"
            t = E.now_us()
            result, reason = "NOT_SENT", "GW_EXPIRED"
            if lifespan is not None:
                try:
                    ep.send(args.dest, env, lifespan, cos)
                    result, reason = "SENT", "OK"
                except ion.IonError as ex:
                    reason = "ZCO_FAILED" if isinstance(ex, ion.ZcoError) else "BP_SEND_FAILED"
            if args.log:
                log = jsonlog.JsonlLog(args.log, "H", args.eid, args.scenario)
                log.write("send", t, result=result, reason=reason, dest_eid=args.dest,
                          lifespan_s=lifespan, lifespan_rule=rule, class_of_service=cos,
                          ordinal=0, bp_send_return_us=E.now_us(), label=args.label,
                          **describe(env, hdr))
                log.close()
            print("%s %s %s" % (result, reason, env.hex()), flush=True)
            if i + 1 < args.repeat:
                time.sleep(args.interval)
    finally:
        ep.close()
    return 0


def cmd_listen(args):
    """Receive for ``--duration`` seconds and log recv records (Earth stand-in)."""
    ion.node_environment(args.node_dir or ion.default_node_dir(args.node))
    ep = ion.BpEndpoint(args.eid)
    log = jsonlog.JsonlLog(args.log, "H", args.eid, args.scenario)
    rx = secb.Receiver(secb.KeyStore())          # Earth holds no rover keys
    r = Rover(ep, log, rx, source_id=EARTH_SOURCE_ID, dest_eid="ipn:99.1")
    stop = threading.Event()
    try:
        r.start_receiving()
        print("harness: listening on %s" % args.eid, flush=True)
        r.run([], stop, args.duration)
    finally:
        r.stop_receiving()
        log.close()
        ep.close()
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(prog="harness", description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)

    def node_args(p, eid):
        p.add_argument("--node", default="earth", choices=("rover", "gateway", "earth"))
        p.add_argument("--node-dir", default=None, help="node working directory")
        p.add_argument("--eid", default=eid)
        p.add_argument("--scenario", default=None)

    p = sub.add_parser("inject", help="send one or more envelopes from a node")
    node_args(p, "ipn:23.1")
    p.add_argument("--dest", default="ipn:99.1")
    p.add_argument("--hex", help="send these envelope bytes as given")
    p.add_argument("--command", default="PING", help="command text (when --hex is absent)")
    p.add_argument("--key", default="TEST-ED25519-01", help="key_id written in the Security frame")
    p.add_argument("--sign-with", default=None,
                   help="label of the key that signs (default: --key); e.g. TEST-ED25519-ROGUE")
    p.add_argument("--secseq", type=int, default=1)
    p.add_argument("--role", default="FLIGHT-OPS")
    p.add_argument("--epoch", type=int, default=1)
    p.add_argument("--priority", type=int, default=1)
    p.add_argument("--ttl", type=int, default=30)
    p.add_argument("--timestamp-offset-s", type=float, default=0.0,
                   help="Timestamp = now + offset (negative: an older command)")
    p.add_argument("--tamper", choices=TAMPER, default=None)
    p.add_argument("--source-id", default=EARTH_SOURCE_ID)
    p.add_argument("--sequence-start", type=int, default=1)
    p.add_argument("--lifespan", type=int, default=None,
                   help="FAULT INJECTION: bundle lifetime override in seconds")
    p.add_argument("--cos", type=int, default=None, help="ION class of service override")
    p.add_argument("--repeat", type=int, default=1)
    p.add_argument("--interval", type=float, default=1.0)
    p.add_argument("--label", default=None)
    p.add_argument("--log", default=None)
    p.set_defaults(fn=cmd_inject)

    p = sub.add_parser("listen", help="receive and log for a while")
    node_args(p, "ipn:23.1")
    p.add_argument("--duration", type=float, required=True)
    p.add_argument("--log", required=True)
    p.set_defaults(fn=cmd_listen)

    args = ap.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
