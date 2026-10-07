"""Run docs/test-vectors.json through Endpoint A's code.

Used by the unit tests and by ``tool vectors`` (scenario S7). Every check
produces one result: ``{"id", "check", "ok", "detail"}``. The bytes Endpoint
A produces from each vector's inputs are returned in ``produced`` (vector id
-> hex) so they can be compared with Endpoint B's output.
"""

import json
import os
import zlib

from . import envelope as E
from . import secb
from .mux import FT_SEC, build_container, parse_container

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DEFAULT_VECTORS = os.path.join(REPO_ROOT, "docs", "test-vectors.json")
CONFIG_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "config")
DEFAULT_KEYS = os.path.join(CONFIG_DIR, "rover-keys.json")
DEFAULT_POLICY = os.path.join(CONFIG_DIR, "rover-policy.json")


def load_vectors(path=DEFAULT_VECTORS):
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def rover_receiver(keys=DEFAULT_KEYS, policy=DEFAULT_POLICY):
    return secb.rover_receiver(keys, policy)


def _frames(spec):
    return [(int(f["type"], 16), bytes.fromhex(f["value_hex"])) for f in spec]


def build_message(m):
    """Build a sec_b message from its ``inputs`` (None for derived messages)."""
    inp = m.get("inputs")
    if inp is None:
        return None, None, None
    builder = E.EnvelopeBuilder(inp["source_id"])
    frames = _frames(inp["frames_before_security_frame"])
    common = dict(ack=bool(inp["flags"] & 1), timestamp_us=inp["timestamp_us"],
                  sequence=inp["sequence"])
    sec = m.get("security")
    if sec is None:
        env = builder.build(inp["priority"], build_container(frames), inp["ttl_s"], **common)
        return env, None, None
    key = secb.test_signing_key(sec["key_id"], label=sec["signing_key"])
    return secb.build_protected(builder, inp["priority"], inp["ttl_s"], frames, key,
                                sec["security_sequence"], suite=int(sec["suite_id"], 16),
                                auth_len=sec["auth_len"], **common)


def run(vectors=None, keys=DEFAULT_KEYS, policy=DEFAULT_POLICY):
    """Return ``(results, produced)``."""
    v = vectors if vectors is not None else load_vectors()
    results, produced = [], {}

    def check(vid, what, ok, detail=""):
        results.append({"id": vid, "check": what, "ok": bool(ok), "detail": detail})

    # CRC-32 check value. PBS_LINK.calculate_crc32 accepts only 44-byte
    # headers; it calls zlib.crc32, which is checked here directly (A10).
    got = zlib.crc32(v["crc32_check"]["input_ascii"].encode("ascii")) & 0xFFFFFFFF
    check("CRC32-CHECK", "crc32", "0x%08X" % got == v["crc32_check"]["crc32"], "0x%08X" % got)

    # Envelopes: build from inputs, parse back
    for vec in v["envelopes"]:
        inp, vid = vec["inputs"], vec["id"]
        want = bytes.fromhex(vec["envelope_hex"])
        check(vid, "source_id_field",
              E.source_id_bytes(inp["source_id"]).hex() == inp["source_id_hex"])
        env = E.build_envelope(inp["source_id"], inp["priority"], bytes.fromhex(inp["payload_hex"]),
                               inp["ttl_s"], inp["sequence"], inp["timestamp_us"],
                               ack=bool(inp["flags"] & 1))
        produced[vid] = env.hex()
        check(vid, "build", env == want, "" if env == want else env.hex())
        reason, hdr, payload = E.check_envelope(want, inp["timestamp_us"])
        fields_ok = (reason == "OK" and hdr.priority == inp["priority"]
                     and hdr.flags == inp["flags"] and hdr.sequence == inp["sequence"]
                     and hdr.source_id_raw.hex() == inp["source_id_hex"]
                     and hdr.timestamp_us == inp["timestamp_us"] and hdr.ttl_s == inp["ttl_s"]
                     and hdr.size == vec["size"] and "0x%08X" % hdr.crc32 == vec["crc32"]
                     and payload.hex() == inp["payload_hex"])
        check(vid, "parse", fields_ok, reason)
        if "mux_frames" in vec:
            frames, _ = parse_container(payload)
            check(vid, "mux", [(f.type, f.value) for f in frames] == _frames(vec["mux_frames"]))

    # Receiver cases (fresh receiver each)
    for rc in v["receiver_cases"]:
        d = rover_receiver(keys, policy).receive(bytes.fromhex(rc["input_hex"]), rc["now_us"])
        exp = rc["expected"]
        check(rc["id"], "receive", (d.result, d.reason) == (exp["result"], exp["reason"]),
              "%s/%s" % (d.result, d.reason))

    # Lifetime cases (PBS-DTN-MAP-01 6.1 / 6.1.1 for ION)
    for lt in v["lifetime_cases"]:
        s, outcome = E.ion_lifespan(lt["timestamp_us"], lt["ttl_s"], lt["clock_us"])
        ok = (s, outcome) == (lt["ion_lifespan_s"], lt["outcome"])
        if lt["ttl_s"]:
            ok = ok and E.lifetime_bound_ms(lt["timestamp_us"], lt["ttl_s"],
                                            lt["c_us"]) == lt["spec_bound_ms"]
        check(lt["id"], "lifetime", ok, "%s/%s" % (s, outcome))

    # Class of service (PBS-DTN-MAP-01 6.3)
    for c in v["cos_map"]:
        p = c["priority"]
        check("COS-P%d" % p, "cos", E.class_of_service(p) == c["ion_class_of_service"]
              and E.DTN_CLASS[p] == c["dtn_class"] and E.ION_ORDINAL == c["ion_ordinal"]
              and E.PRIORITY_NAMES[p] == c["name"])

    # SEC-B messages: rebuild from inputs, check auth_input/auth_data, parse
    msgs = {m["id"]: m for m in v["sec_b"]["messages"]}
    for mid, m in msgs.items():
        want = bytes.fromhex(m["envelope_hex"])
        env, auth_input, tag = build_message(m)
        if env is not None:
            produced[mid] = env.hex()
            check(mid, "build", env == want, "" if env == want else env.hex())
            sec = m.get("security")
            if sec is not None:
                check(mid, "auth_input", auth_input.hex() == sec["auth_input_hex"])
                check(mid, "auth_data", tag.hex() == sec["auth_data_hex"])
        hdr = E.decode_header(want)
        check(mid, "header", hdr.raw.hex() == m["header_hex"]
              and "0x%08X" % hdr.crc32 == m["crc32"])
        if "mux_frames" in m:
            frames, padding = parse_container(want[E.HEADER_SIZE:])
            check(mid, "mux", padding == 0 and [(f.type, f.value) for f in frames]
                  == _frames(m["mux_frames"]))
            if frames and frames[-1].type == FT_SEC and m.get("security"):
                sf = secb.parse_sec_value(frames[-1].value)
                sec = m["security"]
                check(mid, "security_frame", sf is not None
                      and sf.suite == int(sec["suite_id"], 16)
                      and sf.key_id.decode() == sec["key_id"] and sf.auth_len == sec["auth_len"]
                      and sf.security_sequence == sec["security_sequence"]
                      and sf.auth_data.hex() == sec["auth_data_hex"])

    # SEC-B cases: ordered deliveries, fresh rover state per case
    for case in v["sec_b"]["cases"]:
        rx = rover_receiver(keys, policy)
        got = []
        for dl in case["deliveries"]:
            d = rx.receive(bytes.fromhex(msgs[dl["message"]]["envelope_hex"]), dl["now_us"])
            exp = dl["expected"]
            got.append((d.result, d.reason) == (exp["result"], exp["reason"]))
            check("%s/%s" % (case["id"], dl["message"]), "receive",
                  got[-1], "%s/%s" % (d.result, d.reason))
    return results, produced


def summary(results):
    failed = [r for r in results if not r["ok"]]
    return {"checks": len(results), "passed": len(results) - len(failed),
            "failed": len(failed), "failures": failed}
