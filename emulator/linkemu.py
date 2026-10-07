#!/usr/bin/env python3
"""User-space link emulator for the PBS ION demo.

Each emulated link is one direction of one inter-node hop. The emulator
binds a UDP socket on 127.0.0.1:<listen> (the port a node's LTP link
service output, udplso, sends to) and forwards every datagram to
127.0.0.1:<dest> (the peer node's udplsi). On the way it applies, per link:

  rate_Bps     serialization at this rate (bytes of UDP payload per second);
               datagrams wait in a FIFO queue while the link is busy
  queue_bytes  drop-tail limit on bytes waiting in that queue
  blackouts    time windows in which the link carries nothing; a datagram
               whose transmission starts inside a window is lost
  loss         probability that a datagram is lost (independent per datagram,
               pseudo-random generator seeded per link)
  delay_s      one-way propagation delay added after serialization
  jitter_s     extra delay drawn uniformly from [0, jitter_s]; delivery order
               is kept (a datagram never overtakes an earlier one)

Order of decisions for a datagram received at time t: queue admission
(queue_full), transmission start = max(t, end of previous transmission),
blackout at transmission start (blackout), loss (loss), delivery at
transmission end + delay_s + jitter.

Times in the configuration are seconds relative to --t0 (Unix epoch
seconds), the scenario time origin. Every datagram produces a JSON line in
--log: an "rx" record when it arrives, then either a "drop" record or a
"tx" record when it is forwarded. "rx" records carry the decoded LTP
segment header (RFC 5326 Section 3) when the datagram parses as one. On
SIGTERM or SIGINT the emulator writes per-link counters to --stats and exits;
datagrams still waiting are counted as pending_at_stop.

Configuration (JSON):
  {"seed": 1,
   "links": [{"name": "rover-gateway", "listen": 29950, "dest": 15013,
              "delay_s": 0.25, "jitter_s": 0.0, "loss": 0.0,
              "rate_Bps": null, "queue_bytes": null,
              "blackouts": [{"start_s": 30, "end_s": 55, "label": "far-side pass",
                             "kind": "scheduled"}]}]}

Usage:
  linkemu.py --config emulator.json --t0 1767225600 --log events.jsonl --stats stats.json
"""
import argparse
import heapq
import json
import os
import random
import selectors
import signal
import socket
import sys
import time

HOST = "127.0.0.1"
SCHEMA = "pbs-ion-demo-linkemu/1"

LTP_KIND = {0: "DS", 1: "CP", 2: "CP", 3: "CP", 4: "GD", 5: "GD", 6: "GD", 7: "GD",
            8: "RS", 9: "RA", 12: "CS", 13: "CAS", 14: "CR", 15: "CAR"}


# --------------------------------------------------------------------------
# LTP segment header decoding (RFC 5326 Section 3), for the log only.
# --------------------------------------------------------------------------

def _sdnv(buf, i):
    value = 0
    for _ in range(10):
        if i >= len(buf):
            raise ValueError("SDNV runs past the end")
        b = buf[i]
        i += 1
        value = (value << 7) | (b & 0x7F)
        if not b & 0x80:
            return value, i
    raise ValueError("SDNV too long")


def decode_ltp(data):
    """Header fields of an LTP segment, or None if the bytes do not parse.

    Keys: type (segment type code 0-15), kind (DS red data, CP red data
    checkpoint, GD green data, RS report, RA report ack, CS/CAS/CR/CAR
    cancel), eng (session originator engine), ses (session number); data
    segments add off, dlen and for checkpoints cp (checkpoint serial)
    and rsn (report serial); RS adds rs, cp, ub, lb, claims; RA adds rs;
    cancel segments add reason.
    """
    try:
        if len(data) < 4 or data[0] >> 4 != 0:
            return None
        typ = data[0] & 0x0F
        if typ not in LTP_KIND:
            return None
        eng, i = _sdnv(data, 1)
        ses, i = _sdnv(data, i)
        ext = data[i]
        i += 1
        for _ in range(ext >> 4):
            i += 1                                   # extension tag
            length, i = _sdnv(data, i)
            i += length
        out = {"type": typ, "kind": LTP_KIND[typ], "eng": eng, "ses": ses}
        if typ <= 7:
            _client, i = _sdnv(data, i)
            out["off"], i = _sdnv(data, i)
            out["dlen"], i = _sdnv(data, i)
            if typ in (1, 2, 3):
                out["cp"], i = _sdnv(data, i)
                out["rsn"], i = _sdnv(data, i)
        elif typ == 8:
            out["rs"], i = _sdnv(data, i)
            out["cp"], i = _sdnv(data, i)
            out["ub"], i = _sdnv(data, i)
            out["lb"], i = _sdnv(data, i)
            out["claims"], i = _sdnv(data, i)
        elif typ == 9:
            out["rs"], i = _sdnv(data, i)
        elif typ in (12, 14):
            out["reason"] = data[i] if i < len(data) else None
        if i > len(data):
            return None
        return out
    except (ValueError, IndexError):
        return None


# --------------------------------------------------------------------------
# Link model: decisions only, no sockets, so that it can be unit-tested.
# --------------------------------------------------------------------------

class Link:
    """One direction of one hop. All times are seconds on one clock."""

    def __init__(self, name, listen, dest, delay_s=0.0, jitter_s=0.0, loss=0.0,
                 rate_Bps=None, queue_bytes=None, blackouts=(), seed=0, t0=0.0):
        if delay_s < 0 or jitter_s < 0:
            raise ValueError("%s: negative delay or jitter" % name)
        if not 0.0 <= loss <= 1.0:
            raise ValueError("%s: loss must be in [0, 1]" % name)
        if rate_Bps is not None and rate_Bps <= 0:
            raise ValueError("%s: rate_Bps must be positive or null" % name)
        self.name = name
        self.listen = int(listen)
        self.dest = int(dest)
        self.delay_s = float(delay_s)
        self.jitter_s = float(jitter_s)
        self.loss = float(loss)
        self.rate_Bps = float(rate_Bps) if rate_Bps else None
        self.queue_bytes = int(queue_bytes) if queue_bytes else None
        self.t0 = float(t0)
        self.blackouts = []
        for w in blackouts:
            if w["end_s"] <= w["start_s"]:
                raise ValueError("%s: blackout ends before it starts" % name)
            self.blackouts.append((float(w["start_s"]), float(w["end_s"]),
                                   w.get("label", ""), w.get("kind", "unscheduled")))
        # One generator per link, seeded from the link name and the seed, so
        # the loss pattern of a link does not depend on traffic on other links.
        self.rng = random.Random("%s/%s" % (seed, name))
        self.busy_until = None          # end of the transmission in progress
        self.last_delivery = None
        self.n = 0
        self.counters = {"rx": 0, "rx_bytes": 0, "tx": 0, "tx_bytes": 0,
                         "drop_loss": 0, "drop_blackout": 0, "drop_queue_full": 0,
                         "send_error": 0, "pending_at_stop": 0,
                         "max_queue_delay_s": 0.0, "max_queue_bytes": 0}
        self.ltp_counts = {}
        self.data_seen = set()
        self.ltp_data_retransmissions = 0

    def blackout_at(self, t):
        """Window (start_s, end_s, label, kind) containing time t, or None."""
        rel = t - self.t0
        for w in self.blackouts:
            if w[0] <= rel < w[1]:
                return w
        return None

    def queued_bytes(self, t):
        if self.rate_Bps is None or self.busy_until is None or self.busy_until <= t:
            return 0
        return int(round((self.busy_until - t) * self.rate_Bps))

    def admit(self, t, nbytes):
        """Decide the fate of a datagram of nbytes arriving at time t.

        Returns a dict: {"drop": reason, ...} or {"deliver_at": time, ...};
        both carry "start" (transmission start) and "queue_s" when the
        datagram was admitted to the queue.
        """
        self.n += 1
        self.counters["rx"] += 1
        self.counters["rx_bytes"] += nbytes
        if self.rate_Bps is not None:
            backlog = self.queued_bytes(t)
            if self.queue_bytes is not None and backlog + nbytes > self.queue_bytes:
                self.counters["drop_queue_full"] += 1
                return {"drop": "queue_full", "queued_bytes": backlog}
            start = max(t, self.busy_until or t)
            end = start + nbytes / self.rate_Bps
            self.busy_until = end
            self.counters["max_queue_bytes"] = max(self.counters["max_queue_bytes"],
                                                   backlog + nbytes)
        else:
            start = end = t
        queue_s = start - t
        self.counters["max_queue_delay_s"] = max(self.counters["max_queue_delay_s"], queue_s)
        w = self.blackout_at(start)
        if w is not None:
            self.counters["drop_blackout"] += 1
            return {"drop": "blackout", "start": start, "queue_s": queue_s,
                    "window": w[2], "window_kind": w[3]}
        if self.loss > 0.0 and self.rng.random() < self.loss:
            self.counters["drop_loss"] += 1
            return {"drop": "loss", "start": start, "queue_s": queue_s}
        deliver = end + self.delay_s
        if self.jitter_s > 0.0:
            deliver += self.rng.uniform(0.0, self.jitter_s)
        if self.last_delivery is not None and deliver < self.last_delivery:
            deliver = self.last_delivery
        self.last_delivery = deliver
        return {"deliver_at": deliver, "start": start, "queue_s": queue_s}

    def note_ltp(self, seg):
        if seg is None:
            self.ltp_counts["non_ltp"] = self.ltp_counts.get("non_ltp", 0) + 1
            return False
        kind = seg["kind"]
        self.ltp_counts[kind] = self.ltp_counts.get(kind, 0) + 1
        if seg["type"] <= 7:
            key = (seg["eng"], seg["ses"], seg["off"], seg["dlen"])
            if key in self.data_seen:
                self.ltp_data_retransmissions += 1
                return True
            self.data_seen.add(key)
        return False

    def stats(self):
        out = {"listen": self.listen, "dest": self.dest, "delay_s": self.delay_s,
               "jitter_s": self.jitter_s, "loss": self.loss, "rate_Bps": self.rate_Bps,
               "queue_bytes": self.queue_bytes,
               "blackouts": [{"start_s": w[0], "end_s": w[1], "label": w[2], "kind": w[3]}
                             for w in self.blackouts]}
        out.update(self.counters)
        out["max_queue_delay_s"] = round(out["max_queue_delay_s"], 6)
        out["ltp_segments"] = dict(sorted(self.ltp_counts.items()))
        out["ltp_data_retransmissions"] = self.ltp_data_retransmissions
        return out


def load_config(path, t0):
    with open(path) as fh:
        cfg = json.load(fh)
    seed = cfg.get("seed", 0)
    links = []
    names = set()
    ports = set()
    for spec in cfg["links"]:
        if spec["name"] in names or spec["listen"] in ports:
            raise ValueError("duplicate link name or listen port: %s" % spec["name"])
        names.add(spec["name"])
        ports.add(spec["listen"])
        links.append(Link(spec["name"], spec["listen"], spec["dest"],
                          delay_s=spec.get("delay_s", 0.0), jitter_s=spec.get("jitter_s", 0.0),
                          loss=spec.get("loss", 0.0), rate_Bps=spec.get("rate_Bps"),
                          queue_bytes=spec.get("queue_bytes"),
                          blackouts=spec.get("blackouts", []), seed=seed, t0=t0))
    return cfg, links


# --------------------------------------------------------------------------
# Socket loop.
# --------------------------------------------------------------------------

class Clock:
    """Unix epoch seconds derived from the monotonic clock (no steps)."""

    def __init__(self):
        self.offset = time.time() - time.monotonic()

    def now(self):
        return time.monotonic() + self.offset


def run(cfg_path, t0, log_path, stats_path, ready_file=None):
    cfg, links = load_config(cfg_path, t0)
    clock = Clock()
    sel = selectors.DefaultSelector()
    for link in links:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4 * 1024 * 1024)
        s.bind((HOST, link.listen))
        s.setblocking(False)
        out = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        out.setblocking(False)
        link.out = out
        sel.register(s, selectors.EVENT_READ, link)

    log = open(log_path, "a", buffering=1 << 16) if log_path else None

    def emit(rec):
        if log is not None:
            log.write(json.dumps(rec, separators=(",", ":")) + "\n")

    running = True

    def stop(_sig, _frm):
        nonlocal running
        running = False

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)

    emit({"schema": SCHEMA, "ev": "start", "t_us": int(clock.now() * 1e6), "t0": t0,
          "pid": os.getpid(), "config": cfg})
    if ready_file:
        with open(ready_file, "w") as fh:
            fh.write("%d\n" % os.getpid())

    pending = []                      # heap of (deliver_at, seq, link, n, data)
    seq = 0
    last_flush = time.monotonic()
    while running:
        now = clock.now()
        while pending and pending[0][0] <= now:
            due, _, link, n, data = heapq.heappop(pending)
            try:
                link.out.sendto(data, (HOST, link.dest))
                link.counters["tx"] += 1
                link.counters["tx_bytes"] += len(data)
                emit({"ev": "tx", "t_us": int(clock.now() * 1e6), "link": link.name, "n": n,
                      "len": len(data), "due_us": int(due * 1e6)})
            except OSError as ex:      # peer udplsi not bound yet, ICMP refusal
                link.counters["send_error"] += 1
                emit({"ev": "drop", "t_us": int(clock.now() * 1e6), "link": link.name, "n": n,
                      "len": len(data), "reason": "send_error", "error": str(ex)})
        timeout = 0.05
        if pending:
            timeout = max(0.0, min(timeout, pending[0][0] - clock.now()))
        for key, _ in sel.select(timeout=timeout):
            link = key.data
            while True:
                try:
                    data = key.fileobj.recv(65535)
                except (BlockingIOError, InterruptedError):
                    break
                t = clock.now()
                seg = decode_ltp(data)
                retrans = link.note_ltp(seg)
                d = link.admit(t, len(data))
                rec = {"ev": "rx", "t_us": int(t * 1e6), "link": link.name, "n": link.n,
                       "len": len(data)}
                if seg is not None:
                    rec["ltp"] = seg
                    if retrans:
                        rec["ltp_retx"] = True
                emit(rec)
                if "drop" in d:
                    drec = {"ev": "drop", "t_us": int(t * 1e6), "link": link.name, "n": link.n,
                            "len": len(data), "reason": d["drop"]}
                    if "window" in d:
                        drec["window"] = d["window"]
                        drec["window_kind"] = d["window_kind"]
                    emit(drec)
                else:
                    seq += 1
                    heapq.heappush(pending, (d["deliver_at"], seq, link, link.n, data))
        if log is not None and time.monotonic() - last_flush > 0.5:
            log.flush()
            last_flush = time.monotonic()

    for _, _, link, _, _ in pending:
        link.counters["pending_at_stop"] += 1
    stats = {"schema": SCHEMA, "t0": t0, "stopped_us": int(clock.now() * 1e6),
             "seed": cfg.get("seed", 0), "links": {l.name: l.stats() for l in links}}
    emit({"ev": "stop", "t_us": int(clock.now() * 1e6)})
    if log is not None:
        log.close()
    text = json.dumps(stats, indent=1, sort_keys=True) + "\n"
    if stats_path:
        with open(stats_path, "w") as fh:
            fh.write(text)
    else:
        sys.stdout.write(text)
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0],
                                 formatter_class=argparse.RawDescriptionHelpFormatter,
                                 epilog=__doc__.split("\n\n", 1)[1])
    ap.add_argument("--config", required=True, help="link configuration (JSON)")
    ap.add_argument("--t0", type=float, required=True,
                    help="scenario time origin, Unix epoch seconds")
    ap.add_argument("--log", help="per-datagram event log (JSON Lines, appended)")
    ap.add_argument("--stats", help="per-link counters written here on exit")
    ap.add_argument("--ready-file", help="created once all sockets are bound")
    args = ap.parse_args(argv)
    return run(args.config, args.t0, args.log, args.stats, args.ready_file)


if __name__ == "__main__":
    sys.exit(main())
