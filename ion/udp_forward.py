#!/usr/bin/env python3
"""Fixed-delay UDP forwarder used by ion/selftest.sh in place of the link emulator.

Each --link NAME:LISTEN_PORT:DEST_PORT:DELAY_S opens a UDP socket on
127.0.0.1:LISTEN_PORT and re-sends every datagram it receives to
127.0.0.1:DEST_PORT after DELAY_S seconds. Links are one-directional, as LTP
link service output (udplso) sends each direction to its own port.

No loss, no rate limit, no reordering. On SIGTERM/SIGINT it writes per-link
datagram and byte counts as JSON to --stats (or stdout). With --log, it also
writes one JSON line per datagram received: {"t": unix time, "link": name,
"bytes": length}.

Example:
  udp_forward.py --link rover-gateway:29950:15013:0.25 --stats fwd.json
"""
import argparse
import heapq
import json
import selectors
import signal
import socket
import sys
import time

HOST = "127.0.0.1"


def parse_link(text):
    try:
        name, listen, dest, delay = text.split(":")
        return name, int(listen), int(dest), float(delay)
    except ValueError:
        raise argparse.ArgumentTypeError(
            f"bad --link {text!r}; expected NAME:LISTEN_PORT:DEST_PORT:DELAY_S")


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--link", action="append", type=parse_link, required=True)
    ap.add_argument("--stats", help="write per-link counters here on exit")
    ap.add_argument("--log", help="append one JSON line per received datagram")
    args = ap.parse_args()

    sel = selectors.DefaultSelector()
    out = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    stats = {}
    for name, listen, dest, delay in args.link:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4 * 1024 * 1024)
        s.bind((HOST, listen))
        s.setblocking(False)
        sel.register(s, selectors.EVENT_READ, (name, dest, delay))
        stats[name] = {"listen": listen, "dest": dest, "delay_s": delay,
                       "datagrams": 0, "bytes": 0}

    running = True

    def stop(_sig, _frm):
        nonlocal running
        running = False

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)

    log = open(args.log, "a", buffering=1) if args.log else None
    pending = []  # heap of (due_time, seq, dest_port, payload)
    seq = 0
    while running:
        now = time.monotonic()
        while pending and pending[0][0] <= now:
            _, _, dest, data = heapq.heappop(pending)
            out.sendto(data, (HOST, dest))
        timeout = max(0.0, pending[0][0] - now) if pending else 0.5
        for key, _ in sel.select(timeout=min(timeout, 0.5)):
            name, dest, delay = key.data
            while True:
                try:
                    data = key.fileobj.recv(65535)
                except BlockingIOError:
                    break
                st = stats[name]
                st["datagrams"] += 1
                st["bytes"] += len(data)
                seq += 1
                if log:
                    log.write(json.dumps({"t": round(time.time(), 6), "link": name,
                                          "bytes": len(data)}) + "\n")
                heapq.heappush(pending, (time.monotonic() + delay, seq, dest, data))

    if log:
        log.close()
    text = json.dumps(stats, indent=2, sort_keys=True)
    if args.stats:
        with open(args.stats, "w") as f:
            f.write(text + "\n")
    else:
        print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
