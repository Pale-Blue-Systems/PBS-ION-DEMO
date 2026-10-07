#!/usr/bin/env python3
"""Offline checks of the scenario definitions (no ION needed).

  python3 scenarios/test_scenarios.py -v

- ion/topology.json agrees with the udplso/udplsi ports in ion/nodes/*/node.ltprc;
- every scenarios/S*.json loads, compiles to a contact plan and an emulator
  configuration, and uses only known criteria checks;
- every Endpoint A plan expands with Endpoint A's own plan loader
  (endpoint-python/pbs_endpoint_a/plan.py);
- every Endpoint B plan passes Endpoint B's dry run (pbs_tool plan), when
  the tool is built (build/endpoint-c/pbs_tool or $PBS_EARTH_TOOL);
- scheduled outages produce contact-plan gaps, unscheduled ones do not;
- every traffic phase stays under 180 s;
- the no_unknown check with repeat_is_bp_reforward (S3-8) passes a repeated
  delivery only when it is the same bundle and ION re-forwarded a bundle.
"""
import glob
import json
import os
import re
import subprocess
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
import scenario as S  # noqa: E402
import evaluate  # noqa: E402

SCENARIOS = sorted(glob.glob(os.path.join(HERE, "S*.json")))
EARTH_TOOL = os.environ.get("PBS_EARTH_TOOL", os.path.join(ROOT, "build", "endpoint-c", "pbs_tool"))


class Topology(unittest.TestCase):
    def test_ports_match_ltprc(self):
        topo = S.load_topology()
        for name, node in topo["nodes"].items():
            text = "\n".join(l for l in open(os.path.join(ROOT, "ion", "nodes", name, "node.ltprc"))
                             if not l.startswith("#"))
            seat = re.search(r"a seat 'udplsi 127\.0\.0\.1:(\d+)'", text)
            self.assertEqual(int(seat.group(1)), node["udplsi_port"], name)
            for m in re.finditer(r"a span (\d+) .*'udplso 127\.0\.0\.1:(\d+)'", text):
                peer, port = int(m.group(1)), int(m.group(2))
                link = [l for l in topo["links"].values()
                        if topo["nodes"][l["from"]]["node"] == node["node"] and
                        topo["nodes"][l["to"]]["node"] == peer]
                self.assertEqual(len(link), 1, (name, peer))
                self.assertEqual(link[0]["listen"], port, (name, peer))
                self.assertEqual(link[0]["dest"], topo["nodes"][link[0]["to"]]["udplsi_port"])

    def test_ranges_cover_delays(self):
        topo = S.load_topology()
        for name, leg in topo["legs"].items():
            self.assertIsInstance(leg["range_s"], int)
            self.assertGreaterEqual(leg["range_s"], leg["one_way_delay_s"], name)


class Scenarios(unittest.TestCase):
    def test_found(self):
        self.assertEqual([os.path.basename(p) for p in SCENARIOS],
                         ["S%d.json" % i for i in range(1, 8)])

    def test_compile_and_checks(self):
        topo = S.load_topology()
        for path in SCENARIOS:
            scn = S.load(path)
            self.assertLess(scn["duration_s"], 180, path)
            plan = S.contact_plan(scn, topo)
            self.assertIn("a contact", plan)
            emu = S.emulator_config(scn, topo)
            self.assertEqual(len(emu["links"]), 4)
            ids = set()
            for c in scn["criteria"]:
                self.assertTrue(hasattr(evaluate.Evaluator, "c_" + c["check"]), (path, c))
                self.assertNotIn(c["id"], ids)
                ids.add(c["id"])
            self.assertTrue(scn["conformance"], path)

    def test_scheduled_outage_is_a_contact_gap(self):
        topo = S.load_topology()
        for path in SCENARIOS:
            scn = S.load(path)
            for o in scn["outages"]:
                windows = S.contact_windows(scn, o["leg"])
                inside = [w for w in windows if w[0] < o["end_s"] and w[1] > o["start_s"]]
                if o["kind"] == "scheduled":
                    self.assertEqual(inside, [], (path, o))
                else:
                    self.assertTrue(inside, (path, o))
                emu = S.emulator_config(scn, topo)
                links = [l for l in emu["links"] if topo["links"][l["name"]]["leg"] == o["leg"]]
                for l in links:
                    self.assertIn(o["start_s"], [b["start_s"] for b in l["blackouts"]])

    def test_rover_plans_expand(self):
        sys.path.insert(0, os.path.join(ROOT, "endpoint-python"))
        from pbs_endpoint_a import plan as A_plan
        for path in SCENARIOS:
            scn = S.load(path)
            sends = A_plan.expand(S.rover_plan(scn))
            self.assertTrue(sends, path)
            self.assertLess(max(s.at_s for s in sends), scn["duration_s"], path)

    @unittest.skipUnless(os.access(EARTH_TOOL, os.X_OK), "pbs_tool not built")
    def test_earth_plans_dry_run(self):
        for path in SCENARIOS:
            scn = S.load(path)
            text = "".join(json.dumps(i) + "\n" for i in scn["earth"]["plan"])
            r = subprocess.run([EARTH_TOOL, "plan", "--now-us", "1791338844000000"], input=text,
                               capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, (path, r.stderr))
            recs = [json.loads(l) for l in r.stdout.splitlines() if l.strip()]
            n = sum(int(i.get("count", 1)) for i in scn["earth"]["plan"])
            self.assertEqual(len(recs), n, path)
            for i in scn["earth"]["plan"]:
                self.assertLess(i["at_s"] + (i.get("count", 1) - 1) * i.get("interval_s", 1),
                                scn["duration_s"], path)


class RepeatedDelivery(unittest.TestCase):
    """no_unknown with repeat_is_bp_reforward on a synthetic run directory."""

    SRC = "526f7665722d416c7068610000000000"

    def make_run(self, d, recvs, rfw):
        os.makedirs(os.path.join(d, "ion"))
        crit = {"id": "X-1", "check": "no_unknown", "repeat_is_bp_reforward": True, "text": "t"}
        json.dump({"id": "X", "criteria": [crit]}, open(os.path.join(d, "scenario.json"), "w"))
        json.dump({"scenario": "X", "origin_unix_s": 1000.0}, open(os.path.join(d, "run.json"), "w"))
        send = {"event": "send", "t_us": 1000000000, "endpoint": "A", "source_id_hex": self.SRC,
                "sequence": 1, "timestamp_us": 1000000000, "result": "SENT", "reason": "OK",
                "envelope_sha256": "aa", "header_hex": "h", "priority": 4}
        with open(os.path.join(d, "endpoint-a.jsonl"), "w") as fh:
            fh.write(json.dumps(send) + "\n")
        with open(os.path.join(d, "endpoint-b.jsonl"), "w") as fh:
            for i, (sha, created) in enumerate(recvs):
                fh.write(json.dumps({
                    "event": "recv", "t_us": 1001600000 + i * 600000, "endpoint": "B",
                    "source_id_hex": self.SRC, "sequence": 1, "timestamp_us": 1000000000,
                    "result": "ACCEPT", "reason": "OK", "envelope_sha256": sha, "header_hex": "h",
                    "bundle_source_eid": "ipn:99.1", "bundle_creation_ms": created,
                    "bundle_creation_count": 0}) + "\n")
        with open(os.path.join(d, "ion", "gateway.ion.log"), "w") as fh:
            fh.write("[2026/10/07-04:25:05] [x] rfw from 1970/01/01-00:00:00 to "
                     "2026/10/07-04:25:05: (0) %d 0 (1) 0 0 (2) 0 0 (+) %d 0\n" % (rfw, rfw))
        return evaluate.Evaluator(d).evaluate()["criteria"][0]

    def check(self, recvs, rfw):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            return self.make_run(os.path.join(d, "X"), recvs, rfw)

    def test_single_delivery_passes(self):
        self.assertTrue(self.check([("aa", 5)], 0)["passed"])

    def test_same_bundle_reforwarded_passes(self):
        r = self.check([("aa", 5), ("aa", 5)], 1)
        self.assertTrue(r["passed"], r)
        self.assertIn("1 further deliveries", r["measured"])

    def test_repeat_without_reforward_fails(self):
        self.assertFalse(self.check([("aa", 5), ("aa", 5)], 0)["passed"])

    def test_repeat_of_another_bundle_fails(self):
        self.assertFalse(self.check([("aa", 5), ("aa", 9)], 1)["passed"])

    def test_unknown_envelope_fails(self):
        self.assertFalse(self.check([("aa", 5), ("bb", 5)], 1)["passed"])


if __name__ == "__main__":
    unittest.main()
