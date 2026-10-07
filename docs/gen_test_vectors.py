#!/usr/bin/env python3
"""Generate docs/test-vectors.json for the PBS ION demo.

Inputs and rules: docs/SPEC-DIGEST.md (PBS v1.5.0 plus the demo profile).

* PBS-ENV-01 envelopes are built with PBS_LINK 0.1.4 (PBSLink.send with a
  fixed clock) and compared byte for byte with an independent struct/zlib
  build of the PBS-ENV-01 Section 4 layout.
* PBS-MUX-01, PBS-AUTH-01 and PBS-SEC-B-01 structures are built here from the
  specification text and the demo profile (SPEC-DIGEST.md Sections 4-6),
  because PBS_LINK 0.1.4 implements none of them. HMAC-SHA-256 comes from the
  Python standard library, Ed25519 from the 'cryptography' package.
* Expected receiver results come from Receiver.receive() below, which implements
  the receive procedure of SPEC-DIGEST.md Section 6.6. Envelope-level cases are
  also passed to PBS_LINK's parser and its outcome is recorded next to the
  expected result.

Usage:
    PYTHONPATH=<path to PBS_LINK 0.1.4> python3 docs/gen_test_vectors.py [out]

The default output is docs/test-vectors.json next to this script. The script
exits non-zero if any self-check fails.
"""

import hashlib
import hmac
import json
import os
import platform
import struct
import subprocess
import sys
import zlib
from fractions import Fraction

import PBS_LINK
from PBS_LINK import PBSLink, PBSError

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives import serialization
import cryptography

# ---------------------------------------------------------------------------
# Constants (SPEC-DIGEST.md)
# ---------------------------------------------------------------------------

T = 1767225600000000              # PBS-ENV-01 Section 13.2 Timestamp (2026-01-01T00:00:00Z)
SEC = 1_000_000                   # microseconds per second
HEADER_SIZE = 44
MAX_PAYLOAD = 65536               # demo maximum payload size (A9)
DTN_EPOCH_OFFSET_MS = 946_684_800_000
ION_NO_EXPIRY_S = 2_147_483_647   # INT_MAX: ION 4.2.0 bp_send() lifespan is int seconds
LIFETIME_MARGIN_US = 1_000_000    # demo latency margin for the creation-time bound

MUX_VERSION = 0x01
FT_TELEMETRY, FT_COMMAND, FT_STATUS, FT_EVENT = 0x01, 0x02, 0x03, 0x04
FT_SVC, FT_AUTH, FT_PNT, FT_SEC = 0x07, 0x08, 0x09, 0x0A

SUITE_HMAC = 0xFF01
SUITE_ED25519 = 0xFF02
SUITE_AUTH_LEN = {SUITE_HMAC: 32, SUITE_ED25519: 64}
SEC_FLAG_AUTH = 0x01
REPLAY_WINDOW = 64

ROVER = "Rover-Alpha"
EARTH = "Earth-MCC"
AUTHORITY = b"PBS-DEMO-MCC"
AUTHORITY_TYPE = 3                # PBS-AUTH-01 Section 4: mission authority identifier
POLICY_EPOCH = 1
ROLE_PERMS = {
    b"FLIGHT-OPS": ["PING", "SET_HEATER", "DRIVE", "CAPTURE_IMAGE", "SET_MODE"],
    b"SCIENCE-OPS": ["CAPTURE_IMAGE"],
}


def h(b):
    return b.hex()


def listing(text):
    """Hex string from a PBS-ENV-01 header listing (offset column dropped)."""
    return "".join("".join(line.split()[1:]) for line in text.strip().splitlines())


def test_key(label):
    """32 bytes derived from a public label. TEST ONLY."""
    return hashlib.sha256(
        ("PBS-ION-DEMO TEST KEY " + label + " (NOT FOR OPERATIONAL USE)").encode()
    ).digest()


KEYS = {
    # key_id: (suite, secret, bound Source ID, bound authority)
    b"TEST-HMAC-01": (SUITE_HMAC, test_key("TEST-HMAC-01"), EARTH, AUTHORITY),
    b"TEST-ED25519-01": (SUITE_ED25519, test_key("TEST-ED25519-01"), EARTH, AUTHORITY),
}
ROGUE = {
    b"TEST-HMAC-ROGUE": (SUITE_HMAC, test_key("TEST-HMAC-ROGUE")),
    b"TEST-ED25519-ROGUE": (SUITE_ED25519, test_key("TEST-ED25519-ROGUE")),
}


def secret_label(secret):
    for kid, val in list(KEYS.items()) + list(ROGUE.items()):
        if val[1] == secret:
            return kid.decode()
    raise ValueError("unknown secret")


def ed_pub(seed):
    return Ed25519PrivateKey.from_private_bytes(seed).public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw)


def sign(suite, secret, msg):
    if suite == SUITE_HMAC:
        return hmac.new(secret, msg, hashlib.sha256).digest()
    if suite == SUITE_ED25519:
        return Ed25519PrivateKey.from_private_bytes(secret).sign(msg)
    raise ValueError(suite)


def verify(suite, secret, msg, tag):
    if suite == SUITE_HMAC:
        return hmac.compare_digest(hmac.new(secret, msg, hashlib.sha256).digest(), tag)
    if suite == SUITE_ED25519:
        from cryptography.exceptions import InvalidSignature
        try:
            Ed25519PrivateKey.from_private_bytes(secret).public_key().verify(tag, msg)
            return True
        except InvalidSignature:
            return False
    raise ValueError(suite)


# ---------------------------------------------------------------------------
# Envelope builders
# ---------------------------------------------------------------------------

def link_build(source_id, priority, payload, ttl, sequence, ack, ts_us):
    """Build with PBS_LINK 0.1.4 using a fixed clock (exact microseconds)."""
    link = PBSLink(device_id=source_id, max_payload_size=MAX_PAYLOAD,
                   clock_source=lambda: Fraction(ts_us, SEC))
    link.sequence = (sequence - 1) % 65536
    return link.send(priority, payload, ttl=ttl, require_ack=ack)


def raw_header(src16, priority, flags, sequence, ts_us, size, ttl, magic=0x10,
               res3=0, res67=0, crc=None):
    """Independent PBS-ENV-01 Section 4 / 13.1 build (crc=None: compute)."""
    hdr = bytearray(HEADER_SIZE)
    hdr[0], hdr[1], hdr[2], hdr[3] = magic, priority, flags, res3
    hdr[4:6] = sequence.to_bytes(2, "big")
    hdr[6:8] = res67.to_bytes(2, "big")
    hdr[8:24] = src16
    hdr[24:32] = ts_us.to_bytes(8, "big")
    hdr[32:36] = size.to_bytes(4, "big")
    hdr[36:40] = ttl.to_bytes(4, "big")
    if crc is None:
        crc = zlib.crc32(bytes(hdr)) & 0xFFFFFFFF   # bytes 0x28-0x2B are zero here
    hdr[40:44] = crc.to_bytes(4, "big")
    return bytes(hdr)


def src16(source_id):
    b = source_id.encode("utf-8")
    assert len(b) <= 16, "demo senders reject Source IDs longer than 16 bytes (A4)"
    return b.ljust(16, b"\0")


def recrc(env):
    """Return env with the header CRC32 recomputed (attacker or test tool)."""
    hdr = bytearray(env[:HEADER_SIZE])
    hdr[40:44] = b"\0\0\0\0"
    crc = zlib.crc32(bytes(hdr)) & 0xFFFFFFFF
    hdr[40:44] = crc.to_bytes(4, "big")
    return bytes(hdr) + env[HEADER_SIZE:]


def mux(frames, count=None, padding=b""):
    out = bytearray([MUX_VERSION, len(frames) if count is None else count])
    for t, v in frames:
        out += bytes([t]) + len(v).to_bytes(2, "big") + v
    return bytes(out) + padding


def auth_value(authority=AUTHORITY, role=b"FLIGHT-OPS", epoch=POLICY_EPOCH,
               id_type=AUTHORITY_TYPE, version=1):
    return (bytes([version, id_type, len(authority), len(role)])
            + epoch.to_bytes(4, "big") + authority + role)


def sec_meta(suite, key_id, secseq, auth_len, flags=SEC_FLAG_AUTH, nonce=b"", version=1):
    return (bytes([version]) + suite.to_bytes(2, "big") + bytes([flags, len(key_id), len(nonce)])
            + auth_len.to_bytes(2, "big") + secseq.to_bytes(8, "big") + key_id + nonce)


def parse_header(env):
    (magic, prio, flags, seq, src, ts, size, ttl, crc) = struct.unpack(
        ">B B B x H xx 16s Q I I I", env[:HEADER_SIZE])
    return dict(magic=magic, priority=prio, flags=flags, sequence=seq,
                source_id_hex=h(src), timestamp_us=ts, size=size, ttl=ttl,
                crc32="0x%08X" % crc)


# ---------------------------------------------------------------------------
# Reference receiver (SPEC-DIGEST.md Section 6.6)
# ---------------------------------------------------------------------------

class Receiver:
    def __init__(self):
        self.replay = {}   # (source16, key_id) -> [highest, set of accepted within window]

    def _replay_ok(self, key, s):
        hi, seen = self.replay.get(key, (0, set()))
        if s == 0:
            return False
        if s > hi:
            return True
        if s > hi - REPLAY_WINDOW and s not in seen:
            return True
        return False

    def _replay_mark(self, key, s):
        hi, seen = self.replay.get(key, (0, set()))
        seen = set(seen)
        seen.add(s)
        hi = max(hi, s)
        seen = {x for x in seen if x > hi - REPLAY_WINDOW}
        self.replay[key] = (hi, seen)

    def receive(self, env, now_us):
        r = {}
        # R1-R5: PBS-ENV-01 Section 14 order
        if len(env) < HEADER_SIZE:
            return dict(result="REJECT", reason="LENGTH")
        if env[0] != 0x10:
            return dict(result="REJECT", reason="MAGIC")
        hdr = bytearray(env[:HEADER_SIZE])
        got = int.from_bytes(hdr[40:44], "big")
        hdr[40:44] = b"\0\0\0\0"
        if (zlib.crc32(bytes(hdr)) & 0xFFFFFFFF) != got:
            return dict(result="REJECT", reason="CRC")
        f = parse_header(env)
        if f["priority"] > 4:
            return dict(result="REJECT", reason="PRIORITY")
        if f["ttl"] > 0 and now_us - f["timestamp_us"] > f["ttl"] * SEC:
            return dict(result="REJECT", reason="TTL_EXPIRED")
        # R6: payload extraction
        if f["size"] > MAX_PAYLOAD or len(env) != HEADER_SIZE + f["size"]:
            return dict(result="REJECT", reason="LENGTH")
        if f["size"] == 0:
            return dict(result="ACCEPT", reason="OK", frames=[])
        p = env[HEADER_SIZE:]
        # R7: MUX container structure
        if len(p) < 2:
            return dict(result="REJECT", reason="MUX_MALFORMED")
        if p[0] != MUX_VERSION:
            return dict(result="REJECT", reason="MUX_VERSION")
        count, off, frames = p[1], 2, []
        for _ in range(count):
            if off + 3 > len(p):
                return dict(result="REJECT", reason="MUX_MALFORMED")
            t = p[off]
            ln = int.from_bytes(p[off + 1:off + 3], "big")
            if off + 3 + ln > len(p):
                return dict(result="REJECT", reason="MUX_MALFORMED")
            frames.append((t, off, p[off + 3:off + 3 + ln]))
            off += 3 + ln
        padding = len(p) - off
        types = [t for t, _, _ in frames]
        has_cmd = FT_COMMAND in types
        secs = [fr for fr in frames if fr[0] == FT_SEC]
        # R8: unprotected data
        if not secs and not has_cmd:
            return dict(result="ACCEPT", reason="OK",
                        frames=[[t, h(v)] for t, _, v in frames],
                        skipped_frame_types=[t for t in types if t not in (
                            FT_TELEMETRY, FT_STATUS, FT_EVENT)],
                        padding=padding)
        if has_cmd and not secs:
            return dict(result="REJECT", reason="SEC_MISSING")
        # R9: SEC frame position and structure
        if len(secs) != 1 or frames[-1][0] != FT_SEC or padding != 0:
            return dict(result="REJECT", reason="SEC_MALFORMED")
        v = frames[-1][2]
        if len(v) < 16 or v[0] != 1:
            return dict(result="REJECT", reason="SEC_MALFORMED")
        suite = int.from_bytes(v[1:3], "big")
        sflags, kl, nl = v[3], v[4], v[5]
        al = int.from_bytes(v[6:8], "big")
        secseq = int.from_bytes(v[8:16], "big")
        if len(v) != 16 + kl + nl + al:
            return dict(result="REJECT", reason="SEC_MALFORMED")
        if suite not in SUITE_AUTH_LEN:
            return dict(result="REJECT", reason="SEC_SUITE_UNKNOWN")
        if sflags != SEC_FLAG_AUTH or nl != 0 or al != SUITE_AUTH_LEN[suite] or kl == 0:
            return dict(result="REJECT", reason="SEC_MALFORMED")
        key_id = bytes(v[16:16 + kl])
        if key_id not in KEYS or KEYS[key_id][0] != suite:
            return dict(result="REJECT", reason="SEC_KEY_UNKNOWN")
        # R10: authentication over envelope[0 : len - auth_len]
        _, secret, bound_src, bound_auth = KEYS[key_id]
        if not verify(suite, secret, bytes(env[:len(env) - al]), bytes(env[len(env) - al:])):
            return dict(result="REJECT", reason="SEC_AUTH_FAIL")
        # R11: replay
        rkey = (env[8:24], key_id)
        if not self._replay_ok(rkey, secseq):
            return dict(result="REJECT", reason="SEC_REPLAY")
        self._replay_mark(rkey, secseq)
        if not has_cmd:
            return dict(result="ACCEPT", reason="OK", frames=[[t, h(v)] for t, _, v in frames[:-1]])
        # R12: authority context
        auths = [fr for fr in frames if fr[0] == FT_AUTH]
        if not auths:
            return dict(result="REJECT", reason="AUTH_CTX_MISSING")
        if len(auths) > 1:
            return dict(result="REJECT", reason="AUTH_CTX_MALFORMED")
        a = auths[0][2]
        if len(a) < 8 or a[0] != 1 or len(a) != 8 + a[2] + a[3]:
            return dict(result="REJECT", reason="AUTH_CTX_MALFORMED")
        id_type, epoch = a[1], int.from_bytes(a[4:8], "big")
        authority, role = bytes(a[8:8 + a[2]]), bytes(a[8 + a[2]:])
        # R13: authorization
        src = env[8:24].split(b"\0", 1)[0].decode("utf-8", "replace")
        if (src != bound_src or authority != bound_auth or id_type != AUTHORITY_TYPE
                or epoch != POLICY_EPOCH or role not in ROLE_PERMS):
            return dict(result="REJECT", reason="AUTHZ_DENIED")
        cmds = [fr[2] for fr in frames if fr[0] == FT_COMMAND]
        for c in cmds:
            op = bytes(c).split(b" ", 1)[0].decode("ascii", "replace")
            if op not in ROLE_PERMS[role]:
                return dict(result="REJECT", reason="AUTHZ_DENIED")
        return dict(result="ACCEPT", reason="OK", commands=[bytes(c).decode("ascii") for c in cmds],
                    security_sequence=secseq, key_id=key_id.decode())


def pbs_link_parse(env, now_us):
    """Record what PBS_LINK 0.1.4 parse() does with the bytes (envelope level only)."""
    try:
        e = PBSLink(device_id="parser", max_payload_size=MAX_PAYLOAD).parse(
            env, check_ttl=True, current_time=Fraction(now_us, SEC))
        return "parsed (size=%d, payload bytes returned=%d)" % (e.size, len(e.payload))
    except PBSError as ex:
        return "%s: %s" % (type(ex).__name__, ex)


# ---------------------------------------------------------------------------
# Vector construction
# ---------------------------------------------------------------------------

failures = []


def check(cond, msg):
    if not cond:
        failures.append(msg)


def envelope_vector(vid, purpose, refs, source_id, priority, payload, ttl, sequence, ack,
                    ts_us, payload_desc, frames=None, expect_crc=None, expect_header=None):
    env = link_build(source_id, priority, payload, ttl, sequence, ack, ts_us)
    ref = raw_header(src16(source_id), priority, 0x01 if ack else 0x00, sequence, ts_us,
                     len(payload), ttl) + payload
    check(env == ref, vid + ": PBS_LINK output differs from independent build")
    crc = "0x%08X" % int.from_bytes(env[40:44], "big")
    if expect_crc:
        check(crc == expect_crc, "%s: CRC %s != spec %s" % (vid, crc, expect_crc))
    if expect_header:
        check(h(env[:44]) == expect_header, vid + ": header differs from spec listing")
    parsed = PBS_LINK.parse_envelope(env)
    check(parsed.payload == payload and parsed.raw_header == env[:44], vid + ": parse mismatch")
    v = dict(id=vid, purpose=purpose, spec_refs=refs,
             inputs=dict(source_id=source_id, source_id_hex=h(src16(source_id)),
                         priority=priority, flags=0x01 if ack else 0x00, sequence=sequence,
                         timestamp_us=ts_us, ttl_s=ttl, payload_hex=h(payload),
                         payload_description=payload_desc),
             size=len(payload), header_hex=h(env[:44]), crc32=crc, envelope_hex=h(env),
             built_with="PBS_LINK 0.1.4 PBSLink.send (fixed clock); equal to independent struct/zlib build")
    if frames is not None:
        v["mux_frames"] = [dict(type="0x%02X" % t, value_hex=h(val)) for t, val in frames]
    return v, env


def main():
    out_path = sys.argv[1] if len(sys.argv) > 1 else os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "test-vectors.json")

    envs = {}
    vectors = []

    def add(*a, **k):
        v, env = envelope_vector(*a, **k)
        vectors.append(v)
        envs[v["id"]] = env
        return env

    # --- PBS-ENV-01 Sections 13.2 and 12.5 -------------------------------
    add("ENV-01", "PBS-ENV-01 Section 13.2 test vector (header-only)",
        ["PBS-ENV-01 13.2"], "Rover-A", 0, b"", 30, 1, True, T, "empty (Size 0)",
        expect_crc="0x588721ED",
        expect_header=listing("""
0x00  10 00 01 00   00 01 00 00   52 6f 76 65   72 2d 41 00
0x10  00 00 00 00   00 00 00 00   00 06 47 48   46 20 40 00
0x20  00 00 00 00   00 00 00 1e   58 87 21 ed"""))
    add("ENV-02", "PBS-ENV-01 Section 12.5 TTL 0 vector",
        ["PBS-ENV-01 12.5", "PBS-ENV-01 12.1"], "Rover-A", 0, b"", 0, 1, True, T, "empty (Size 0)",
        expect_crc="0x8757080E",
        expect_header=listing("""
0x00  10 00 01 00   00 01 00 00   52 6f 76 65   72 2d 41 00
0x10  00 00 00 00   00 00 00 00   00 06 47 48   46 20 40 00
0x20  00 00 00 00   00 00 00 00   87 57 08 0e"""))
    add("ENV-03", "PBS-ENV-01 Section 12.5 counter-example: the Section 13.2 header with TTL 10, "
        "as a v1.4.1 decrementing relay would have forwarded it. A valid envelope that expires at T+10 s.",
        ["PBS-ENV-01 12.5", "PBS-ENV-01 12.3"], "Rover-A", 0, b"", 10, 1, True, T, "empty (Size 0)",
        expect_crc="0xCDE710AF")

    # --- One envelope per priority (rover telemetry, MUX telemetry frame) --
    tlm = [
        (0, b"EVT MOTOR_STALL WHEEL=3", 0, "CRITICAL alert, TTL 0 (PBS-ENV-01 12.4)"),
        (1, b"TLM POWER BATT=27.9V SOC=81%", 60, "HIGH telemetry, TTL 60"),
        (2, b"TLM THERMAL CPU=-12.5C", 60, "NORMAL telemetry, TTL 60"),
        (3, b"TLM ENV DUST=0.02", 3600, "LOW science, TTL 3600"),
    ]
    seq = 1
    for prio, text, ttl, desc in tlm:
        fr = [(FT_TELEMETRY, text)]
        add("ENV-%02d" % (4 + prio), "Rover telemetry, priority %d (%s)" % (prio, desc),
            ["PBS-ENV-01 4", "PBS-PRIO-01 4", "PBS-MUX-01 4"], ROVER, prio, mux(fr), ttl, seq,
            False, T + seq * 100_000, "MUX container, one Telemetry frame (0x01), ASCII", frames=fr)
        seq += 1
    bulk = bytes(range(256))
    fr = [(FT_TELEMETRY, bulk)]
    add("ENV-08", "Rover bulk data, priority 4 (BULK), TTL 86400, 256 binary bytes",
        ["PBS-ENV-01 4", "PBS-PRIO-01 4", "PBS-MUX-01 4"], ROVER, 4, mux(fr), 86400, seq, False,
        T + seq * 100_000, "MUX container, one Telemetry frame (0x01) with bytes 0x00..0xFF", frames=fr)
    seq += 1

    # --- Source ID length edge cases ---------------------------------------
    fr = [(FT_STATUS, b"STATUS OK")]
    add("ENV-09", "Source ID of exactly 16 ASCII bytes (no null terminator)",
        ["PBS-ENV-01 9"], "LUNAR-ROVER-0016", 2, mux(fr), 60, 1, False, T,
        "MUX container, one Status frame (0x03)", frames=fr)
    add("ENV-10", "Source ID of exactly 16 UTF-8 bytes with multi-byte characters",
        ["PBS-ENV-01 9"], "Rover-Ü-Mond-Ä", 2, mux(fr), 60, 1, False, T,
        "MUX container, one Status frame (0x03)", frames=fr)

    # --- Sequence rollover, TTL max, microsecond timestamp, ACK flag -------
    add("ENV-11", "Sequence 65535 (last value before rollover), header-only heartbeat",
        ["PBS-ENV-01 8"], ROVER, 3, b"", 60, 65535, False, T, "empty (Size 0)")
    add("ENV-12", "Sequence 0 (after rollover from 65535), header-only heartbeat",
        ["PBS-ENV-01 8", "PBS-CONFORMANCE-01 7"], ROVER, 3, b"", 60, 0, False, T + 1, "empty (Size 0)")
    add("ENV-13", "Header-only, priority 4, TTL 0, Flags 0x00",
        ["PBS-ENV-01 11", "PBS-ENV-01 12.1"], ROVER, 4, b"", 0, 7, False, T, "empty (Size 0)")
    add("ENV-14", "TTL 4294967295 (maximum u32), Timestamp with a microsecond fraction",
        ["PBS-ENV-01 4", "PBS-ENV-01 10"], ROVER, 2, b"", 4294967295, 8, False, T + 123456,
        "empty (Size 0)")
    fr = [(FT_TELEMETRY, b"TLM PING-REPLY")]
    add("ENV-15", "Earth-side envelope with ACK_REQ set (Flags 0x01), non-empty payload",
        ["PBS-ENV-01 7"], EARTH, 2, mux(fr), 60, 1, True, T + 2 * SEC,
        "MUX container, one Telemetry frame (0x01), ASCII", frames=fr)

    # ---------------------------------------------------------------------
    # Envelope-level receiver cases
    # ---------------------------------------------------------------------
    rcases = []

    def rcase(cid, desc, refs, data, now_us, expect, derived_from=None):
        got = Receiver().receive(data, now_us)
        check(got["result"] == expect[0] and got["reason"] == expect[1],
              "%s: oracle %s/%s, expected %s/%s" % (cid, got["result"], got["reason"], *expect))
        rcases.append(dict(id=cid, description=desc, spec_refs=refs, derived_from=derived_from,
                           input_hex=h(data), now_us=now_us,
                           expected=dict(result=expect[0], reason=expect[1]),
                           pbs_link_0_1_4_parse=pbs_link_parse(data, now_us)))

    e1 = envs["ENV-01"]
    rcase("RX-01", "ENV-01 received at T+5 s (C01 Section 8 statement 1, receipt)",
          ["PBS-ENV-01 12.2", "PBS-CONFORMANCE-01 8"], e1, T + 5 * SEC, ("ACCEPT", "OK"), "ENV-01")
    rcase("RX-02", "ENV-01 at T+30 s: age equals TTL, not expired",
          ["PBS-ENV-01 12.2", "PBS-ENV-01 12.5", "PBS-CONFORMANCE-01 8"], e1, T + 30 * SEC,
          ("ACCEPT", "OK"), "ENV-01")
    rcase("RX-03", "ENV-01 at T+30.000001 s: age exceeds TTL by 1 us (integer-microsecond rule, A6)",
          ["PBS-ENV-01 12.2"], e1, T + 30 * SEC + 1, ("REJECT", "TTL_EXPIRED"), "ENV-01")
    rcase("RX-04", "ENV-01 at T+31 s: expired",
          ["PBS-ENV-01 12.2", "PBS-ENV-01 12.5", "PBS-CONFORMANCE-01 8"], e1, T + 31 * SEC,
          ("REJECT", "TTL_EXPIRED"), "ENV-01")
    rcase("RX-05", "ENV-02 (TTL 0) at current_time 1798761600 (365 days after Timestamp)",
          ["PBS-ENV-01 12.1", "PBS-ENV-01 12.5", "PBS-CONFORMANCE-01 8"], envs["ENV-02"],
          1798761600 * SEC, ("ACCEPT", "OK"), "ENV-02")
    rcase("RX-06", "ENV-03 (TTL 10) at T+26 s: expired",
          ["PBS-ENV-01 12.5"], envs["ENV-03"], T + 26 * SEC, ("REJECT", "TTL_EXPIRED"), "ENV-03")
    bad = bytearray(e1); bad[0] = 0x11
    rcase("RX-07", "Magic 0x11 with CRC32 recomputed over the modified header",
          ["PBS-ENV-01 5", "PBS-ENV-01 14"], recrc(bytes(bad)), T + 5 * SEC, ("REJECT", "MAGIC"), "ENV-01")
    bad = bytearray(e1); bad[1] = 0x01
    rcase("RX-08", "Priority byte changed 0 -> 1, CRC32 not updated",
          ["PBS-ENV-01 13", "PBS-PRIO-01 10"], bytes(bad), T + 5 * SEC, ("REJECT", "CRC"), "ENV-01")
    bad = bytearray(e1); bad[40:44] = (0x019507AC).to_bytes(4, "big")
    rcase("RX-09", "CRC32 computed over bytes 0x00-0x27 only (0x019507AC)",
          ["PBS-ENV-01 13.2"], bytes(bad), T + 5 * SEC, ("REJECT", "CRC"), "ENV-01")
    bad = bytearray(e1); bad[1] = 5
    rcase("RX-10", "Priority 5 with valid CRC32",
          ["PBS-ENV-01 6", "PBS-PRIO-01 5.1"], recrc(bytes(bad)), T + 5 * SEC, ("REJECT", "PRIORITY"), "ENV-01")
    bad = bytearray(e1); bad[1] = 255
    rcase("RX-11", "Priority 255 with valid CRC32",
          ["PBS-ENV-01 6", "PBS-PRIO-01 5.1"], recrc(bytes(bad)), T + 5 * SEC, ("REJECT", "PRIORITY"), "ENV-01")
    bad = bytearray(e1); bad[3] = 0xA5; bad[6:8] = b"\x5a\x5a"
    rcase("RX-12", "Reserved bytes 0x03 = 0xA5 and 0x06-0x07 = 0x5A5A with valid CRC32: accepted, reserved fields ignored",
          ["PBS-ENV-01 4", "PBS-ENV-01 18"], recrc(bytes(bad)), T + 5 * SEC, ("ACCEPT", "OK"), "ENV-01")
    bad = bytearray(e1); bad[2] = 0xFF
    rcase("RX-13", "Flags 0xFF (reserved bits 1-7 set) with valid CRC32: accepted, ACK_REQ = 1, reserved bits ignored",
          ["PBS-ENV-01 7", "PBS-ENV-01 18"], recrc(bytes(bad)), T + 5 * SEC, ("ACCEPT", "OK"), "ENV-01")
    e4 = envs["ENV-04"]
    rcase("RX-14", "ENV-04 with its last payload byte missing (fewer than Size bytes)",
          ["PBS-ENV-01 11", "PBS-ENV-01 14"], e4[:-1], T + 1 * SEC, ("REJECT", "LENGTH"), "ENV-04")
    rcase("RX-15", "ENV-04 with one byte appended after the payload (bundle payload longer than 44 + Size, A8)",
          ["PBS-DTN-MAP-01 6.2"], e4 + b"\x00", T + 1 * SEC, ("REJECT", "LENGTH"), "ENV-04")
    rcase("RX-16", "43 bytes (shorter than the header)", ["PBS-ENV-01 4"], e1[:43], T + 5 * SEC,
          ("REJECT", "LENGTH"), "ENV-01")

    def tlm_env(payload, sequence=20):
        return link_build(ROVER, 2, payload, 60, sequence, False, T)

    rcase("RX-17", "MUX container with mux_version 0x02",
          ["PBS-MUX-01 4.1"], tlm_env(b"\x02\x01\x01\x00\x02OK"), T + 1 * SEC, ("REJECT", "MUX_VERSION"))
    rcase("RX-18", "MUX frame length runs past the end of the payload",
          ["PBS-MUX-01 4.2", "PBS-MUX-01 9"], tlm_env(b"\x01\x01\x01\x00\x09OK"), T + 1 * SEC,
          ("REJECT", "MUX_MALFORMED"))
    rcase("RX-19", "frame_count 2 with one frame present",
          ["PBS-MUX-01 4.2"], tlm_env(mux([(FT_TELEMETRY, b"OK")], count=2)), T + 1 * SEC,
          ("REJECT", "MUX_MALFORMED"))
    rcase("RX-20", "Two padding bytes after the last frame of an unprotected container: accepted, padding ignored",
          ["PBS-MUX-01 4.3"], tlm_env(mux([(FT_TELEMETRY, b"OK")], padding=b"\xff\xff")), T + 1 * SEC,
          ("ACCEPT", "OK"))
    rcase("RX-21", "Unknown frame type 0x55 before a Telemetry frame: accepted, 0x55 skipped",
          ["PBS-MUX-01 4.2", "PBS-MUX-01 10", "PBS-CONFORMANCE-02 T014"],
          tlm_env(mux([(0x55, b"\x00\x01\x02"), (FT_TELEMETRY, b"OK")])), T + 1 * SEC, ("ACCEPT", "OK"))
    rcase("RX-22", "MUX container with frame_count 0 (2-byte payload): accepted, no frames",
          ["PBS-MUX-01 4.1"], tlm_env(mux([])), T + 1 * SEC, ("ACCEPT", "OK"))

    # ---------------------------------------------------------------------
    # PBS-SEC-B-01 protected commands
    # ---------------------------------------------------------------------
    sec_msgs = {}
    cmd_seq = [0]

    def command(mid, desc, cmd=b"DRIVE 10", suite=SUITE_ED25519, key_id=b"TEST-ED25519-01",
                signer_secret=None, secseq=1, role=b"FLIGHT-OPS", epoch=POLICY_EPOCH,
                source=EARTH, priority=1, ttl=30, ts=T, with_auth=True, with_sec=True,
                extra_frames_before=(), auth_len=None, refs=None):
        cmd_seq[0] += 1
        frames = []
        if with_auth:
            frames.append((FT_AUTH, auth_value(role=role, epoch=epoch)))
        frames.extend(extra_frames_before)
        frames.append((FT_COMMAND, cmd))
        al = auth_len if auth_len is not None else SUITE_AUTH_LEN.get(suite, 32)
        if signer_secret is None:
            signer_secret = KEYS[key_id][1] if key_id in KEYS else ROGUE[key_id][1]
        if with_sec:
            meta = sec_meta(suite, key_id, secseq, al)
            body = mux(frames, count=len(frames) + 1) + bytes([FT_SEC]) + (len(meta) + al).to_bytes(2, "big") + meta
            hdr = link_build(source, priority, body + b"\0" * al, ttl, cmd_seq[0], False, ts)[:HEADER_SIZE]
            auth_input = hdr + body
            tag = sign(suite if suite in SUITE_AUTH_LEN else SUITE_HMAC, signer_secret, auth_input)[:al]
            env = auth_input + tag
            check(env == link_build(source, priority, body + tag, ttl, cmd_seq[0], False, ts),
                  mid + ": PBS_LINK header differs")
            sec_value = meta + tag
        else:
            body = mux(frames)
            env = link_build(source, priority, body, ttl, cmd_seq[0], False, ts)
            auth_input, tag, sec_value = b"", b"", None
        all_frames = frames + ([(FT_SEC, sec_value)] if with_sec else [])
        m = dict(id=mid, description=desc, spec_refs=refs or [],
                 inputs=dict(source_id=source, source_id_hex=h(src16(source)), priority=priority,
                             flags=0, sequence=cmd_seq[0], timestamp_us=ts, ttl_s=ttl,
                             frames_before_security_frame=[dict(type="0x%02X" % t, value_hex=h(v))
                                                           for t, v in frames]),
                 envelope_hex=h(env), header_hex=h(env[:44]), crc32="0x%08X" % int.from_bytes(env[40:44], "big"),
                 size=len(env) - 44,
                 mux_frames=[dict(type="0x%02X" % t, value_hex=h(v)) for t, v in all_frames])
        if with_sec:
            m["security"] = dict(version=1, suite_id="0x%04X" % suite, flags="0x%02X" % SEC_FLAG_AUTH,
                                 key_id=key_id.decode(), nonce_hex="", auth_len=al,
                                 security_sequence=secseq,
                                 signing_key=secret_label(signer_secret),
                                 auth_input_len=len(auth_input), auth_input_hex=h(auth_input),
                                 auth_data_hex=h(tag))
        sec_msgs[mid] = (m, env)
        return env

    def tamper(mid, desc, base, fn, refs):
        m0, env0 = sec_msgs[base]
        env = fn(bytearray(env0))
        m = dict(id=mid, description=desc, spec_refs=refs, derived_from=base, envelope_hex=h(env),
                 header_hex=h(env[:44]), crc32="0x%08X" % int.from_bytes(env[40:44], "big"))
        sec_msgs[mid] = (m, bytes(env))
        return bytes(env)

    R_AUTH = ["PBS-SEC-B-01 4", "PBS-SEC-B-01 5", "PBS-SEC-B-01 6 (REQ-001, REQ-002)"]
    command("CMD-01", "Valid command, suite 0xFF02 (Ed25519), role FLIGHT-OPS, DRIVE 10", secseq=1,
            refs=R_AUTH + ["PBS-AUTH-01 2", "PBS-SEC-B-01 6 (REQ-005, REQ-006)"])
    command("CMD-02", "Valid command, suite 0xFF01 (HMAC-SHA-256), SET_HEATER ON", cmd=b"SET_HEATER ON",
            suite=SUITE_HMAC, key_id=b"TEST-HMAC-01", secseq=1, refs=R_AUTH + ["PBS-AUTH-01 2"])
    off_cmd = 44 + 2 + 3 + len(auth_value()) + 3   # start of the COMMAND frame value in CMD-01
    tamper("CMD-03", "CMD-01 with command byte changed (DRIVE 10 -> DRIVE 90); header and CRC32 unchanged",
           "CMD-01", lambda b: (b.__setitem__(off_cmd + 6, ord("9")), b)[1], R_AUTH + ["PBS-CONFORMANCE-02 T006"])
    tamper("CMD-04", "CMD-01 with Priority raised 1 -> 0 and CRC32 recomputed",
           "CMD-01", lambda b: bytearray(recrc(bytes((b.__setitem__(1, 0), b)[1]))),
           R_AUTH + ["PBS-SEC-B-01 6 (REQ-004)", "PBS-PRIO-01 10"])
    tamper("CMD-05", "CMD-01 with TTL raised 30 -> 3600 and CRC32 recomputed",
           "CMD-01", lambda b: bytearray(recrc(bytes((b.__setitem__(slice(36, 40), (3600).to_bytes(4, "big")), b)[1]))),
           R_AUTH + ["PBS-SEC-B-01 5"])
    sseq_off = len(sec_msgs["CMD-01"][1]) - 64 - len(b"TEST-ED25519-01") - 8
    tamper("CMD-06", "CMD-01 with security_sequence changed 1 -> 2 (replay evasion attempt)",
           "CMD-01", lambda b: (b.__setitem__(slice(sseq_off, sseq_off + 8), (2).to_bytes(8, "big")), b)[1],
           R_AUTH + ["PBS-SEC-B-01 6 (REQ-003)"])
    command("CMD-07", "Signed with a key not provisioned at the rover (TEST-ED25519-ROGUE seed) but labelled key_id TEST-ED25519-01",
            signer_secret=ROGUE[b"TEST-ED25519-ROGUE"][1], secseq=1, refs=R_AUTH)
    command("CMD-08", "Suite 0xFF01, signed with TEST-HMAC-ROGUE key but labelled key_id TEST-HMAC-01",
            suite=SUITE_HMAC, key_id=b"TEST-HMAC-01", signer_secret=ROGUE[b"TEST-HMAC-ROGUE"][1],
            secseq=1, refs=R_AUTH)
    command("CMD-09", "key_id TEST-ED25519-ROGUE, not provisioned at the rover",
            key_id=b"TEST-ED25519-ROGUE", secseq=1, refs=["PBS-SEC-B-01 7"])
    command("CMD-10", "Role SCIENCE-OPS issuing DRIVE 10 (operation not permitted for the role)",
            role=b"SCIENCE-OPS", secseq=1, refs=["PBS-AUTH-01 3 (REQ-003, REQ-006)", "PBS-CONFORMANCE-02 T008"])
    command("CMD-11", "Role SCIENCE-OPS issuing CAPTURE_IMAGE NAVCAM (permitted)", cmd=b"CAPTURE_IMAGE NAVCAM",
            role=b"SCIENCE-OPS", secseq=1, refs=["PBS-AUTH-01 3", "PBS-CONFORMANCE-02 T008"])
    command("CMD-12", "policy_epoch 2 (rover policy epoch is 1)", epoch=2, secseq=1,
            refs=["PBS-AUTH-01 3 (REQ-005)", "PBS-CONFORMANCE-02 T008"])
    command("CMD-13", "Command with Authority Context and no Security frame", with_sec=False,
            refs=["PBS-SEC-B-01 6 (REQ-005)"])
    command("CMD-14", "Signed command with no Authority Context frame", with_auth=False, secseq=1,
            refs=["PBS-SEC-B-01 6 (REQ-005)", "PBS-AUTH-01 3 (REQ-002)"])
    command("CMD-15", "suite_id 0xFF7F (not defined in the demo profile), HMAC tag",
            suite=0xFF7F, key_id=b"TEST-HMAC-01", auth_len=32, secseq=1, refs=["PBS-SEC-B-01 3"])
    command("CMD-16", "Valid command carrying an application-defined frame 0x80 before the command frame",
            extra_frames_before=((0x80, b"\xde\xad\xbe\xef"),), cmd=b"PING", secseq=1,
            refs=R_AUTH + ["PBS-MUX-01 5.9", "PBS-CONFORMANCE-02 T014"])
    command("CMD-17", "Validly signed with the Earth key, Source ID Rover-Alpha (key bound to Earth-MCC)",
            source=ROVER, secseq=1, refs=["PBS-AUTH-01 3 (REQ-003)"])
    tamper("CMD-18", "CMD-01 with two 0x00 padding bytes appended and Size and CRC32 updated (Security frame no longer ends the payload)",
           "CMD-01", lambda b: bytearray(recrc(bytes((b.__setitem__(slice(32, 36), (len(b) - 44 + 2).to_bytes(4, "big")), b)[1]) + b"\0\0")),
           ["PBS-SEC-B-01 5", "PBS-MUX-01 4.3"])
    # Replay-window sequence (Ed25519)
    for s in (5, 3, 70, 7, 6):
        command("CMD-W%02d" % s, "Valid command, security_sequence %d (replay-window case)" % s,
                cmd=b"PING", secseq=s, refs=["PBS-SEC-B-01 4", "PBS-SEC-B-01 6 (REQ-003)"])
    command("CMD-W00", "Valid signature, security_sequence 0", cmd=b"PING", secseq=0,
            refs=["PBS-SEC-B-01 6 (REQ-003)"])

    NOW = T + 2 * SEC
    case_defs = [
        ("SB-01", "Valid Ed25519 command", [("CMD-01", NOW, "ACCEPT", "OK")]),
        ("SB-02", "Valid HMAC-SHA-256 command", [("CMD-02", NOW, "ACCEPT", "OK")]),
        ("SB-03", "Tampered command payload", [("CMD-03", NOW, "REJECT", "SEC_AUTH_FAIL")]),
        ("SB-04", "Tampered priority", [("CMD-04", NOW, "REJECT", "SEC_AUTH_FAIL")]),
        ("SB-05", "Tampered TTL", [("CMD-05", NOW, "REJECT", "SEC_AUTH_FAIL")]),
        ("SB-06", "Tampered security_sequence", [("CMD-06", NOW, "REJECT", "SEC_AUTH_FAIL")]),
        ("SB-07", "Wrong key (Ed25519)", [("CMD-07", NOW, "REJECT", "SEC_AUTH_FAIL")]),
        ("SB-08", "Wrong key (HMAC)", [("CMD-08", NOW, "REJECT", "SEC_AUTH_FAIL")]),
        ("SB-09", "Unknown key_id", [("CMD-09", NOW, "REJECT", "SEC_KEY_UNKNOWN")]),
        ("SB-10", "Replay of an accepted command", [("CMD-01", NOW, "ACCEPT", "OK"),
                                                     ("CMD-01", NOW + SEC, "REJECT", "SEC_REPLAY")]),
        ("SB-11", "Expired command: CMD-01 at T+31 s is rejected at the TTL step, before authentication, and does not consume its security_sequence; the same bytes at T+30 s are accepted",
         [("CMD-01", T + 31 * SEC, "REJECT", "TTL_EXPIRED"), ("CMD-01", T + 30 * SEC, "ACCEPT", "OK")]),
        ("SB-12", "Unauthorized operation for role", [("CMD-10", NOW, "REJECT", "AUTHZ_DENIED")]),
        ("SB-13", "Authorized operation for role SCIENCE-OPS", [("CMD-11", NOW, "ACCEPT", "OK")]),
        ("SB-14", "Wrong policy epoch", [("CMD-12", NOW, "REJECT", "AUTHZ_DENIED")]),
        ("SB-15", "Unprotected command", [("CMD-13", NOW, "REJECT", "SEC_MISSING")]),
        ("SB-16", "Protected command without Authority Context", [("CMD-14", NOW, "REJECT", "AUTH_CTX_MISSING")]),
        ("SB-17", "Unknown suite", [("CMD-15", NOW, "REJECT", "SEC_SUITE_UNKNOWN")]),
        ("SB-18", "Unknown application-defined frame inside a protected command", [("CMD-16", NOW, "ACCEPT", "OK")]),
        ("SB-19", "Source ID not bound to the key", [("CMD-17", NOW, "REJECT", "AUTHZ_DENIED")]),
        ("SB-20", "Padding after the Security frame", [("CMD-18", NOW, "REJECT", "SEC_MALFORMED")]),
        ("SB-21", "Replay window of 64 (deliveries in order, one receiver state)",
         [("CMD-W05", NOW, "ACCEPT", "OK"), ("CMD-W03", NOW, "ACCEPT", "OK"),
          ("CMD-W03", NOW, "REJECT", "SEC_REPLAY"), ("CMD-W70", NOW, "ACCEPT", "OK"),
          ("CMD-W06", NOW, "REJECT", "SEC_REPLAY"), ("CMD-W07", NOW, "ACCEPT", "OK"),
          ("CMD-W07", NOW, "REJECT", "SEC_REPLAY"), ("CMD-W00", NOW, "REJECT", "SEC_REPLAY")]),
    ]
    sb_cases = []
    for cid, desc, deliveries in case_defs:
        rcv = Receiver()
        steps = []
        for mid, now, er, ereason in deliveries:
            got = rcv.receive(sec_msgs[mid][1], now)
            check(got["result"] == er and got["reason"] == ereason,
                  "%s/%s: oracle %s/%s expected %s/%s" % (cid, mid, got["result"], got["reason"], er, ereason))
            steps.append(dict(message=mid, now_us=now, expected=dict(result=er, reason=ereason)))
        sb_cases.append(dict(id=cid, description=desc, deliveries=steps))

    # ---------------------------------------------------------------------
    # Lifetime cases (PBS-DTN-MAP-01 6.1, 6.1.1; demo ION profile)
    # ---------------------------------------------------------------------
    def bound_ms(ts_us, ttl, c_us):
        return (ttl * SEC - max(0, c_us - ts_us)) // 1000

    def ion_lifespan(ts_us, ttl, now_us):
        if ttl == 0:
            return ION_NO_EXPIRY_S, "NO_EXPIRY"
        b = bound_ms(ts_us, ttl, now_us + LIFETIME_MARGIN_US)
        if b < 1:
            return None, "GW_EXPIRED"
        s = min(b // 1000, ION_NO_EXPIRY_S)
        if s < 1:
            return None, "GW_LIFETIME_UNDER_1S"
        return s, "OK"

    life = []
    for lid, ts, ttl, now, desc in [
        ("LT-01", T, 30, T, "TTL 30, handed to the BPA at its Timestamp"),
        ("LT-02", T, 30, T + 5 * SEC, "TTL 30, handed to the BPA 5 s after Timestamp"),
        ("LT-03", T, 30, T - 2 * SEC, "TTL 30, gateway clock 2 s behind Timestamp (max(0, ...) term)"),
        ("LT-04", T, 30, T + 28 * SEC + 999_000, "TTL 30, 1.001 s remaining at the clock reading"),
        ("LT-05", T, 30, T + 29 * SEC, "TTL 30, 1 s remaining at the clock reading, 0 ms remaining at c_us (clock + margin)"),
        ("LT-06", T, 30, T + 30 * SEC - 1000, "TTL 30, 1 ms remaining at the clock reading"),
        ("LT-07", T, 30, T + 31 * SEC, "TTL 30, expired at the clock reading"),
        ("LT-08", T, 0, T + 400 * 86400 * SEC, "TTL 0, any age: no-expiry lifetime"),
        ("LT-09", T, 4294967295, T, "TTL 4294967295: bound exceeds INT_MAX s, clamped to the no-expiry lifetime"),
        ("LT-10", T, 5, T + 2 * SEC, "TTL 5 (short-TTL S5 envelope) handed over 2 s after Timestamp"),
    ]:
        spec_c = now + LIFETIME_MARGIN_US
        sp = None if ttl == 0 else bound_ms(ts, ttl, spec_c)
        s, outcome = ion_lifespan(ts, ttl, now)
        life.append(dict(id=lid, description=desc, timestamp_us=ts, ttl_s=ttl, clock_us=now,
                         c_us=spec_c if ttl else None,
                         spec_bound_ms=sp,
                         spec_bound_formula="floor((TTL*1000000 - max(0, c_us - Timestamp)) / 1000)",
                         ion_lifespan_s=s, outcome=outcome))
    # The pure spec bound example from PBS-DTN-MAP-01 6.1 with creation_time_ms.
    ctime_ms = (T // 1000) - DTN_EPOCH_OFFSET_MS
    check((ctime_ms + DTN_EPOCH_OFFSET_MS) * 1000 == T, "creation time conversion")
    expect_life = {"LT-01": 29, "LT-02": 24, "LT-03": 30, "LT-04": None, "LT-05": None,
                   "LT-06": None, "LT-07": None, "LT-08": ION_NO_EXPIRY_S, "LT-09": ION_NO_EXPIRY_S,
                   "LT-10": 2}
    for l in life:
        check(l["ion_lifespan_s"] == expect_life[l["id"]],
              "%s: lifespan %r expected %r" % (l["id"], l["ion_lifespan_s"], expect_life[l["id"]]))

    # ---------------------------------------------------------------------
    # Assemble
    # ---------------------------------------------------------------------
    def git_head(path):
        try:
            return subprocess.run(["git", "-C", path, "rev-parse", "HEAD"], capture_output=True,
                                  text=True, check=True).stdout.strip()
        except Exception:
            return None

    link_dir = os.path.dirname(os.path.dirname(os.path.abspath(PBS_LINK.__file__)))
    crc_check = zlib.crc32(b"123456789") & 0xFFFFFFFF
    check(crc_check == 0xCBF43926, "CRC-32 check value")

    doc = dict(
        title="PBS ION demo test vectors",
        description=("Envelope, MUX, PBS-AUTH-01 and PBS-SEC-B-01 vectors for the PBS ION demo. "
                     "Rules: docs/SPEC-DIGEST.md. All byte strings are lowercase hex. "
                     "Times are Unix epoch microseconds unless a field name says otherwise. "
                     "Keys in sec_b.test_keys are TEST KEYS, derived from public labels, "
                     "and must not be used outside this demo."),
        generator="docs/gen_test_vectors.py",
        sources=dict(
            pbs="PBS-PROTOCOL-OPEN main, PBS v1.5.0, commit f18957b8fd96eb7955ffa8b55081f8f5fd352ce1",
            pbs_link=dict(version=PBS_LINK.__version__, path_commit=git_head(link_dir)),
            python=platform.python_version(), cryptography=cryptography.__version__),
        constants=dict(header_size=HEADER_SIZE, max_payload_size=MAX_PAYLOAD, mux_version=MUX_VERSION,
                       replay_window=REPLAY_WINDOW, ion_no_expiry_lifetime_s=ION_NO_EXPIRY_S,
                       lifetime_margin_us=LIFETIME_MARGIN_US,
                       dtn_epoch_offset_ms=DTN_EPOCH_OFFSET_MS,
                       creation_time_example=dict(creation_time_ms=ctime_ms, c_us=T)),
        crc32_check=dict(input_ascii="123456789", crc32="0x%08X" % crc_check,
                         parameters="CRC-32/ISO-HDLC (zlib): poly 0x04C11DB7 (reflected 0xEDB88320), "
                                    "init 0xFFFFFFFF, refin true, refout true, xorout 0xFFFFFFFF"),
        envelopes=vectors,
        receiver_cases=rcases,
        ttl_note=("Expired iff TTL > 0 and now_us - Timestamp > TTL * 1000000 (SPEC-DIGEST.md 2.4). "
                  "RX-01..RX-06 cover PBS-ENV-01 12.5 and PBS-CONFORMANCE-01 Section 8."),
        lifetime_cases=life,
        cos_map=[dict(priority=p, name=n, dtn_class=c, ion_class_of_service=ion, ion_ordinal=0)
                 for p, n, c, ion in [(0, "CRITICAL", "Expedited", 2), (1, "HIGH", "Expedited", 2),
                                       (2, "NORMAL", "Normal", 1), (3, "LOW", "Bulk", 0),
                                       (4, "BULK", "Bulk", 0)]],
        sec_b=dict(
            suites={
                "0xFF01": dict(name="PBS-DEMO-TEST-HMAC-SHA256", algorithm="HMAC-SHA-256 (RFC 2104, FIPS 180-4)",
                               key_bytes=32, auth_len=32, nonce_len=0, flags="0x01",
                               registered=False),
                "0xFF02": dict(name="PBS-DEMO-TEST-ED25519", algorithm="Ed25519 (RFC 8032, pure, no context)",
                               key_bytes=32, auth_len=64, nonce_len=0, flags="0x01",
                               registered=False)},
            canonical_input="envelope[0 : 44 + Size - auth_len] (all 44 header bytes and the payload up to auth_data)",
            test_keys={
                "TEST-HMAC-01": dict(suite="0xFF01", key_hex=h(KEYS[b"TEST-HMAC-01"][1]),
                                     derivation='SHA-256("PBS-ION-DEMO TEST KEY TEST-HMAC-01 (NOT FOR OPERATIONAL USE)")',
                                     provisioned_at_rover=True, bound_source_id=EARTH,
                                     bound_authority=AUTHORITY.decode()),
                "TEST-ED25519-01": dict(suite="0xFF02", seed_hex=h(KEYS[b"TEST-ED25519-01"][1]),
                                        public_key_hex=h(ed_pub(KEYS[b"TEST-ED25519-01"][1])),
                                        derivation='seed = SHA-256("PBS-ION-DEMO TEST KEY TEST-ED25519-01 (NOT FOR OPERATIONAL USE)")',
                                        provisioned_at_rover=True, bound_source_id=EARTH,
                                        bound_authority=AUTHORITY.decode()),
                "TEST-HMAC-ROGUE": dict(suite="0xFF01", key_hex=h(ROGUE[b"TEST-HMAC-ROGUE"][1]),
                                        derivation='SHA-256("PBS-ION-DEMO TEST KEY TEST-HMAC-ROGUE (NOT FOR OPERATIONAL USE)")',
                                        provisioned_at_rover=False),
                "TEST-ED25519-ROGUE": dict(suite="0xFF02", seed_hex=h(ROGUE[b"TEST-ED25519-ROGUE"][1]),
                                           public_key_hex=h(ed_pub(ROGUE[b"TEST-ED25519-ROGUE"][1])),
                                           derivation='seed = SHA-256("PBS-ION-DEMO TEST KEY TEST-ED25519-ROGUE (NOT FOR OPERATIONAL USE)")',
                                           provisioned_at_rover=False)},
            rover_policy=dict(authority_id_type=AUTHORITY_TYPE, authority_id=AUTHORITY.decode(),
                              policy_epoch=POLICY_EPOCH,
                              roles={k.decode(): v for k, v in ROLE_PERMS.items()},
                              replay_window=REPLAY_WINDOW,
                              replay_state_key="(Source ID 16 raw bytes, key_id)"),
            messages=[m for m, _ in sec_msgs.values()],
            cases=sb_cases),
    )

    if failures:
        for f in failures:
            print("FAIL:", f, file=sys.stderr)
        sys.exit(1)

    with open(out_path, "w") as fh:
        json.dump(doc, fh, indent=1, ensure_ascii=False)
        fh.write("\n")
    n_sb = sum(len(c["deliveries"]) for c in sb_cases)
    print("wrote %s: %d envelopes, %d receiver cases, %d lifetime cases, %d SEC-B messages, "
          "%d SEC-B cases (%d deliveries); all self-checks passed"
          % (out_path, len(vectors), len(rcases), len(life), len(sec_msgs), len(sb_cases), n_sb))


if __name__ == "__main__":
    main()
