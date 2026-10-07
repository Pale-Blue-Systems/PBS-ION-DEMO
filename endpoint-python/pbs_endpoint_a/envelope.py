"""PBS-ENV-01 envelopes (built and parsed with PBS_LINK 0.1.4) and the
PBS-DTN-MAP-01 bundle mapping rules used by Endpoint A.

References are to docs/SPEC-DIGEST.md (sections and A-numbered resolutions)
and to the PBS v1.5.0 specifications it cites.
"""

import hashlib
import struct
import time
from dataclasses import dataclass
from fractions import Fraction

from .pbslink import PBS_LINK

HEADER_SIZE = PBS_LINK.HEADER_SIZE          # 44 (PBS-ENV-01 Section 4)
HEADER_FORMAT = PBS_LINK.HEADER_FORMAT      # '>B B B x H xx 16s Q I I I'
MAGIC = PBS_LINK.MAGIC_BYTE                 # 0x10
MAX_PAYLOAD = 65536                         # demo maximum (A9)
SEC = 1_000_000                             # microseconds per second

DTN_EPOCH_OFFSET_MS = 946_684_800_000       # 2000-01-01T00:00:00Z in Unix ms
ION_NO_EXPIRY_S = 2_147_483_647             # no-expiry lifetime (SPEC-DIGEST 7.4, A30)
LIFETIME_MARGIN_US = 1_000_000              # send-latency margin for c_us (A29)

PRIORITY_NAMES = {0: "CRITICAL", 1: "HIGH", 2: "NORMAL", 3: "LOW", 4: "BULK"}

# PBS-DTN-MAP-01 Section 6.3 -> ION class of service (SPEC-DIGEST 7.5, A31).
ION_CLASS_OF_SERVICE = {0: 2, 1: 2, 2: 1, 3: 0, 4: 0}
ION_ORDINAL = 0
DTN_CLASS = {0: "Expedited", 1: "Expedited", 2: "Normal", 3: "Bulk", 4: "Bulk"}

# Source ID -> EID (SPEC-DIGEST 7.1).
SOURCE_EID = {"Rover-Alpha": "ipn:99.1", "Earth-MCC": "ipn:23.1"}


def now_us():
    """Host clock (CLOCK_REALTIME) in Unix epoch microseconds."""
    return time.time_ns() // 1000


class SourceIdError(ValueError):
    """Source ID whose UTF-8 encoding is longer than 16 bytes (A4)."""


def source_id_bytes(source_id):
    """The 16-byte Source ID field: UTF-8, null-padded; refuses > 16 bytes (A4)."""
    raw = source_id.encode("utf-8")
    if len(raw) > 16:
        raise SourceIdError("Source ID %r is %d bytes in UTF-8; the field holds 16"
                            % (source_id, len(raw)))
    return raw.ljust(16, b"\0")


def source_id_display(raw16):
    """Display form: bytes up to the first 0x00, UTF-8 with replacement (A4)."""
    return bytes(raw16).split(b"\0", 1)[0].decode("utf-8", "replace")


@dataclass(frozen=True)
class Header:
    magic: int
    priority: int
    flags: int
    sequence: int
    source_id_raw: bytes
    timestamp_us: int
    size: int
    ttl_s: int
    crc32: int
    raw: bytes

    @property
    def source_id(self):
        return source_id_display(self.source_id_raw)


def decode_header(data):
    """Unpack the 44 header bytes with PBS_LINK's header format (no checks)."""
    raw = bytes(data[:HEADER_SIZE])
    magic, prio, flags, seq, src, ts, size, ttl, crc = struct.unpack(HEADER_FORMAT, raw)
    return Header(magic, prio, flags, seq, src, ts, size, ttl, crc, raw)


class EnvelopeBuilder:
    """Builds envelopes for one Source ID with PBS_LINK's ``PBSLink.send``.

    One builder holds one PBS_LINK sequence counter: the first envelope
    carries ``sequence_start`` (default 1), each next one adds 1, wrapping
    65535 -> 0 (PBS-ENV-01 Section 8, A5). The Timestamp is passed in exact
    integer microseconds through PBS_LINK's ``clock_source`` hook.
    """

    def __init__(self, source_id, sequence_start=1):
        self.source_id = source_id
        self.source_id_raw = source_id_bytes(source_id)     # refuses > 16 bytes
        self._ts = None
        self.link = PBS_LINK.PBSLink(device_id=source_id, max_payload_size=MAX_PAYLOAD,
                                     clock_source=self._clock)
        self.link.sequence = (sequence_start - 1) % 65536

    def _clock(self):
        return Fraction(self._ts, SEC)

    def build(self, priority, payload, ttl_s, ack=False, timestamp_us=None, sequence=None):
        """Return the envelope bytes (44-byte header + payload).

        ``timestamp_us`` defaults to the host clock. ``sequence`` sets the
        Sequence of this envelope explicitly; the counter continues from it.
        """
        if sequence is not None:
            self.link.sequence = (sequence - 1) % 65536
        self._ts = now_us() if timestamp_us is None else int(timestamp_us)
        return self.link.send(priority, bytes(payload), ttl=ttl_s, require_ack=bool(ack))


def build_envelope(source_id, priority, payload, ttl_s, sequence, timestamp_us, ack=False):
    """One envelope with every header input given explicitly (vectors, S7)."""
    return EnvelopeBuilder(source_id).build(priority, payload, ttl_s, ack=ack,
                                            timestamp_us=timestamp_us, sequence=sequence)


def is_expired(timestamp_us, ttl_s, now):
    """PBS-ENV-01 Section 12.2 in integer microseconds (A6, A37)."""
    return ttl_s > 0 and now - timestamp_us > ttl_s * SEC


_PARSER = PBS_LINK.PBSLink(device_id="Endpoint-A", max_payload_size=MAX_PAYLOAD)


def check_envelope(data, now):
    """Receive checks R1 to R6 (SPEC-DIGEST 2.5): length, magic, CRC32,
    priority, TTL, payload extraction.

    Returns ``(reason, header, payload)``; reason ``"OK"`` on success.
    ``header`` is ``None`` only for a buffer shorter than 44 bytes.

    PBS_LINK ``parse()`` performs the magic, CRC32 and priority checks and
    extracts the payload. It checks payload completeness before TTL and does
    not check for trailing bytes or the demo maximum Size, so those checks
    are made here in the PBS-ENV-01 Section 14 order (A7, A8, A9).
    """
    data = bytes(data)
    if len(data) < HEADER_SIZE:
        return "LENGTH", None, None
    hdr = decode_header(data)
    try:
        env = _PARSER.parse(data, validate_crc=True, validate_priority=True, check_ttl=False)
    except PBS_LINK.PBSMagicError:
        return "MAGIC", hdr, None
    except PBS_LINK.PBSCRCError:
        return "CRC", hdr, None
    except PBS_LINK.PBSPriorityError:
        return "PRIORITY", hdr, None
    except PBS_LINK.PBSValidationError:
        # Fewer than 44 + Size bytes. TTL is checked before payload extraction.
        if is_expired(hdr.timestamp_us, hdr.ttl_s, now):
            return "TTL_EXPIRED", hdr, None
        return "LENGTH", hdr, None
    if is_expired(env.timestamp, env.ttl, now):
        return "TTL_EXPIRED", hdr, None
    if env.size > MAX_PAYLOAD or len(data) != HEADER_SIZE + env.size:
        return "LENGTH", hdr, None
    return "OK", hdr, env.payload


def lifetime_bound_ms(timestamp_us, ttl_s, c_us):
    """PBS-DTN-MAP-01 Section 6.1 upper bound on the bundle lifetime (ms)."""
    return (ttl_s * SEC - max(0, c_us - timestamp_us)) // 1000


def ion_lifespan(timestamp_us, ttl_s, clock_us):
    """Lifetime in whole seconds for ION ``bp_send()`` (SPEC-DIGEST 7.4).

    Returns ``(lifespan_s, outcome)``; ``lifespan_s`` is None when the
    envelope must not be sent (outcome ``GW_EXPIRED`` or
    ``GW_LIFETIME_UNDER_1S``). Outcomes ``OK`` and ``NO_EXPIRY`` are sent.
    """
    if ttl_s == 0:
        return ION_NO_EXPIRY_S, "NO_EXPIRY"
    bound = lifetime_bound_ms(timestamp_us, ttl_s, clock_us + LIFETIME_MARGIN_US)
    if bound < 1:
        return None, "GW_EXPIRED"
    lifespan = min(bound // 1000, ION_NO_EXPIRY_S)
    if lifespan < 1:
        return None, "GW_LIFETIME_UNDER_1S"
    return lifespan, "OK"


def class_of_service(priority):
    return ION_CLASS_OF_SERVICE[priority]


def creation_ms_to_unix_us(creation_ms):
    return (creation_ms + DTN_EPOCH_OFFSET_MS) * 1000


def sha256_hex(data):
    return hashlib.sha256(bytes(data)).hexdigest()
