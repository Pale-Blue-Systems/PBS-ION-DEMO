"""Scenario definitions: loading and compilation into run-time inputs.

A scenario file (scenarios/S*.json) describes one run. Times are seconds of
scenario time: 0 is the traffic origin, the instant both endpoint plans
start. This module turns a scenario into

  - the ION contact plan (ionrc lines relative to the contact-plan reference
    time T0 = origin - lead_s; ion/start_nodes.sh prepends "@ T0"),
  - the link emulator configuration (emulator/linkemu.py; times relative to
    the origin),
  - the Endpoint A plan (endpoint-python/pbs_endpoint_a/plan.py format) and
    the Endpoint B plan (JSON lines, endpoint-c/README.md format).

Scenario keys:
  id, title, summary       text
  duration_s               traffic phase length; endpoints are stopped then
  seed                     emulator loss seed
  links                    per-direction emulator settings; key "*" applies to
                           all four directions, a direction name overrides it:
                           delay_s, jitter_s, loss, rate_Bps, queue_bytes
  contacts.rate_Bps        per-leg contact-plan rate (bytes/s), default from
                           ion/topology.json
  outages                  list of {leg, start_s, end_s, kind, label}; kind
                           "scheduled": contact-plan gap in both directions and
                           emulator blackout in both directions;
                           "unscheduled": emulator blackout only (ION is not told)
  rover.plan, rover.args   Endpoint A plan object and extra rover arguments
  earth.plan               Endpoint B plan items (list of objects)
  probes                   list of {at_s, node, tool} with tool "bplist" or
                           "bpstats", run on that ION node at that time
  criteria                 pass/fail checks (scenarios/evaluate.py)
  conformance              PBS-CONFORMANCE-01/02 items the scenario exercises
"""
import copy
import json
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOPOLOGY = os.path.join(ROOT, "ion", "topology.json")
LINK_KEYS = {"delay_s", "jitter_s", "loss", "rate_Bps", "queue_bytes"}
CONTACT_TAIL_S = 3600          # contacts continue this long after the traffic phase


def load_topology(path=TOPOLOGY):
    with open(path) as fh:
        return json.load(fh)


def load(path):
    with open(path) as fh:
        scn = json.load(fh)
    for key in ("id", "title", "duration_s"):
        if key not in scn:
            raise ValueError("%s: missing %r" % (path, key))
    scn.setdefault("seed", 1)
    scn.setdefault("links", {})
    scn.setdefault("contacts", {})
    scn.setdefault("outages", [])
    scn.setdefault("rover", {})
    scn.setdefault("earth", {})
    scn.setdefault("probes", [])
    scn.setdefault("criteria", [])
    scn.setdefault("conformance", [])
    scn.setdefault("lead_s", 10)
    scn.setdefault("live", True)
    topo = load_topology()
    for name, spec in scn["links"].items():
        if name != "*" and name not in topo["links"]:
            raise ValueError("%s: unknown link %r" % (path, name))
        bad = set(spec) - LINK_KEYS
        if bad:
            raise ValueError("%s: link %s: unknown keys %s" % (path, name, sorted(bad)))
    for o in scn["outages"]:
        if o["leg"] not in topo["legs"]:
            raise ValueError("%s: unknown leg %r" % (path, o["leg"]))
        if o.get("kind") not in ("scheduled", "unscheduled"):
            raise ValueError("%s: outage kind must be scheduled or unscheduled" % path)
        if not o["end_s"] > o["start_s"]:
            raise ValueError("%s: outage ends before it starts" % path)
        if o["kind"] == "scheduled" and (int(o["start_s"]) != o["start_s"] or
                                         int(o["end_s"]) != o["end_s"]):
            raise ValueError("%s: scheduled outage times must be whole seconds "
                             "(ION contact times are whole seconds)" % path)
    return scn


def link_settings(scn, topo, link):
    """Emulator settings of one direction after defaults and overrides."""
    leg = topo["legs"][topo["links"][link]["leg"]]
    out = {"delay_s": leg["one_way_delay_s"], "jitter_s": 0.0, "loss": 0.0,
           "rate_Bps": None, "queue_bytes": None}
    out.update(scn["links"].get("*", {}))
    out.update(scn["links"].get(link, {}))
    return out


def leg_rate(scn, topo, leg):
    return int(scn["contacts"].get("rate_Bps", {}).get(leg, topo["legs"][leg]["rate_Bps"]))


def contact_windows(scn, leg):
    """Scheduled contact windows of a leg in scenario time: [(start, end)]."""
    lead = scn["lead_s"]
    end = scn["duration_s"] + CONTACT_TAIL_S
    gaps = sorted((o["start_s"], o["end_s"]) for o in scn["outages"]
                  if o["leg"] == leg and o["kind"] == "scheduled")
    windows = []
    t = -lead
    for gs, ge in gaps:
        if gs > t:
            windows.append((t, gs))
        t = max(t, ge)
    if t < end:
        windows.append((t, end))
    return windows


def contact_plan(scn, topo):
    """ionrc text (relative times from T0 = origin - lead_s)."""
    lead = scn["lead_s"]
    lines = ["# Contact plan for scenario %s, generated by scenarios/scenario.py." % scn["id"],
             "# Times are seconds after the reference time T0 that ion/start_nodes.sh",
             "# writes as '@ <UTC>' above; scenario time 0 (traffic origin) is T0 + %d s." % lead,
             "# Rates in bytes per second; ranges in whole light seconds. A range whose",
             "# first node number is the lower one applies to both directions."]
    for leg_name, leg in sorted(topo["legs"].items()):
        a = topo["nodes"][leg["a"]]["node"]
        b = topo["nodes"][leg["b"]]["node"]
        rate = leg_rate(scn, topo, leg_name)
        for s, e in contact_windows(scn, leg_name):
            for frm, to in ((a, b), (b, a)):
                lines.append("a contact +%d +%d %d %d %d" % (s + lead, e + lead, frm, to, rate))
        lo, hi = sorted((a, b))
        lines.append("a range +0 +%d %d %d %d" % (scn["duration_s"] + CONTACT_TAIL_S + lead,
                                                  lo, hi, leg["range_s"]))
    return "\n".join(lines) + "\n"


def emulator_config(scn, topo):
    links = []
    for name, spec in sorted(topo["links"].items()):
        s = link_settings(scn, topo, name)
        blackouts = []
        for o in scn["outages"]:
            if o["leg"] != spec["leg"]:
                continue
            if "directions" in o and name not in o["directions"]:
                continue
            blackouts.append({"start_s": o["start_s"], "end_s": o["end_s"],
                              "label": o.get("label", ""), "kind": o["kind"]})
        links.append({"name": name, "listen": spec["listen"], "dest": spec["dest"],
                      "delay_s": s["delay_s"], "jitter_s": s["jitter_s"], "loss": s["loss"],
                      "rate_Bps": s["rate_Bps"], "queue_bytes": s["queue_bytes"],
                      "blackouts": blackouts})
    return {"seed": scn["seed"], "scenario": scn["id"], "links": links}


def rover_plan(scn):
    plan = copy.deepcopy(scn["rover"].get("plan", {"items": []}))
    plan.setdefault("description", "Endpoint A plan for scenario %s" % scn["id"])
    return plan


def earth_plan_lines(scn):
    out = ["# Endpoint B plan for scenario %s (generated by scenarios/scenario.py)." % scn["id"]]
    for item in scn["earth"].get("plan", []):
        out.append(json.dumps(item, separators=(",", ":")))
    return "\n".join(out) + "\n"


def outage_windows(scn):
    """All outages with the links they black out, for reports."""
    return [dict(o) for o in scn["outages"]]
