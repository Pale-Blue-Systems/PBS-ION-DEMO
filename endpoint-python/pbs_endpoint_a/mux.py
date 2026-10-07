"""PBS-MUX-01 container (SPEC-DIGEST.md Section 4).

container = mux_version (u8) | frame_count (u8) | frame * frame_count | padding
frame     = type (u8) | length (u16, big-endian) | value
"""

from dataclasses import dataclass

MUX_VERSION = 0x01                          # demo value (A13)

FT_TELEMETRY = 0x01
FT_COMMAND = 0x02
FT_STATUS = 0x03
FT_EVENT = 0x04
FT_SVC = 0x07
FT_AUTH = 0x08
FT_PNT = 0x09
FT_SEC = 0x0A

FRAME_NAMES = {FT_TELEMETRY: "telemetry", FT_COMMAND: "command", FT_STATUS: "status",
               FT_EVENT: "event", FT_SVC: "service-intent", FT_AUTH: "authority-context",
               FT_PNT: "pnt-context", FT_SEC: "security"}
FRAME_BY_NAME = {v: k for k, v in FRAME_NAMES.items()}


class MuxError(Exception):
    """Container rejected; ``reason`` is MUX_VERSION or MUX_MALFORMED (A13, A14)."""

    def __init__(self, reason, detail=""):
        super().__init__("%s %s" % (reason, detail))
        self.reason = reason


@dataclass(frozen=True)
class Frame:
    type: int
    value: bytes


def encode_frame(ftype, value):
    value = bytes(value)
    if not 0 <= ftype <= 0xFF:
        raise ValueError("frame type %r" % ftype)
    if len(value) > 0xFFFF:
        raise ValueError("frame value of %d bytes exceeds 65535" % len(value))
    return bytes([ftype]) + len(value).to_bytes(2, "big") + value


def build_container(frames, count=None, padding=b""):
    """Container with the given (type, value) frames; senders write no padding."""
    frames = list(frames)
    n = len(frames) if count is None else count
    if not 0 <= n <= 0xFF:
        raise ValueError("frame_count %d" % n)
    out = bytearray([MUX_VERSION, n])
    for ftype, value in frames:
        out += encode_frame(ftype, value)
    return bytes(out) + bytes(padding)


def parse_container(payload):
    """Return ``(frames, padding_len)``; raise MuxError on a bad container.

    Exactly ``frame_count`` frames are read in order; bytes after the last
    frame are padding and are never parsed as frames (PBS-MUX-01 4.1 to 4.3).
    A frame running past the payload, or fewer than ``frame_count`` complete
    frames, rejects the container as a whole (A14).
    """
    p = bytes(payload)
    if len(p) < 2:
        raise MuxError("MUX_MALFORMED", "container shorter than 2 bytes")
    if p[0] != MUX_VERSION:
        raise MuxError("MUX_VERSION", "mux_version 0x%02X" % p[0])
    count, off, frames = p[1], 2, []
    for i in range(count):
        if off + 3 > len(p):
            raise MuxError("MUX_MALFORMED", "frame %d header past end" % i)
        ftype = p[off]
        ln = int.from_bytes(p[off + 1:off + 3], "big")
        if off + 3 + ln > len(p):
            raise MuxError("MUX_MALFORMED", "frame %d value past end" % i)
        frames.append(Frame(ftype, p[off + 3:off + 3 + ln]))
        off += 3 + ln
    return frames, len(p) - off
