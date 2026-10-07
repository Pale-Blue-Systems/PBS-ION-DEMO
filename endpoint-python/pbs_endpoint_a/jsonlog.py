"""JSON Lines log in the format of docs/LOG-SCHEMA.md (pbs-ion-demo-log/1)."""

import collections
import json
import threading

from . import envelope as E

SCHEMA = "pbs-ion-demo-log/1"
ENVELOPE_HEX_MAX = 4096             # default hex limit (docs/LOG-SCHEMA.md Section 4)
_hex_max = ENVELOPE_HEX_MAX


def set_hex_max(n):
    """Log envelope_hex for envelopes of at most n bytes (bin/rover --log-hex-max)."""
    global _hex_max
    _hex_max = int(n)


def hex_max():
    return _hex_max


def latency_us(t_us, timestamp_us):
    """t_us - Timestamp, or None outside the signed 64-bit range (LOG-SCHEMA.md 6)."""
    d = t_us - timestamp_us
    return d if -(1 << 63) <= d < (1 << 63) else None

COMMON_FIELDS = ("schema", "event", "t_us", "endpoint", "node_eid", "scenario")
ENVELOPE_FIELDS = ("source_id", "source_id_hex", "sequence", "priority", "flags", "timestamp_us",
                   "ttl_s", "size", "crc32", "header_hex", "envelope_len", "envelope_sha256",
                   "envelope_hex", "frame_types")
EVENT_FIELDS = {
    "run_start": ("impl", "pbs_link_version", "ion_version", "source_id", "dest_eid", "pid",
                  "argv", "keys"),
    "send": ENVELOPE_FIELDS + ("result", "reason", "dest_eid", "lifespan_s", "lifespan_rule",
                               "class_of_service", "ordinal", "bp_send_return_us", "label",
                               "commands", "suite_id", "key_id", "security_sequence"),
    "recv": ENVELOPE_FIELDS + ("result", "reason", "bundle_source_eid", "bundle_creation_ms",
                               "bundle_creation_count", "bundle_creation_us",
                               "bundle_ttl_field_ms", "bundle_admin_record", "expected_eid",
                               "eid_match", "latency_us", "commands", "suite_id", "key_id",
                               "security_sequence", "authority_id", "role", "policy_epoch"),
    "exec": ("source_id_hex", "sequence", "timestamp_us", "command", "operation", "key_id",
             "security_sequence", "role", "action"),
    "run_end": ("counts",),
}


def envelope_fields(data, header=None, frame_types=None):
    """Section 4 fields for the bytes ``data`` (header decoded if >= 44 bytes)."""
    data = bytes(data)
    out = dict.fromkeys(ENVELOPE_FIELDS)
    out["envelope_len"] = len(data)
    out["envelope_sha256"] = E.sha256_hex(data)
    out["envelope_hex"] = data.hex() if len(data) <= _hex_max else None
    out["frame_types"] = None if frame_types is None else list(frame_types)
    if header is None and len(data) >= E.HEADER_SIZE:
        header = E.decode_header(data)
    if header is not None:
        out.update(source_id=header.source_id, source_id_hex=header.source_id_raw.hex(),
                   sequence=header.sequence, priority=header.priority, flags=header.flags,
                   timestamp_us=header.timestamp_us, ttl_s=header.ttl_s, size=header.size,
                   crc32="0x%08X" % header.crc32, header_hex=header.raw.hex())
    return out


class JsonlLog:
    """Thread-safe writer; every line is flushed when written."""

    def __init__(self, path, endpoint, node_eid, scenario=None):
        self.path = path
        self.endpoint = endpoint
        self.node_eid = node_eid
        self.scenario = scenario
        self.counts = collections.Counter()
        self._lock = threading.Lock()
        self._fh = open(path, "a", encoding="utf-8")

    def write(self, event, t_us, **fields):
        rec = {"schema": SCHEMA, "event": event, "t_us": int(t_us), "endpoint": self.endpoint,
               "node_eid": self.node_eid, "scenario": self.scenario}
        for name in EVENT_FIELDS[event]:
            rec[name] = fields.pop(name, None)
        for name, value in fields.items():
            if not name.startswith("x_"):
                raise KeyError("field %r is not in docs/LOG-SCHEMA.md for event %r" % (name, event))
            rec[name] = value
        line = json.dumps(rec, separators=(",", ":"), ensure_ascii=False)
        with self._lock:
            if event == "send":
                self.counts["send:%s" % rec["result"]] += 1
                if rec["result"] != "SENT":
                    self.counts["send:%s:%s" % (rec["result"], rec["reason"])] += 1
            elif event == "recv":
                self.counts["recv:%s:%s" % (rec["result"], rec["reason"])] += 1
            elif event == "exec":
                self.counts["exec"] += 1
            self._fh.write(line + "\n")
            self._fh.flush()
        return rec

    def close(self):
        with self._lock:
            if not self._fh.closed:
                self._fh.close()
