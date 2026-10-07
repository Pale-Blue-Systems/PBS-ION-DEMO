#!/usr/bin/env python3
"""Unit and socket tests for emulator/linkemu.py (standard library only).

  python3 emulator/test_linkemu.py -v
"""
import json
import os
import signal
import socket
import subprocess
import sys
import tempfile
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import linkemu  # noqa: E402


def sdnv(v):
    out = [v & 0x7F]
    v >>= 7
    while v:
        out.append(0x80 | (v & 0x7F))
        v >>= 7
    return bytes(reversed(out))


def ltp_data(typ, eng, ses, off, data, cp=None, rsn=None, client=1):
    seg = bytes([typ]) + sdnv(eng) + sdnv(ses) + b"\x00" + sdnv(client) + sdnv(off) + sdnv(len(data))
    if typ in (1, 2, 3):
        seg += sdnv(cp) + sdnv(rsn)
    return seg + data


class DecodeLtp(unittest.TestCase):
    def test_red_data_checkpoint(self):
        seg = ltp_data(3, 99, 300, 1024, b"x" * 40, cp=7, rsn=0)
        d = linkemu.decode_ltp(seg)
        self.assertEqual(d, {"type": 3, "kind": "CP", "eng": 99, "ses": 300, "off": 1024,
                             "dlen": 40, "cp": 7, "rsn": 0})

    def test_red_data_plain(self):
        d = linkemu.decode_ltp(ltp_data(0, 50, 1, 0, b"y" * 10))
        self.assertEqual((d["kind"], d["eng"], d["ses"], d["off"], d["dlen"]), ("DS", 50, 1, 0, 10))

    def test_report_and_ack(self):
        rs = bytes([8]) + sdnv(99) + sdnv(12) + b"\x00" + sdnv(5) + sdnv(7) + sdnv(2048) + \
            sdnv(0) + sdnv(1) + sdnv(0) + sdnv(2048)
        d = linkemu.decode_ltp(rs)
        self.assertEqual((d["kind"], d["rs"], d["cp"], d["ub"], d["lb"], d["claims"]),
                         ("RS", 5, 7, 2048, 0, 1))
        ra = bytes([9]) + sdnv(99) + sdnv(12) + b"\x00" + sdnv(5)
        self.assertEqual(linkemu.decode_ltp(ra)["kind"], "RA")

    def test_header_extension_skipped(self):
        seg = bytes([0]) + sdnv(23) + sdnv(4) + b"\x10" + b"\x00" + sdnv(3) + b"abc" + \
            sdnv(1) + sdnv(0) + sdnv(2) + b"zz"
        d = linkemu.decode_ltp(seg)
        self.assertEqual((d["eng"], d["ses"], d["off"], d["dlen"]), (23, 4, 0, 2))

    def test_not_ltp(self):
        self.assertIsNone(linkemu.decode_ltp(b"\xff\x00\x01\x02"))
        self.assertIsNone(linkemu.decode_ltp(b"\x00"))
        self.assertIsNone(linkemu.decode_ltp(b"\x00\xff\xff\xff\xff\xff\xff\xff\xff\xff\xff\xff"))


class LinkModel(unittest.TestCase):
    def test_delay_only(self):
        link = linkemu.Link("a-b", 1, 2, delay_s=1.3)
        d = link.admit(100.0, 500)
        self.assertAlmostEqual(d["deliver_at"], 101.3)
        self.assertEqual(d["queue_s"], 0.0)

    def test_rate_serializes_back_to_back(self):
        link = linkemu.Link("a-b", 1, 2, delay_s=0.25, rate_Bps=1000)
        d1 = link.admit(10.0, 500)            # 0.5 s on the wire
        d2 = link.admit(10.0, 500)
        self.assertAlmostEqual(d1["deliver_at"], 10.0 + 0.5 + 0.25)
        self.assertAlmostEqual(d2["start"], 10.5)
        self.assertAlmostEqual(d2["queue_s"], 0.5)
        self.assertAlmostEqual(d2["deliver_at"], 10.0 + 1.0 + 0.25)
        d3 = link.admit(20.0, 100)            # link idle again
        self.assertAlmostEqual(d3["start"], 20.0)

    def test_queue_limit_drop_tail(self):
        link = linkemu.Link("a-b", 1, 2, rate_Bps=1000, queue_bytes=1500)
        self.assertIn("deliver_at", link.admit(0.0, 1000))
        self.assertIn("deliver_at", link.admit(0.0, 500))
        d = link.admit(0.0, 100)
        self.assertEqual(d["drop"], "queue_full")
        self.assertEqual(link.counters["drop_queue_full"], 1)
        self.assertIn("deliver_at", link.admit(1.0, 100))   # 500 B left in the queue

    def test_blackout_window_edges(self):
        link = linkemu.Link("a-b", 1, 2, delay_s=0.1, t0=1000.0,
                            blackouts=[{"start_s": 10, "end_s": 20, "label": "occultation"}])
        self.assertIn("deliver_at", link.admit(1009.999, 10))
        d = link.admit(1010.0, 10)
        self.assertEqual((d["drop"], d["window"], d["window_kind"]),
                         ("blackout", "occultation", "unscheduled"))
        self.assertEqual(link.admit(1019.999, 10)["drop"], "blackout")
        self.assertIn("deliver_at", link.admit(1020.0, 10))

    def test_blackout_applies_at_transmission_start(self):
        # Queued before the window, transmitted inside it: lost.
        link = linkemu.Link("a-b", 1, 2, rate_Bps=100, t0=0.0,
                            blackouts=[{"start_s": 1.0, "end_s": 2.0}])
        self.assertIn("deliver_at", link.admit(0.0, 100))    # 0.0 .. 1.0
        self.assertEqual(link.admit(0.0, 100)["drop"], "blackout")   # starts at 1.0
        self.assertIn("deliver_at", link.admit(0.0, 100))    # starts at 2.0

    def test_loss_is_seeded_and_near_rate(self):
        def pattern(seed):
            link = linkemu.Link("a-b", 1, 2, loss=0.05, seed=seed)
            return [("drop" in link.admit(float(i), 100)) for i in range(20000)]
        p1, p2, p3 = pattern(7), pattern(7), pattern(8)
        self.assertEqual(p1, p2)
        self.assertNotEqual(p1, p3)
        self.assertTrue(0.045 < sum(p1) / len(p1) < 0.055, sum(p1) / len(p1))

    def test_loss_independent_of_other_links(self):
        a = linkemu.Link("a-b", 1, 2, loss=0.5, seed=1)
        b = linkemu.Link("b-a", 3, 4, loss=0.5, seed=1)
        pa = [("drop" in a.admit(float(i), 1)) for i in range(200)]
        pb = [("drop" in b.admit(float(i), 1)) for i in range(200)]
        self.assertNotEqual(pa, pb)

    def test_jitter_keeps_order(self):
        link = linkemu.Link("a-b", 1, 2, delay_s=1.0, jitter_s=0.5, seed=3)
        last = 0.0
        for i in range(2000):
            d = link.admit(i * 0.001, 10)["deliver_at"]
            self.assertGreaterEqual(d, last)
            self.assertGreaterEqual(d, i * 0.001 + 1.0)
            self.assertLessEqual(d, i * 0.001 + 1.5 + 0.001 * 2000)
            last = d

    def test_retransmission_counter(self):
        link = linkemu.Link("a-b", 1, 2)
        seg = linkemu.decode_ltp(ltp_data(1, 99, 5, 0, b"a" * 8, cp=1, rsn=0))
        self.assertFalse(link.note_ltp(seg))
        self.assertTrue(link.note_ltp(seg))
        self.assertEqual(link.ltp_data_retransmissions, 1)
        self.assertEqual(link.ltp_counts["CP"], 2)

    def test_bad_config(self):
        with self.assertRaises(ValueError):
            linkemu.Link("x", 1, 2, loss=1.5)
        with self.assertRaises(ValueError):
            linkemu.Link("x", 1, 2, blackouts=[{"start_s": 5, "end_s": 5}])


class SocketRun(unittest.TestCase):
    """Runs linkemu.py as a process on two high ports and measures it."""

    def test_forwarding_delay_loss_and_log(self):
        tmp = tempfile.mkdtemp(prefix="linkemu-test-")
        sink_a = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sink_a.bind(("127.0.0.1", 0))
        sink_b = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sink_b.bind(("127.0.0.1", 0))
        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        probe.bind(("127.0.0.1", 0))
        for s in (sink_a, sink_b, probe):
            self.addCleanup(s.close)
        free = []
        for _ in range(2):                       # two free ports for the emulator
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.bind(("127.0.0.1", 0))
            free.append(s.getsockname()[1])
            s.close()
        t0 = time.time()
        cfg = {"seed": 1, "links": [
            {"name": "x-y", "listen": free[0], "dest": sink_a.getsockname()[1], "delay_s": 0.2},
            {"name": "y-x", "listen": free[1], "dest": sink_b.getsockname()[1], "delay_s": 0.05,
             "loss": 1.0}]}
        with open(os.path.join(tmp, "cfg.json"), "w") as fh:
            json.dump(cfg, fh)
        ready = os.path.join(tmp, "ready")
        proc = subprocess.Popen([sys.executable, os.path.join(HERE, "linkemu.py"),
                                 "--config", os.path.join(tmp, "cfg.json"), "--t0", str(t0),
                                 "--log", os.path.join(tmp, "events.jsonl"),
                                 "--stats", os.path.join(tmp, "stats.json"),
                                 "--ready-file", ready])
        try:
            for _ in range(100):
                if os.path.exists(ready):
                    break
                time.sleep(0.05)
            self.assertTrue(os.path.exists(ready))
            seg = ltp_data(3, 99, 1, 0, b"hello", cp=1, rsn=0)
            sent = time.time()
            probe.sendto(seg, ("127.0.0.1", free[0]))
            probe.sendto(b"lost", ("127.0.0.1", free[1]))
            sink_a.settimeout(2.0)
            data, _ = sink_a.recvfrom(65535)
            elapsed = time.time() - sent
            self.assertEqual(data, seg)
            self.assertGreaterEqual(elapsed, 0.2)
            self.assertLess(elapsed, 0.3)
            sink_b.settimeout(0.3)
            with self.assertRaises(socket.timeout):
                sink_b.recvfrom(65535)
        finally:
            proc.send_signal(signal.SIGTERM)
            proc.wait(5)
        stats = json.load(open(os.path.join(tmp, "stats.json")))
        self.assertEqual(stats["links"]["x-y"]["tx"], 1)
        self.assertEqual(stats["links"]["x-y"]["ltp_segments"], {"CP": 1})
        self.assertEqual(stats["links"]["y-x"]["drop_loss"], 1)
        events = [json.loads(l) for l in open(os.path.join(tmp, "events.jsonl"))]
        kinds = [(e["ev"], e.get("link")) for e in events]
        self.assertEqual(kinds[0][0], "start")
        self.assertIn(("rx", "x-y"), kinds)
        self.assertIn(("tx", "x-y"), kinds)
        self.assertIn(("drop", "y-x"), kinds)
        rx = next(e for e in events if e["ev"] == "rx" and e["link"] == "x-y")
        tx = next(e for e in events if e["ev"] == "tx" and e["link"] == "x-y")
        self.assertEqual(rx["ltp"]["kind"], "CP")
        self.assertGreaterEqual(tx["t_us"] - rx["t_us"], 200000)
        self.assertLess(tx["t_us"] - rx["t_us"], 210000)


if __name__ == "__main__":
    unittest.main()
