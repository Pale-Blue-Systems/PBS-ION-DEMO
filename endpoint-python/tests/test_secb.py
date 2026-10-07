import json
import os

import pytest

from pbs_endpoint_a import envelope as E
from pbs_endpoint_a import secb
from pbs_endpoint_a.mux import FT_AUTH, FT_COMMAND, MuxError, build_container, parse_container

T = 1767225600000000
NOW = T + 2_000_000
CONFIG = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "config")


def earth_builder():
    return E.EnvelopeBuilder("Earth-MCC")


def ed_key():
    return secb.test_signing_key("TEST-ED25519-01")


def test_rover_keys_match_vectors_and_hold_no_signing_key(vectors):
    doc = json.load(open(os.path.join(CONFIG, "rover-keys.json")))
    assert "TEST KEYS" in doc["warning"]
    tk = vectors["sec_b"]["test_keys"]
    by_id = {k["key_id"]: k for k in doc["keys"]}
    assert set(by_id) == {k for k, v in tk.items() if v["provisioned_at_rover"]}
    ed = by_id["TEST-ED25519-01"]
    assert ed["public_key_hex"] == tk["TEST-ED25519-01"]["public_key_hex"]
    assert "seed_hex" not in ed and "key_hex" not in ed
    assert by_id["TEST-HMAC-01"]["key_hex"] == tk["TEST-HMAC-01"]["key_hex"]
    # derivations from the public labels
    assert secb.derive_test_key("TEST-HMAC-01").hex() == tk["TEST-HMAC-01"]["key_hex"]
    assert secb.ed25519_public(secb.derive_test_key("TEST-ED25519-01")).hex() == ed["public_key_hex"]


def test_policy_matches_vectors(vectors):
    pol = secb.Policy.from_json(os.path.join(CONFIG, "rover-policy.json"))
    vp = vectors["sec_b"]["rover_policy"]
    assert pol.authority_id.decode() == vp["authority_id"]
    assert pol.authority_id_type == vp["authority_id_type"]
    assert pol.policy_epoch == vp["policy_epoch"]
    assert {k.decode(): list(v) for k, v in pol.roles.items()} == vp["roles"]


def test_sign_verify_roundtrip(rover_rx):
    env = secb.build_command(earth_builder(), "DRIVE 3", ed_key(), 9, timestamp_us=T)
    d = rover_rx.receive(env, NOW)
    assert (d.result, d.reason) == ("ACCEPT", "OK")
    assert d.commands == [b"DRIVE 3"]
    assert d.sec.security_sequence == 9 and d.auth.role == b"FLIGHT-OPS"
    hmac_key = secb.test_signing_key("TEST-HMAC-01")
    env = secb.build_command(earth_builder(), "SET_MODE SAFE", hmac_key, 1, timestamp_us=T)
    assert rover_rx.receive(env, NOW).reason == "OK"


def test_every_single_bit_flip_of_a_valid_command_is_rejected(messages):
    """Flip one bit at every position of CMD-01: no altered byte is accepted."""
    base = messages["CMD-01"]
    reasons = {}
    for i in range(len(base)):
        for bit in (0x01, 0x80):
            b = bytearray(base)
            b[i] ^= bit
            d = secb.Receiver(secb.KeyStore.from_json(os.path.join(CONFIG, "rover-keys.json")),
                              secb.Policy.from_json(os.path.join(CONFIG, "rover-policy.json"))
                              ).receive(bytes(b), NOW)
            assert d.result == "REJECT", (i, bit)
            reasons[d.reason] = reasons.get(d.reason, 0) + 1
    assert {"CRC", "SEC_AUTH_FAIL"} <= set(reasons)


def test_wrong_key_for_suite_is_unknown(rover_rx):
    """key_id provisioned for Ed25519 but used with the HMAC suite."""
    k = secb.SigningKey(b"TEST-ED25519-01", secb.SUITE_HMAC, secb.derive_test_key("TEST-ED25519-01"))
    env = secb.build_command(earth_builder(), "PING", k, 1, timestamp_us=T)
    assert rover_rx.receive(env, NOW).reason == "SEC_KEY_UNKNOWN"


def test_replay_window_rules():
    w = secb.ReplayWindow(64)
    k = (b"Earth-MCC".ljust(16, b"\0"), b"K")
    seq = [(5, True), (3, True), (3, False), (70, True), (6, False), (7, True), (7, False),
           (0, False), (69, True), (71, True), (8, True), (7, False)]
    for s, ok in seq:
        assert w.check(k, s) is ok, s
        if ok:
            w.mark(k, s)
    other = (b"Earth-MCC".ljust(16, b"\0"), b"OTHER")
    assert w.check(other, 3) is True


def test_replay_state_not_consumed_by_rejections(rover_rx):
    """A command rejected before R11 (tampered) does not use its security_sequence."""
    builder = earth_builder()
    good = secb.build_command(builder, "PING", ed_key(), 5, timestamp_us=T)
    bad = bytearray(good)
    bad[-1] ^= 1
    assert rover_rx.receive(bytes(bad), NOW).reason == "SEC_AUTH_FAIL"
    assert rover_rx.receive(good, NOW).reason == "OK"
    assert rover_rx.receive(good, NOW).reason == "SEC_REPLAY"


def test_authz_denied_for_unpermitted_second_command(rover_rx):
    """Every Command frame must be permitted for the role."""
    frames = [(FT_AUTH, secb.auth_value(b"PBS-DEMO-MCC", b"SCIENCE-OPS", 1)),
              (FT_COMMAND, b"CAPTURE_IMAGE NAVCAM"), (FT_COMMAND, b"DRIVE 1")]
    env, _, _ = secb.build_protected(earth_builder(), 1, 30, frames, ed_key(), 1, timestamp_us=T)
    assert rover_rx.receive(env, NOW).reason == "AUTHZ_DENIED"


def test_malformed_auth_context(rover_rx):
    frames = [(FT_AUTH, b"\x01\x03\x0c\x0a\x00\x00\x00\x01PBS"), (FT_COMMAND, b"PING")]
    env, _, _ = secb.build_protected(earth_builder(), 1, 30, frames, ed_key(), 1, timestamp_us=T)
    assert rover_rx.receive(env, NOW).reason == "AUTH_CTX_MALFORMED"
    two = [(FT_AUTH, secb.auth_value(b"PBS-DEMO-MCC", b"FLIGHT-OPS", 1))] * 2 + [(FT_COMMAND, b"PING")]
    env, _, _ = secb.build_protected(earth_builder(), 1, 30, two, ed_key(), 2, timestamp_us=T)
    assert rover_rx.receive(env, NOW).reason == "AUTH_CTX_MALFORMED"


def test_protected_data_without_command_accepted(rover_rx):
    frames = [(0x01, b"TLM SIGNED")]
    env, _, _ = secb.build_protected(earth_builder(), 2, 60, frames, ed_key(), 1, timestamp_us=T)
    d = rover_rx.receive(env, NOW)
    assert (d.result, d.reason, d.commands) == ("ACCEPT", "OK", None)


def test_two_security_frames_rejected(rover_rx, messages):
    env = messages["CMD-01"]
    frames, _ = parse_container(env[44:])
    body = build_container([(f.type, f.value) for f in frames] + [(frames[-1].type, frames[-1].value)])
    bad = E.build_envelope("Earth-MCC", 1, body, 30, 1, T)
    assert rover_rx.receive(bad, NOW).reason == "SEC_MALFORMED"


def test_mux_errors():
    with pytest.raises(MuxError) as e:
        parse_container(b"\x02\x00")
    assert e.value.reason == "MUX_VERSION"
    for bad in (b"\x01", b"\x01\x01\x01\x00", b"\x01\x01\x01\x00\x05ab", b"\x01\x02\x01\x00\x01a"):
        with pytest.raises(MuxError) as e:
            parse_container(bad)
        assert e.value.reason == "MUX_MALFORMED"
    frames, pad = parse_container(b"\x01\x01\x01\x00\x01a\xff\xff")
    assert [(f.type, f.value) for f in frames] == [(1, b"a")] and pad == 2
