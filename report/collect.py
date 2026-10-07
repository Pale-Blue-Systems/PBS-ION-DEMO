#!/usr/bin/env python3
"""Collect one scenario run into tables.

Reads results/<scenario>/ (endpoint-a.jsonl, endpoint-b.jsonl in the format
of docs/LOG-SCHEMA.md, emulator-stats.json, run.json, probe outputs and the
copied ion.log files) and writes

  envelopes.csv   one row per envelope an endpoint built (send record),
                  joined with the other endpoint's recv record
  receipts.csv    one row per recv record, with the matching send row
  links.csv       one row per emulated link direction (emulator counters)
  ionstats.csv    bpstats counters per node (last report in ion.log)

  report/collect.py results/S1 [results/S2 ...]

Join rule: a recv record matches a send record of the other endpoint with
the same envelope identity (source_id_hex, sequence, timestamp_us;
docs/LOG-SCHEMA.md Section 4). Send records are taken in time order and
each takes the earliest unmatched recv record with that identity received
after it was sent, so a replayed envelope (same bytes as its original)
matches the second receipt.
"""
import csv
import json
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PEER = {"A": "B", "B": "A"}
COS_TABLE = {0: 2, 1: 2, 2: 1, 3: 0, 4: 0}          # SPEC-DIGEST.md 7.5
PRIO_NAME = {0: "CRITICAL", 1: "HIGH", 2: "NORMAL", 3: "LOW", 4: "BULK"}

ENVELOPE_COLUMNS = [
    "scenario", "dir", "sender", "label", "priority", "priority_name", "ttl_s", "size",
    "envelope_len", "sequence", "timestamp_us", "send_t_s", "send_result", "send_reason",
    "lifespan_s", "lifespan_rule", "class_of_service", "commands", "suite_id", "key_id",
    "security_sequence", "delivered", "n_recv", "recv_t_s", "latency_s", "recv_result",
    "recv_reason", "sha256_match", "header_match", "eid_match", "bundle_source_eid",
    "bundle_ttl_field_ms", "bundle_creation_lag_s", "exec_count", "envelope_sha256",
]
RECEIPT_COLUMNS = [
    "scenario", "receiver", "t_s", "source_id", "sequence", "priority", "ttl_s", "size",
    "timestamp_us", "result", "reason", "latency_s", "bundle_source_eid", "eid_match",
    "suite_id", "key_id", "security_sequence", "role", "commands", "matched_label",
]
LINK_COLUMNS = [
    "scenario", "link", "delay_s", "loss", "rate_Bps", "queue_bytes", "blackouts", "rx", "tx",
    "rx_bytes", "tx_bytes", "drop_loss", "drop_blackout", "drop_queue_full", "send_error",
    "pending_at_stop", "max_queue_delay_s", "ltp_DS", "ltp_CP", "ltp_RS", "ltp_RA",
    "ltp_cancel", "ltp_data_retransmissions",
]
STAT_CLASSES = ["src", "fwd", "xmt", "rcv", "dlv", "rfw", "exp"]


def read_jsonl(path):
    out = []
    if not os.path.exists(path):
        return out
    with open(path, errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                out.append({"event": "_unparsed", "line": line[:200]})
    return out


def read_json(path, default=None):
    if not os.path.exists(path):
        return default
    with open(path) as fh:
        return json.load(fh)


def identity(rec):
    k = (rec.get("source_id_hex"), rec.get("sequence"), rec.get("timestamp_us"))
    return None if None in k else k


def parse_bpstats(text):
    """Last bpstats report in an ion.log: {class: {"0": n, "1": n, "2": n, "+": n}}."""
    out = {}
    pat = re.compile(r"\[x\] (\w+) from .* to .*: \(0\) (\d+) (\d+) \(1\) (\d+) (\d+) "
                     r"\(2\) (\d+) (\d+) \(\+\) (\d+) (\d+)")
    for line in text.splitlines():
        m = pat.search(line)
        if m and m.group(1) in STAT_CLASSES:
            g = [int(x) for x in m.groups()[1:]]
            out[m.group(1)] = {"0": g[0], "1": g[2], "2": g[4], "+": g[6],
                               "bytes": g[7]}
    return out


LTP_STATS = ["ckpt_xmit", "ckpt_re_xmit", "seg_re_xmit", "pos_rpt_recv", "neg_rpt_recv",
             "export_cancel_xmit", "export_cancel_recv", "export_complete", "ckpt_recv",
             "rpt_re_xmit", "import_complete", "in_seg_recv_red", "in_seg_redundant",
             "in_seg_screened", "in_seg_ses_closed", "in_seg_too_fast"]


def parse_ltpstats(text):
    """Last 'ltpstats -a' report per span in an ion.log:
    {peer engine: {stat name: count}}."""
    out = {}
    pat = re.compile(r"\[x\] span (\d+) (\w+) from .* to .*: (\d+) (\d+)")
    for line in text.splitlines():
        m = pat.search(line)
        if m:
            out.setdefault(int(m.group(1)), {})[m.group(2)] = int(m.group(3))
    return out


def parse_bplist_count(text):
    m = re.findall(r"Report count is (\d+)", text)
    if m:
        return int(m[-1])
    m = re.findall(r"(\d+) bundles?", text)
    return int(m[-1]) if m else None


def parse_bplist_detail(text):
    """Bundles listed by 'bplist' (detail mode): source and destination EID,
    payload bytes, and the PBS envelope identity read from the payload."""
    out = []
    cur = None
    in_payload = False
    for line in text.splitlines():
        if line.startswith("**** Bundle"):
            cur = {"source_eid": None, "dest_eid": None, "payload": bytearray()}
            out.append(cur)
            in_payload = False
        elif cur is None:
            continue
        elif line.startswith("Source EID"):
            cur["source_eid"] = line.split("'")[1]
        elif line.startswith("Destination EID"):
            cur["dest_eid"] = line.split("'")[1]
        elif line.startswith("****** Payload"):
            in_payload = True
        elif line.startswith("******") or line.startswith("- payload") or \
                line.startswith("**** End"):
            in_payload = False
        elif in_payload and line.startswith("("):
            body = line.split(")", 1)[1].strip()
            groups = body.split("   ", 1)[0].split()
            try:
                cur["payload"] += bytes.fromhex("".join(groups))
            except ValueError:
                pass
    for b in out:
        p = bytes(b.pop("payload"))
        b["payload_len"] = len(p)
        if len(p) >= 44 and p[0] == 0x10:
            b["source_id_hex"] = p[8:24].hex()
            b["sequence"] = int.from_bytes(p[4:6], "big")
            b["timestamp_us"] = int.from_bytes(p[24:32], "big")
            b["priority"] = p[1]
            b["ttl_s"] = int.from_bytes(p[36:40], "big")
        else:
            b["source_id_hex"] = b["sequence"] = b["timestamp_us"] = None
    return out


class RunData:
    def __init__(self, outdir):
        self.dir = os.path.abspath(outdir)
        self.run = read_json(os.path.join(self.dir, "run.json"), {}) or {}
        self.scn = read_json(os.path.join(self.dir, "scenario.json"), {}) or {}
        self.id = self.scn.get("id") or self.run.get("scenario") or os.path.basename(self.dir)
        self.origin = self.run.get("origin_unix_s")
        self.logs = {"A": read_jsonl(os.path.join(self.dir, "endpoint-a.jsonl")),
                     "B": read_jsonl(os.path.join(self.dir, "endpoint-b.jsonl"))}
        self.emu = read_json(os.path.join(self.dir, "emulator-stats.json"), {}) or {}
        self.envelopes = []
        self.receipts = []
        self.unmatched = {"A": [], "B": []}
        self._join()

    def t_s(self, t_us):
        if t_us is None or self.origin is None:
            return None
        return round(t_us / 1e6 - self.origin, 6)

    def records(self, endpoint, event):
        return [r for r in self.logs[endpoint] if r.get("event") == event]

    def _join(self):
        recv = {}
        execs = {}
        for ep in ("A", "B"):
            pool = {}
            for r in sorted(self.records(ep, "recv"), key=lambda r: r["t_us"]):
                pool.setdefault(identity(r), []).append(r)
            recv[ep] = pool
            ex = {}
            for r in self.records(ep, "exec"):
                ex.setdefault(identity(r), []).append(r)
            execs[ep] = ex
        matched_ids = set()
        rows = []
        sends = []
        for ep in ("A", "B"):
            sends += [(ep, r) for r in self.records(ep, "send")]
        sends.sort(key=lambda er: er[1]["t_us"])
        for ep, s in sends:
            peer = PEER[ep]
            row = self._send_row(ep, s)
            got = None
            if s.get("result") == "SENT":
                cands = recv[peer].get(identity(s), [])
                for r in cands:
                    if id(r) not in matched_ids and r["t_us"] >= s["t_us"]:
                        got = r
                        break
            if got is not None:
                matched_ids.add(id(got))
                got["_matched_label"] = s.get("label")
                same = [r for r in recv[peer].get(identity(s), [])]
                row.update(self._recv_fields(s, got))
                row["n_recv"] = len(same)
                ex = execs[peer].get(identity(s), [])
                row["exec_count"] = len([e for e in ex if got["t_us"] <= e["t_us"] <= got["t_us"] + 1000000]) \
                    if got.get("result") == "ACCEPT" else 0
            rows.append(row)
        self.envelopes = rows
        for ep in ("A", "B"):
            for key, lst in recv[ep].items():
                for r in lst:
                    self.receipts.append(self._receipt_row(ep, r))
                    if id(r) not in matched_ids:
                        self.unmatched[ep].append(r)
        self.receipts.sort(key=lambda r: (r["t_s"] is None, r["t_s"]))

    def _send_row(self, ep, s):
        prio = s.get("priority")
        return {
            "scenario": self.id, "dir": "%s->%s" % (ep, PEER[ep]), "sender": ep,
            "label": s.get("label"), "priority": prio,
            "priority_name": PRIO_NAME.get(prio, str(prio)), "ttl_s": s.get("ttl_s"),
            "size": s.get("size"), "envelope_len": s.get("envelope_len"),
            "sequence": s.get("sequence"), "timestamp_us": s.get("timestamp_us"),
            "send_t_s": self.t_s(s.get("t_us")), "send_result": s.get("result"),
            "send_reason": s.get("reason"), "lifespan_s": s.get("lifespan_s"),
            "lifespan_rule": s.get("lifespan_rule"),
            "class_of_service": s.get("class_of_service"),
            "commands": ";".join(s.get("commands") or []), "suite_id": s.get("suite_id"),
            "key_id": s.get("key_id"), "security_sequence": s.get("security_sequence"),
            "delivered": 0, "n_recv": 0, "recv_t_s": None, "latency_s": None,
            "recv_result": None, "recv_reason": None, "sha256_match": None,
            "header_match": None, "eid_match": None, "bundle_source_eid": None,
            "bundle_ttl_field_ms": None, "bundle_creation_lag_s": None, "exec_count": 0,
            "envelope_sha256": s.get("envelope_sha256"),
        }

    def _recv_fields(self, s, r):
        lat = r.get("latency_us")
        if lat is None and r.get("timestamp_us") is not None:
            lat = r["t_us"] - r["timestamp_us"]
        lag = None
        if r.get("bundle_creation_us") is not None and s.get("timestamp_us") is not None:
            lag = round((r["bundle_creation_us"] - s["timestamp_us"]) / 1e6, 6)
        return {
            "delivered": 1, "recv_t_s": self.t_s(r["t_us"]),
            "latency_s": None if lat is None else round(lat / 1e6, 6),
            "recv_result": r.get("result"), "recv_reason": r.get("reason"),
            "sha256_match": r.get("envelope_sha256") == s.get("envelope_sha256"),
            "header_match": r.get("header_hex") == s.get("header_hex"),
            "eid_match": r.get("eid_match"), "bundle_source_eid": r.get("bundle_source_eid"),
            "bundle_ttl_field_ms": r.get("bundle_ttl_field_ms"),
            "bundle_creation_lag_s": lag,
        }

    def _receipt_row(self, ep, r):
        lat = r.get("latency_us")
        return {
            "scenario": self.id, "receiver": ep, "t_s": self.t_s(r.get("t_us")),
            "source_id": r.get("source_id"), "sequence": r.get("sequence"),
            "priority": r.get("priority"), "ttl_s": r.get("ttl_s"), "size": r.get("size"),
            "timestamp_us": r.get("timestamp_us"), "result": r.get("result"),
            "reason": r.get("reason"),
            "latency_s": None if lat is None else round(lat / 1e6, 6),
            "bundle_source_eid": r.get("bundle_source_eid"), "eid_match": r.get("eid_match"),
            "suite_id": r.get("suite_id"), "key_id": r.get("key_id"),
            "security_sequence": r.get("security_sequence"), "role": r.get("role"),
            "commands": ";".join(r.get("commands") or []),
            "matched_label": r.get("_matched_label"),
        }

    def emulator_timing(self):
        """Emulator event-log summary: forwarding lateness (tx time minus the
        scheduled delivery time, ms) and, per link, the receive times of all
        datagrams in scenario seconds."""
        if hasattr(self, "_emu_timing"):
            return self._emu_timing
        late = []
        rx = {}
        segs = {}
        path = os.path.join(self.dir, "emulator.jsonl")
        if os.path.exists(path):
            with open(path) as fh:
                for line in fh:
                    try:
                        e = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if e.get("ev") == "tx" and "due_us" in e:
                        late.append((e["t_us"] - e["due_us"]) / 1000.0)
                    elif e.get("ev") == "rx" and self.origin is not None:
                        t = e["t_us"] / 1e6 - self.origin
                        rx.setdefault(e["link"], []).append(t)
                        lt = e.get("ltp")
                        if lt:
                            segs.setdefault(e["link"], []).append(
                                (t, lt["kind"], lt["eng"], lt["ses"]))
        self._emu_timing = {"lateness_ms": sorted(late), "rx": rx, "ltp": segs}
        return self._emu_timing

    def ltp_after_resume(self, data_link, report_link, t_from, t_to):
        """LTP blocks whose first checkpoint the emulator saw on data_link in
        [t_from, t_to): how many, how many were answered by a report on
        report_link before the checkpoint was sent again, and the time from
        the first checkpoint to the first report."""
        segs = self.emulator_timing()["ltp"]
        first_cp, cp_times = {}, {}
        for t, kind, eng, ses in segs.get(data_link, []):
            if kind == "CP":
                cp_times.setdefault((eng, ses), []).append(t)
        first_rs = {}
        for t, kind, eng, ses in segs.get(report_link, []):
            if kind == "RS" and (eng, ses) not in first_rs:
                first_rs[(eng, ses)] = t
        blocks = []
        for key, times in cp_times.items():
            if t_from <= times[0] < t_to:
                rs = first_rs.get(key)
                again = [x for x in times[1:] if rs is None or x < rs]
                blocks.append({"session": key, "first_cp": times[0], "first_rs": rs,
                               "cp_resent_before_report": len(again) > 0,
                               "report_after_s": None if rs is None else rs - times[0]})
        return blocks

    def first_rx_after(self, links, t_s):
        times = [t for l in links for t in self.emulator_timing()["rx"].get(l, []) if t >= t_s]
        return min(times) if times else None

    def link_rows(self):
        rows = []
        for name, st in sorted((self.emu.get("links") or {}).items()):
            segs = st.get("ltp_segments", {})
            rows.append({
                "scenario": self.id, "link": name, "delay_s": st.get("delay_s"),
                "loss": st.get("loss"), "rate_Bps": st.get("rate_Bps"),
                "queue_bytes": st.get("queue_bytes"),
                "blackouts": ";".join("%g-%g %s" % (w["start_s"], w["end_s"], w["kind"])
                                      for w in st.get("blackouts", [])),
                "rx": st.get("rx"), "tx": st.get("tx"), "rx_bytes": st.get("rx_bytes"),
                "tx_bytes": st.get("tx_bytes"), "drop_loss": st.get("drop_loss"),
                "drop_blackout": st.get("drop_blackout"),
                "drop_queue_full": st.get("drop_queue_full"),
                "send_error": st.get("send_error"), "pending_at_stop": st.get("pending_at_stop"),
                "max_queue_delay_s": st.get("max_queue_delay_s"),
                "ltp_DS": segs.get("DS", 0), "ltp_CP": segs.get("CP", 0),
                "ltp_RS": segs.get("RS", 0), "ltp_RA": segs.get("RA", 0),
                "ltp_cancel": sum(segs.get(k, 0) for k in ("CS", "CAS", "CR", "CAR")),
                "ltp_data_retransmissions": st.get("ltp_data_retransmissions"),
            })
        return rows

    def ion_stats(self):
        out = {}
        for node in ("rover", "gateway", "earth"):
            p = os.path.join(self.dir, "ion", "%s.ion.log" % node)
            if os.path.exists(p):
                out[node] = parse_bpstats(open(p, errors="replace").read())
        return out

    def ltp_stats(self):
        out = {}
        for node in ("rover", "gateway", "earth"):
            p = os.path.join(self.dir, "ion", "%s.ion.log" % node)
            if os.path.exists(p):
                st = parse_ltpstats(open(p, errors="replace").read())
                if st:
                    out[node] = st
        return out

    def ion_stat_rows(self):
        rows = []
        for node, st in self.ion_stats().items():
            for cls in STAT_CLASSES:
                c = st.get(cls)
                if c:
                    rows.append({"scenario": self.id, "node": node, "class": cls,
                                 "prio0_bulk": c["0"], "prio1_std": c["1"],
                                 "prio2_expedited": c["2"], "total": c["+"],
                                 "total_bytes": c["bytes"]})
        return rows

    def probes(self):
        out = []
        for p in self.run.get("probes", []):
            rec = dict(p)
            path = os.path.join(self.dir, p["file"])
            text = open(path, errors="replace").read() if os.path.exists(path) else ""
            if p["argv"] == ["bplist", "count"]:
                rec["count"] = parse_bplist_count(text)
            elif p["argv"] == ["bplist"]:
                rec["bundles"] = parse_bplist_detail(text)
                rec["count"] = len(rec["bundles"])
                by_id = {(r["sequence"], r["timestamp_us"]): r for r in self.envelopes}
                for b in rec["bundles"]:
                    r = by_id.get((b["sequence"], b["timestamp_us"]))
                    b["label"] = r["label"] if r else None
                    b["dir"] = r["dir"] if r else None
            if p["argv"][:1] == ["bpstats"]:
                rec["stats"] = parse_bpstats(open(os.path.join(
                    self.dir, "ion", "%s.ion.log" % p["node"]), errors="replace").read()) \
                    if os.path.exists(os.path.join(self.dir, "ion", "%s.ion.log" % p["node"])) \
                    else {}
            out.append(rec)
        return out


def write_csv(path, columns, rows):
    with open(path, "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=columns, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow({k: ("" if r.get(k) is None else r.get(k)) for k in columns})


def collect(outdir):
    d = RunData(outdir)
    write_csv(os.path.join(d.dir, "envelopes.csv"), ENVELOPE_COLUMNS, d.envelopes)
    write_csv(os.path.join(d.dir, "receipts.csv"), RECEIPT_COLUMNS, d.receipts)
    write_csv(os.path.join(d.dir, "links.csv"), LINK_COLUMNS, d.link_rows())
    write_csv(os.path.join(d.dir, "ionstats.csv"),
              ["scenario", "node", "class", "prio0_bulk", "prio1_std", "prio2_expedited",
               "total", "total_bytes"], d.ion_stat_rows())
    return d


def main(argv):
    if not argv:
        print(__doc__, file=sys.stderr)
        return 2
    for p in argv:
        d = collect(p)
        n = len(d.envelopes)
        dl = sum(r["delivered"] for r in d.envelopes)
        print("%s: %d envelopes, %d delivered, %d receipts" % (d.id, n, dl, len(d.receipts)))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
