"""Telemetry plans for the rover.

A plan is a JSON object with a list ``items``. Each item sends ``count``
envelopes, the first ``at_s`` seconds after the rover starts its plan, then
one every ``interval_s`` seconds:

    {
      "description": "...",
      "items": [
        {"at_s": 0.0, "count": 3, "interval_s": 1.0, "priority": 0, "ttl_s": 0,
         "frame": "event", "text": "EVT MOTOR_STALL WHEEL=3", "payload_bytes": null,
         "ack_req": false, "label": "CRITICAL", "lifespan_override_s": null}
      ]
    }

Keys:
  at_s                 start offset in seconds (default 0)
  count                number of envelopes (default 1)
  interval_s           spacing in seconds (default 1)
  priority             0 CRITICAL .. 4 BULK (required)
  ttl_s                envelope TTL in seconds (default per PBS-ENV-01 12.4:
                       0, 60, 60, 3600, 86400 for priorities 0..4)
  frame                MUX frame type: telemetry, event or status (default telemetry)
  text                 ASCII frame text; " N=<i>" is appended (i = 1..count)
  payload_bytes        envelope Size; the frame value is extended with filler
                       bytes to reach it; 0 sends a header-only envelope
  ack_req              set ACK_REQ (Flags bit 0)
  label                copied to the log record (default "P<priority>")
  lifespan_override_s  FAULT INJECTION: pass this bundle lifetime to bp_send()
                       instead of the PBS-DTN-MAP-01 6.1 value (logged with
                       lifespan_rule OVERRIDE); not conformant
  extra_frames         frames placed before the main frame, e.g. an
                       application-defined type: [{"type": "0x80", "hex": "00ff"}]
                       or {"type": 128, "text": "..."}; not combined with
                       payload_bytes
"""

import json
from dataclasses import dataclass

from .mux import FT_EVENT, FT_STATUS, FT_TELEMETRY, build_container

DEFAULT_TTL = {0: 0, 1: 60, 2: 60, 3: 3600, 4: 86400}       # PBS-ENV-01 Section 12.4
FILLER = b"0123456789abcdef"
ALLOWED_FRAMES = {"telemetry": FT_TELEMETRY, "event": FT_EVENT, "status": FT_STATUS}
ITEM_KEYS = {"at_s", "count", "interval_s", "priority", "ttl_s", "frame", "text",
             "payload_bytes", "ack_req", "label", "lifespan_override_s", "extra_frames"}

NOMINAL_TEXT = {
    0: ("event", "EVT MOTOR_STALL WHEEL=3"),
    1: ("telemetry", "TLM POWER BATT=27.9V SOC=81%"),
    2: ("telemetry", "TLM THERMAL CPU=-12.5C"),
    3: ("telemetry", "TLM ENV DUST=0.02"),
    4: ("telemetry", "TLM IMAGE-CHUNK"),
}


@dataclass(frozen=True)
class Send:
    """One envelope to send at ``at_s`` seconds after plan start."""
    at_s: float
    priority: int
    ttl_s: int
    payload: bytes
    frame_types: tuple
    ack_req: bool
    label: str
    lifespan_override_s: object


def extra_frame_list(spec):
    """[(type, value)] from the plan's extra_frames list."""
    out = []
    for f in spec or ():
        t = f["type"]
        t = int(t, 16) if isinstance(t, str) else int(t)
        if not 0 <= t <= 255:
            raise ValueError("extra frame type %r" % f["type"])
        value = bytes.fromhex(f["hex"]) if "hex" in f else f.get("text", "").encode("ascii")
        out.append((t, value))
    return out


def make_payload(frame, text, index, payload_bytes, extra=()):
    """MUX container with ``extra`` frames and one main frame;
    ``payload_bytes`` fixes the Size."""
    if extra and payload_bytes is not None:
        raise ValueError("extra_frames cannot be combined with payload_bytes")
    if payload_bytes == 0:
        return b"", ()
    if payload_bytes is not None and payload_bytes > 65536:
        raise ValueError("payload_bytes %d exceeds the 65536-byte maximum (A9)" % payload_bytes)
    ftype = ALLOWED_FRAMES[frame]
    value = ("%s N=%d" % (text, index)).encode("ascii")
    if payload_bytes is not None:
        need = payload_bytes - 5          # container 2 bytes + frame header 3 bytes
        if need < len(value):
            raise ValueError("payload_bytes %d too small for text %r" % (payload_bytes, value))
        fill = need - len(value)
        value += (FILLER * (fill // len(FILLER) + 1))[:fill]
    frames = list(extra) + [(ftype, value)]
    return build_container(frames), tuple(t for t, _ in frames)


def expand(plan):
    """List of Send, ordered by time (stable for equal times)."""
    out = []
    for n, item in enumerate(plan.get("items", [])):
        unknown = set(item) - ITEM_KEYS
        if unknown:
            raise ValueError("plan item %d: unknown keys %s" % (n, sorted(unknown)))
        prio = int(item["priority"])
        if prio not in DEFAULT_TTL:
            raise ValueError("plan item %d: priority %r" % (n, prio))
        frame = item.get("frame", "telemetry")
        if frame not in ALLOWED_FRAMES:
            raise ValueError("plan item %d: frame %r (allowed: %s)"
                             % (n, frame, ", ".join(ALLOWED_FRAMES)))
        count = int(item.get("count", 1))
        interval = float(item.get("interval_s", 1.0))
        at = float(item.get("at_s", 0.0))
        ttl = int(item.get("ttl_s", DEFAULT_TTL[prio]))
        text = item.get("text", NOMINAL_TEXT[prio][1])
        label = item.get("label") or "P%d" % prio
        extra = extra_frame_list(item.get("extra_frames"))
        for i in range(1, count + 1):
            payload, types = make_payload(frame, text, i, item.get("payload_bytes"), extra)
            out.append(Send(at + (i - 1) * interval, prio, ttl, payload, types,
                            bool(item.get("ack_req", False)), label,
                            item.get("lifespan_override_s")))
    out.sort(key=lambda s: s.at_s)
    return out


def nominal(count=1, interval_s=5.0):
    """Built-in plan: one envelope per priority per round, ``count`` rounds."""
    items = []
    for prio in range(5):
        frame, text = NOMINAL_TEXT[prio]
        items.append({"at_s": 0.1 * prio, "count": count, "interval_s": interval_s,
                      "priority": prio, "frame": frame, "text": text,
                      "label": "NOMINAL-P%d" % prio})
    return {"description": "one envelope per priority per round", "items": items}


def load(spec):
    """``spec`` is a JSON file path, ``builtin:nominal``, or ``builtin:none``."""
    if spec in (None, "builtin:none"):
        return {"items": []}
    if spec.startswith("builtin:nominal"):
        parts = spec.split(":")[2:]
        count = int(parts[0]) if parts else 1
        interval = float(parts[1]) if len(parts) > 1 else 5.0
        return nominal(count, interval)
    with open(spec, encoding="utf-8") as fh:
        return json.load(fh)
