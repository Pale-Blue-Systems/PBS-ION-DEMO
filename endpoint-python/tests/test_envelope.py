import struct
import zlib

import pytest

from pbs_endpoint_a import envelope as E
from pbs_endpoint_a.pbslink import PBS_LINK

T = 1767225600000000
SEC = 1_000_000


def test_header_layout_matches_independent_build():
    env = E.build_envelope("Rover-Alpha", 3, b"\x01\x00", 60, 513, T + 7, ack=True)
    hdr = bytearray(44)
    hdr[0:4] = bytes([0x10, 3, 1, 0])
    hdr[4:6] = (513).to_bytes(2, "big")
    hdr[8:24] = b"Rover-Alpha".ljust(16, b"\0")
    hdr[24:32] = (T + 7).to_bytes(8, "big")
    hdr[32:36] = (2).to_bytes(4, "big")
    hdr[36:40] = (60).to_bytes(4, "big")
    hdr[40:44] = (zlib.crc32(bytes(hdr)) & 0xFFFFFFFF).to_bytes(4, "big")
    assert env == bytes(hdr) + b"\x01\x00"
    assert struct.calcsize(E.HEADER_FORMAT) == 44


def test_source_id_over_16_bytes_refused():
    with pytest.raises(E.SourceIdError):
        E.EnvelopeBuilder("Rover-Alpha-Lunar-South")        # 23 ASCII bytes
    with pytest.raises(E.SourceIdError):
        E.source_id_bytes("Rover-Ü-Mond-Äx")                 # 17 UTF-8 bytes
    assert E.source_id_bytes("Rover-Ü-Mond-Ä") == bytes.fromhex("526f7665722dc39c2d4d6f6e642dc384")


def test_sequence_starts_at_1_and_wraps():
    b = E.EnvelopeBuilder("Rover-Alpha")
    seqs = [E.decode_header(b.build(2, b"", 60, timestamp_us=T)).sequence for _ in range(3)]
    assert seqs == [1, 2, 3]
    b = E.EnvelopeBuilder("Rover-Alpha", sequence_start=65535)
    seqs = [E.decode_header(b.build(2, b"", 60, timestamp_us=T)).sequence for _ in range(3)]
    assert seqs == [65535, 0, 1]


def test_timestamp_is_exact_microseconds():
    env = E.EnvelopeBuilder("Rover-Alpha").build(2, b"", 60, timestamp_us=T + 123456)
    assert E.decode_header(env).timestamp_us == T + 123456


@pytest.mark.parametrize("now,expired", [
    (T + 30 * SEC, False), (T + 30 * SEC + 1, True), (T - 5 * SEC, False)])
def test_expiry_integer_microseconds(now, expired):
    assert E.is_expired(T, 30, now) is expired
    assert E.is_expired(T, 0, now + 10 ** 15) is False


def test_expired_and_short_reports_ttl_first():
    """PBS-ENV-01 Section 14 orders TTL before payload extraction; PBS_LINK
    raises its length error first, Endpoint A reports TTL_EXPIRED."""
    env = E.build_envelope("Rover-Alpha", 2, b"\x01\x00", 30, 1, T)
    short = env[:-1]
    with pytest.raises(PBS_LINK.PBSValidationError):
        PBS_LINK.PBSLink("x").parse(short, check_ttl=True, current_time=(T + 31 * SEC) / SEC)
    assert E.check_envelope(short, T + 31 * SEC)[0] == "TTL_EXPIRED"
    assert E.check_envelope(short, T + 1 * SEC)[0] == "LENGTH"


def test_size_above_demo_maximum_rejected():
    payload = b"\x01\x00" + b"\0" * (65537 - 2)
    env = PBS_LINK.PBSLink("Rover-Alpha", max_payload_size=10 ** 6).send(2, payload, ttl=60)
    assert E.check_envelope(env, E.now_us())[0] == "LENGTH"


def test_trailing_byte_rejected():
    env = E.build_envelope("Rover-Alpha", 2, b"\x01\x00", 60, 1, T)
    assert E.check_envelope(env, T)[0] == "OK"
    assert E.check_envelope(env + b"\0", T)[0] == "LENGTH"


@pytest.mark.parametrize("ttl,clock_offset_us,want", [
    (0, 10 ** 12, (E.ION_NO_EXPIRY_S, "NO_EXPIRY")),
    (60, 0, (59, "OK")),
    (60, 1, (58, "OK")),               # 58.999999 s rounds down
    (3, 0, (2, "OK")),
    (2, 0, (1, "OK")),
    (2, 1, (None, "GW_LIFETIME_UNDER_1S")),
    (1, 0, (None, "GW_EXPIRED")),
    (4294967295, 0, (E.ION_NO_EXPIRY_S, "OK")),
])
def test_ion_lifespan(ttl, clock_offset_us, want):
    assert E.ion_lifespan(T, ttl, T + clock_offset_us) == want


def test_lifetime_never_exceeds_ttl_bound():
    for ttl in (3, 5, 30, 60, 3600):
        for off in range(0, ttl * SEC, SEC // 3):
            s, _ = E.ion_lifespan(T, ttl, T + off)
            if s is not None:
                c_us = T + off + E.LIFETIME_MARGIN_US
                assert s * 1000 <= E.lifetime_bound_ms(T, ttl, c_us)


def test_class_of_service_table():
    assert [E.class_of_service(p) for p in range(5)] == [2, 2, 1, 0, 0]


def test_creation_time_conversion():
    assert E.creation_ms_to_unix_us(820540800000) == T
