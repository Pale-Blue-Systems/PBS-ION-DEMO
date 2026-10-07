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
- every traffic phase stays under 180 s.
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


if __name__ == "__main__":
    unittest.main()
