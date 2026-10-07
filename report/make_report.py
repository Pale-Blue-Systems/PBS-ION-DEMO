#!/usr/bin/env python3
"""Write REPORT.md from the scenario results.

  report/make_report.py [--results results] [--out REPORT.md]

For every results/<S>/ directory with a run.json, reads the logs through
report/collect.py and the verdict written by scenarios/evaluate.py (it runs
the evaluation if verdict.json is missing), then writes

  REPORT.md                    conditions, pass/fail per criterion, measurements
  results/summary.json         verdicts and headline numbers per scenario
  results/all-envelopes.csv    envelopes.csv of every scenario
  results/charts/*.png         charts (only when matplotlib can be imported)

Every number in REPORT.md is computed from the files of the run it describes.
"""
import argparse
import csv
import datetime
import hashlib
import json
import os
import statistics
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(ROOT, "scenarios"))
import charts  # noqa: E402
import collect  # noqa: E402

PRIO = collect.PRIO_NAME
PROPAGATION_S = {"A->B": 1.55, "B->A": 1.55}       # 0.25 + 1.3 s one way (ion/topology.json)


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def q(values, p):
    if not values:
        return None
    v = sorted(values)
    k = max(0, min(len(v) - 1, int(round(p * (len(v) - 1)))))
    return v[k]


def fs(v, nd=3):
    return "n/a" if v is None else ("%.*f" % (nd, v))


def md_escape(s):
    return str(s).replace("|", "\\|").replace("\n", " ")


def table(header, rows):
    out = ["| " + " | ".join(header) + " |", "|" + "---|" * len(header)]
    for r in rows:
        out.append("| " + " | ".join(md_escape(x) for x in r) + " |")
    return "\n".join(out)


def latency_rows(rows):
    out = []
    for direction in ("A->B", "B->A"):
        for p in range(5):
            sel = [r for r in rows if r["dir"] == direction and r["priority"] == p and
                   r["send_result"] == "SENT"]
            if not sel:
                continue
            lat = [r["latency_s"] for r in sel if r["delivered"]]
            out.append([direction, PRIO[p], len(sel), len(lat),
                        "%.3f" % (len(lat) / len(sel)), fs(q(lat, 0.0)), fs(q(lat, 0.5)),
                        fs(q(lat, 0.95)), fs(q(lat, 1.0))])
    return out


def outage_rows(run):
    """Envelopes sent while a leg on their path was down."""
    out = []
    rows = run.envelopes
    for o in run.scn.get("outages", []):
        sel = [r for r in rows if r["send_result"] == "SENT" and r["send_t_s"] is not None and
               o["start_s"] <= r["send_t_s"] < o["end_s"]]
        for direction in ("A->B", "B->A"):
            d = [r for r in sel if r["dir"] == direction]
            if not d:
                continue
            got = [r for r in d if r["delivered"]]
            held = [r["latency_s"] - PROPAGATION_S[direction] for r in got]
            after = [r["recv_t_s"] - o["end_s"] for r in got]
            topo = collect_topology()
            leg_links = [n for n, l in topo["links"].items() if l["leg"] == o["leg"]]
            first = run.first_rx_after(leg_links, o["end_s"])
            out.append(["%s %g-%g s (%s)" % (o["leg"], o["start_s"], o["end_s"], o["kind"]),
                        direction, len(d), len(got), fs(q(held, 0.0)), fs(q(held, 0.5)),
                        fs(q(held, 1.0)), fs(None if first is None else first - o["end_s"]),
                        fs(q(after, 0.0)), fs(q(after, 1.0))])
    return out


def resume_rows(run):
    """LTP blocks started in the 3 s after a leg resumed (emulator view)."""
    topo = collect_topology()
    out = []
    for o in run.scn.get("outages", []):
        leg_links = [n for n, l in topo["links"].items() if l["leg"] == o["leg"]]
        first = run.first_rx_after(leg_links, o["end_s"])
        if first is None:
            continue
        for data_link in leg_links:
            spec = topo["links"][data_link]
            report_link = [n for n, l in topo["links"].items()
                           if l["from"] == spec["to"] and l["to"] == spec["from"]][0]
            blocks = run.ltp_after_resume(data_link, report_link, first, first + 3.0)
            if not blocks:
                continue
            resent = sum(1 for b in blocks if b["cp_resent_before_report"])
            rep = [b["report_after_s"] for b in blocks if b["report_after_s"] is not None]
            out.append(["%s %g-%g s (%s)" % (o["leg"], o["start_s"], o["end_s"], o["kind"]),
                        data_link, fs(first - o["end_s"]), len(blocks), resent,
                        fs(q(rep, 0.0)), fs(q(rep, 0.5)), fs(q(rep, 1.0))])
    return out


def link_rows(run):
    out = []
    for r in run.link_rows():
        out.append([r["link"], r["delay_s"], r["loss"], r["rate_Bps"] or "none",
                    r["queue_bytes"] or "none", r["blackouts"] or "none", r["rx"], r["tx"],
                    r["drop_loss"], r["drop_blackout"], r["drop_queue_full"],
                    r["ltp_DS"] + r["ltp_CP"], r["ltp_RS"], r["ltp_cancel"],
                    r["ltp_data_retransmissions"], r["max_queue_delay_s"]])
    return out


def conditions(run):
    scn = run.scn
    info = run.run
    lines = []
    lines.append("- Traffic phase: %s s after the traffic origin; contact-plan reference time "
                 "T0 = origin - %s s (%s UTC); traffic origin %s UTC." % (
                     scn.get("duration_s"), scn.get("lead_s", 10), info.get("t0_utc"),
                     datetime.datetime.fromtimestamp(info["origin_unix_s"], datetime.timezone.utc)
                     .strftime("%Y-%m-%d %H:%M:%S") if info.get("origin_unix_s") else "n/a"))
    ea = info.get("endpoint_a", {})
    if ea:
        lines.append("- Endpoint A plan origin: %+.3f s from the traffic origin (bin/rover "
                     "start-up %.3f s); Endpoint B plan origin: the traffic origin "
                     "(`--t0-us`)." % (ea.get("plan_origin_offset_s", 0), ea.get("startup_s", 0)))
    topo = collect_topology()
    legs = []
    for name, leg in topo["legs"].items():
        rate = scn.get("contacts", {}).get("rate_Bps", {}).get(name, leg["rate_Bps"])
        legs.append("%s: one-way delay %s s (emulator), contact-plan range %s s, contact rate "
                    "%s bytes/s" % (name, leg["one_way_delay_s"], leg["range_s"], rate))
    lines.append("- Legs: " + "; ".join(legs) + ".")
    late = run.emulator_timing()["lateness_ms"]
    if late:
        lines.append("- Emulator forwarding accuracy: %d datagrams forwarded; send time minus "
                     "scheduled time median %.3f ms, p99 %.3f ms, max %.3f ms." % (
                         len(late), q(late, 0.5), q(late, 0.99), late[-1]))
    links = scn.get("links", {})
    if links:
        lines.append("- Emulator settings beyond the delays: " + "; ".join(
            "%s: %s" % (k, ", ".join("%s=%s" % kv for kv in v.items())) for k, v in links.items()) +
            " (`*` = all four directions); loss seed %s." % scn.get("seed"))
    else:
        lines.append("- Emulator: delay only; no loss, no rate limit.")
    for o in scn.get("outages", []):
        lines.append("- Outage %s s to %s s on %s (%s): %s." % (
            o["start_s"], o["end_s"], o["leg"], o["kind"], o.get("label", "")))
    if not scn.get("outages"):
        lines.append("- No outage.")
    counts = {}
    for r in run.envelopes:
        k = (r["dir"], r["priority"])
        counts[k] = counts.get(k, 0) + 1
    mix = []
    for direction in ("A->B", "B->A"):
        parts = ["%s %d" % (PRIO.get(p, p), counts[(direction, p)]) for p in range(5)
                 if (direction, p) in counts]
        if parts:
            mix.append("%s: %s" % (direction, ", ".join(parts)))
    lines.append("- Envelopes built (send records, including not sent): " + "; ".join(mix) + ".")
    return "\n".join(lines)


_TOPO = None


def collect_topology():
    global _TOPO
    if _TOPO is None:
        with open(os.path.join(ROOT, "ion", "topology.json")) as fh:
            _TOPO = json.load(fh)
    return _TOPO


def s5_rows(run):
    out = []
    labels = []
    for r in run.envelopes:
        if r["label"] and r["label"].startswith("S5-") and r["label"] not in labels:
            labels.append(r["label"])
    for lab in labels:
        for direction in ("A->B", "B->A"):
            sel = [r for r in run.envelopes if r["label"] == lab and r["dir"] == direction]
            if not sel:
                continue
            r0 = sel[0]
            res = {}
            for r in sel:
                if r["send_result"] != "SENT":
                    k = "NOT_SENT %s" % r["send_reason"]
                elif r["delivered"]:
                    k = "%s %s" % (r["recv_result"], r["recv_reason"])
                else:
                    k = "not delivered"
                res[k] = res.get(k, 0) + 1
            out.append([lab, direction, r0["ttl_s"], ", ".join(sorted({str(r["lifespan_s"]) for r in sel})),
                        r0["lifespan_rule"] or "", len(sel), "; ".join("%s x%d" % kv for kv in res.items())])
    return out


def s6_rows(run):
    out = []
    for r in run.envelopes:
        if r["dir"] != "B->A" or not (r["label"] or "").startswith("S6-"):
            continue
        have = "%s %s" % (r["recv_result"], r["recv_reason"]) if r["delivered"] else "not delivered"
        out.append([r["label"], r["commands"] or "", r["suite_id"] or "", r["key_id"] or "",
                    r["security_sequence"] if r["security_sequence"] is not None else "",
                    have, r["exec_count"], fs(r["latency_s"])])
    return out


def probe_rows(run):
    out = []
    for p in run.probes():
        if "bundles" not in p:
            continue
        labels = {}
        for b in p["bundles"]:
            k = b.get("label") or "(not a demo envelope)"
            labels[k] = labels.get(k, 0) + 1
        out.append([p.get("label", ""), p["node"], fs(p.get("scenario_t_s"), 1), p["count"],
                    "; ".join("%s x%d" % kv for kv in sorted(labels.items())) or "none"])
    return out


def ionstat_rows(run):
    out = []
    for node, st in run.ion_stats().items():
        def g(cls):
            return st.get(cls, {}).get("+", "n/a")
        out.append([node, g("src"), g("fwd"), g("xmt"), g("rcv"), g("dlv"), g("rfw"), g("exp")])
    return out


def ltpstat_rows(run):
    names = {99: "rover", 50: "gateway", 23: "earth"}
    out = []
    for node, spans in run.ltp_stats().items():
        for peer, st in sorted(spans.items()):
            out.append(["%s to %s" % (node, names.get(peer, peer)),
                        st.get("ckpt_xmit", "n/a"), st.get("ckpt_re_xmit", "n/a"),
                        st.get("seg_re_xmit", "n/a"), st.get("export_cancel_xmit", "n/a"),
                        st.get("export_complete", "n/a"), st.get("import_complete", "n/a"),
                        st.get("rpt_re_xmit", "n/a"), st.get("in_seg_redundant", "n/a"),
                        st.get("in_seg_screened", "n/a")])
    return out


def scenario_section(run, verdict, chart_paths, results_rel, report_dir):
    scn = run.scn
    sid = run.id
    lines = ["## %s: %s" % (sid, scn.get("title", "")), ""]
    lines.append("Result: **%s** (%d of %d criteria passed)%s." % (
        "PASS" if verdict["passed"] else "FAIL", verdict["criteria_passed"],
        verdict["criteria_total"],
        "" if verdict["run_completed"] else "; the run did not complete: %s" % verdict["run_error"]))
    lines.append("")
    lines.append(scn.get("summary", ""))
    lines.append("")
    lines.append("### Conditions")
    lines.append("")
    lines.append(conditions(run))
    lines.append("")
    lines.append("### Criteria")
    lines.append("")
    rows = []
    for c in verdict["criteria"]:
        rows.append([c["id"], c["text"], c["measured"],
                     "info" if c["informative"] else ("PASS" if c["passed"] else "FAIL")])
    lines.append(table(["ID", "Criterion", "Measured", "Result"], rows))
    lines.append("")
    details = [c for c in verdict["criteria"] if c["details"] and (c["check"] == "expect_by_label")]
    for c in details:
        if sid == "S6":
            continue
        lines.append("%s detail:" % c["id"])
        lines.append("")
        lines.append("```")
        lines.extend(c["details"])
        lines.append("```")
        lines.append("")
    failed = [c for c in verdict["criteria"] if not c["passed"] and not c["informative"] and c["details"]]
    for c in failed:
        lines.append("%s failures:" % c["id"])
        lines.append("")
        lines.append("```")
        lines.extend(c["details"][:30])
        lines.append("```")
        lines.append("")
    lines.append("### Measurements")
    lines.append("")
    lines.append("Delivery and latency per direction and priority (latency = receive time minus "
                 "envelope Timestamp, both from the host clock):")
    lines.append("")
    lines.append(table(["Direction", "Priority", "Sent", "Received", "Delivery ratio",
                        "Min (s)", "Median (s)", "p95 (s)", "Max (s)"], latency_rows(run.envelopes)))
    lines.append("")
    orow = outage_rows(run)
    if orow:
        lines.append("Envelopes sent during an outage. Time held = latency minus the 1.55 s "
                     "one-way propagation of the path. Leg resumed = first datagram the emulator "
                     "received on either direction of the leg after the outage ended, minus the "
                     "end time. Delivered after = receive time minus the end of the outage:")
        lines.append("")
        lines.append(table(["Outage", "Direction", "Sent", "Received", "Time held min (s)",
                            "median (s)", "max (s)", "Leg resumed after (s)",
                            "Delivered after, first (s)", "last (s)"], orow))
        lines.append("")
    rrow = resume_rows(run)
    if rrow:
        lines.append("LTP blocks whose first checkpoint segment the emulator forwarded in the "
                     "3 s after the leg resumed; a block counts as resent when the sender "
                     "transmitted its checkpoint again before any report segment for it came "
                     "back; report delay = first report minus first checkpoint (emulator "
                     "receive times):")
        lines.append("")
        lines.append(table(["Outage", "Data direction", "Leg resumed after (s)", "Blocks",
                            "Checkpoint resent before a report", "Report delay min (s)",
                            "median (s)", "max (s)"], rrow))
        lines.append("")
    prow = probe_rows(run)
    if prow:
        lines.append("Bundles held in ION storage (`bplist` on the node during the run; each "
                     "bundle's payload identified as a demo envelope by Sequence and Timestamp):")
        lines.append("")
        lines.append(table(["Probe", "Node", "At (s)", "Bundles", "Envelopes by label"], prow))
        lines.append("")
    if sid == "S5":
        lines.append("Expiry outcomes by label (lifetime = bundle lifetime passed to ION, "
                     "DTN-MAP-01-6.1 = computed from the TTL, OVERRIDE = fault injection):")
        lines.append("")
        lines.append(table(["Label", "Direction", "TTL (s)", "Lifetime (s)", "Lifetime rule",
                            "Envelopes", "Outcome"], s5_rows(run)))
        lines.append("")
    if sid == "S6":
        lines.append("Command dispositions at the rover (Endpoint A), in send order. Latency is "
                     "measured from the envelope Timestamp: a replay carries the Timestamp of its "
                     "original, and the Timestamp-tamper case carries a Timestamp 10 s in the "
                     "future, so its latency is negative:")
        lines.append("")
        lines.append(table(["Label", "Command", "Suite", "key_id", "security_sequence",
                            "Rover result", "Executed", "Latency (s)"], s6_rows(run)))
        lines.append("")
    if sid == "S7":
        p = os.path.join(run.dir, "vector-comparison.json")
        if os.path.exists(p):
            v = json.load(open(p))
            lines.append("Offline comparison (`scenarios/s7_compare.py`, docs/test-vectors.json "
                         "SHA-256 `%s`):" % v["vectors_sha256"])
            lines.append("")
            lines.append(table(["Item kind", "Items", "A == B", "A == expected", "B == expected"],
                               [[k, x["items"], x["a_eq_b"], x["a_ok"], x["b_ok"]]
                                for k, x in v["summary"].items()]))
            lines.append("")
        for name in ("a-crosscheck", "b-crosscheck"):
            p = os.path.join(run.dir, name + ".out")
            if os.path.exists(p):
                text = open(p).read().strip()
                lines.append("`%s` output (%s):" % (name, "Endpoint A `bin/tool crosscheck`" if
                                                    name.startswith("a") else
                                                    "Endpoint B `pbs_tool crosscheck`"))
                lines.append("")
                lines.append("```")
                lines.append(text[-1500:])
                lines.append("```")
                lines.append("")
    lines.append("Emulated links (counters from `emulator-stats.json`; LTP segment types decoded "
                 "from each datagram; a data retransmission is a data segment whose session, "
                 "offset and length the emulator had already seen on that link):")
    lines.append("")
    lines.append(table(["Link", "Delay (s)", "Loss", "Rate (B/s)", "Queue (B)", "Blackouts",
                        "Datagrams in", "out", "Lost (random)", "Lost (blackout)",
                        "Lost (queue full)", "LTP data segments", "LTP reports", "LTP cancels",
                        "LTP data retransmissions", "Max queue wait (s)"], link_rows(run)))
    lines.append("")
    srow = ionstat_rows(run)
    if srow:
        lines.append("ION bundle counters per node (last `bpstats` report in the node's ion.log, "
                     "cumulative since node start: sourced, forwarded, transmitted, received, "
                     "delivered, reforwarded, deleted at lifetime expiry):")
        lines.append("")
        lines.append(table(["Node", "src", "fwd", "xmt", "rcv", "dlv", "rfw", "exp"], srow))
        lines.append("")
    lrow = ltpstat_rows(run)
    if lrow:
        lines.append("ION LTP span counters per node and span (`ltpstats -a` after the traffic "
                     "phase; export = blocks this node sent on the span, import = blocks it "
                     "received; screened = segments discarded because they arrived while the "
                     "contact plan gave the sender no transmission to this node):")
        lines.append("")
        lines.append(table(["Node to peer", "Checkpoints sent", "Checkpoints resent",
                            "Data segments resent", "Export cancels", "Exports complete",
                            "Imports complete", "Reports resent", "Redundant segments in",
                            "Screened segments in"], lrow))
        lines.append("")
    for name in ("timeline", "arrivals", "latency"):
        if name in chart_paths:
            rel = os.path.relpath(chart_paths[name], report_dir)
            lines.append("![%s %s](%s)" % (sid, name, rel))
            lines.append("")
    lines.append("### PBS conformance items exercised")
    lines.append("")
    lines.append(table(["Item", "How"], [[c["ref"], c["how"]] for c in scn.get("conformance", [])]))
    lines.append("")
    lines.append("Files: `%s/` (scenario definition `scenario.json`, contact plan `contacts.ionrc`, "
                 "emulator configuration `emulator.json`, endpoint plans `rover-plan.json` and "
                 "`earth-plan.jsonl`, endpoint logs `endpoint-a.jsonl` and `endpoint-b.jsonl`, "
                 "emulator log `emulator.jsonl`, node logs `ion/`, tables `envelopes.csv`, "
                 "`receipts.csv`, `links.csv`, `ionstats.csv`, verdict `verdict.json`)." % (
                     os.path.join(results_rel, sid)))
    lines.append("")
    return "\n".join(lines)


def headline(run, verdict):
    rows = run.envelopes
    sent = [r for r in rows if r["send_result"] == "SENT"]
    got = [r for r in sent if r["delivered"]]
    lat = [r["latency_s"] for r in got]
    return {"scenario": run.id, "title": run.scn.get("title"), "passed": verdict["passed"],
            "criteria_passed": verdict["criteria_passed"], "criteria_total": verdict["criteria_total"],
            "envelopes_built": len(rows), "envelopes_sent": len(sent), "envelopes_received": len(got),
            "byte_identical": sum(1 for r in got if r["sha256_match"] and r["header_match"]),
            "latency_median_s": q(lat, 0.5), "latency_max_s": q(lat, 1.0),
            "stop_clean_without_fallback": run.run.get("stop_clean_without_fallback")}


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--results", default=os.path.join(ROOT, "results"))
    ap.add_argument("--out", default=os.path.join(ROOT, "REPORT.md"))
    args = ap.parse_args(argv)
    res = os.path.abspath(args.results)
    dirs = sorted(d for d in os.listdir(res) if os.path.exists(os.path.join(res, d, "run.json")))
    if not dirs:
        print("make_report: no scenario results in %s" % res, file=sys.stderr)
        return 1
    import evaluate
    runs = []
    for d in dirs:
        path = os.path.join(res, d)
        run = collect.collect(path)
        vp = os.path.join(path, "verdict.json")
        verdict = json.load(open(vp)) if os.path.exists(vp) else evaluate.Evaluator(path).evaluate()
        runs.append((run, verdict))

    chart_dir = os.path.join(res, "charts")
    chart_map = {}
    for run, _ in runs:
        chart_map[run.id] = charts.make(run, chart_dir)

    with open(os.path.join(res, "all-envelopes.csv"), "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=collect.ENVELOPE_COLUMNS, extrasaction="ignore")
        w.writeheader()
        for run, _ in runs:
            for r in run.envelopes:
                w.writerow({k: ("" if r.get(k) is None else r.get(k)) for k in collect.ENVELOPE_COLUMNS})

    first = runs[0][0].run
    vec = os.path.join(ROOT, "docs", "test-vectors.json")
    summary = {"generated_utc": datetime.datetime.now(datetime.timezone.utc).isoformat(),
               "git": first.get("git"), "ion": first.get("ion"), "host": first.get("host"),
               "test_vectors_sha256": sha256_file(vec) if os.path.exists(vec) else None,
               "scenarios": [headline(r, v) for r, v in runs]}
    with open(os.path.join(res, "summary.json"), "w") as fh:
        json.dump(summary, fh, indent=1)

    results_rel = os.path.relpath(res, os.path.dirname(os.path.abspath(args.out)))
    L = []
    L.append("# PBS ION demo: results")
    L.append("")
    L.append("Generated by `report/make_report.py` from `%s/` on %s. Every number below is "
             "computed from the logs of these runs. The demo exercises the PBS-CONFORMANCE-01 and "
             "PBS-CONFORMANCE-02 items listed per scenario; it makes no conformance claim and no "
             "certification claim." % (results_rel, summary["generated_utc"][:19].replace("T", " ") + " UTC"))
    L.append("")
    L.append("## Summary")
    L.append("")
    rows = []
    for run, v in runs:
        h = headline(run, v)
        rows.append([run.id, run.scn.get("title", ""), "PASS" if v["passed"] else "FAIL",
                     "%d/%d" % (v["criteria_passed"], v["criteria_total"]),
                     "%d/%d" % (h["envelopes_received"], h["envelopes_sent"]),
                     fs(h["latency_median_s"]), fs(h["latency_max_s"]),
                     {True: "yes", False: "no", None: "n/a"}[h["stop_clean_without_fallback"]]])
    L.append(table(["Scenario", "Title", "Result", "Criteria", "Received/sent envelopes",
                    "Median latency (s)", "Max latency (s)", "Clean ION stop"], rows))
    L.append("")
    L.append("Received/sent counts every envelope handed to ION, including those the scenario "
             "expects to be lost (S5) or rejected (S5, S6).")
    L.append("")
    L.append("## Test environment")
    L.append("")
    git = first.get("git") or {}
    ion = first.get("ion") or {}
    host = first.get("host") or {}
    impls = {}
    for run, _ in runs:
        for ep in ("A", "B"):
            for r in run.logs[ep]:
                if r.get("event") == "run_start":
                    impls[ep] = (r.get("impl"), r.get("pbs_link_version"), r.get("ion_version"))
    env_rows = [
        ["Demo repository commit", "%s%s" % (git.get("commit"), " (tracked files modified)" if
                                             git.get("worktree_modified") else
                                             " (no tracked file modified outside results/ and REPORT.md)")],
        ["ION", "nasa-jpl/ION-DTN tag %s, commit %s; version string %s; built by ion/build_ion.sh "
                "with NULL crypto suites (no BPSec)" % (ion.get("tag"), ion.get("commit"),
                                                        (impls.get("A") or impls.get("B") or (None, None, None))[2])],
        ["Endpoint A (rover)", "%s, PBS_LINK %s, Python %s; ION access through ctypes to libbp"
         % ((impls.get("A") or ("n/a",))[0], (impls.get("A") or (None, "n/a"))[1], host.get("python"))],
        ["Endpoint B (Earth)", "%s, C, linked against ION libbp and OpenSSL libcrypto"
         % ((impls.get("B") or ("n/a",))[0])],
        ["Link emulator", "emulator/linkemu.py (user-space UDP proxy, one socket per link "
                          "direction, per-datagram log)"],
        ["Host", "%s; %s CPUs (%s)" % (host.get("platform"), host.get("nproc"), host.get("cpu"))],
        ["Clock", "all three ION nodes, both endpoints and the emulator run on this host and read "
                  "its CLOCK_REALTIME; kernel clock source %s; NTP synchronized: %s. No clock "
                  "offset exists between the nodes; latency resolution is 1 us in the logs."
                  % (host.get("clock_source"), host.get("ntp_synchronized") or
                     "not determined (timedatectl not available)")],
        ["Cryptographic suites", "PBS-SEC-B-01 demo suites 0xFF02 (Ed25519) and 0xFF01 "
                                 "(HMAC-SHA-256), TEST KEYS derived from public labels "
                                 "(docs/SPEC-DIGEST.md 6.3); not for operational use"],
        ["Test vectors", "docs/test-vectors.json, SHA-256 %s" % summary["test_vectors_sha256"]],
    ]
    L.append(table(["Item", "Value"], env_rows))
    L.append("")
    ck = os.path.join(res, "selfcheck", "checks.json")
    if os.path.exists(ck):
        checks = json.load(open(ck))
        summary["selfchecks"] = checks
        L.append("Self-checks run by `run_demo.sh` before the scenarios (logs in `%s/selfcheck/`):"
                 % results_rel)
        L.append("")
        L.append(table(["Check", "Command", "Result", "Duration (s)", "Last output line"],
                       [[c["name"], "`%s`" % c["command"].replace(ROOT + "/", ""),
                         "pass" if c["exit"] == 0 else "FAIL (exit %d)" % c["exit"],
                         "%.1f" % c["duration_s"], c["last_line"][:120]] for c in checks]))
        L.append("")
        with open(os.path.join(res, "summary.json"), "w") as fh:
            json.dump(summary, fh, indent=1)
    topo = collect_topology()
    L.append("Topology (`ion/topology.json`): ROVER node ipn:99 (Endpoint A on ipn:99.1, Source "
             "ID Rover-Alpha), LUNAR GATEWAY relay node ipn:50 (store and forward only, no PBS "
             "processing), EARTH node ipn:23 (Endpoint B on ipn:23.1, Source ID Earth-MCC). "
             "BPv7 over LTP over UDP; every LTP datagram between nodes passes through the "
             "emulator. One-way delays: %s." % "; ".join(
                 "%s %s s" % (k, v["one_way_delay_s"]) for k, v in topo["legs"].items()))
    L.append("")
    for run, v in runs:
        L.append(scenario_section(run, v, chart_map.get(run.id, {}), results_rel,
                                  os.path.dirname(os.path.abspath(args.out))))
    L.append("## Not exercised")
    L.append("")
    L.append("PBS-CONFORMANCE-02 T001 (IP carriage), T003 (provider or path transition), T005 "
             "(latest-state degradation), T009 (PNT context) and T013 (security layering with "
             "BPSec), and the PBS-DTN-MAP-02 Section 10 items on duplicate handling and on "
             "PBS-SEC-B preservation with BPSec enabled (docs/SPEC-DIGEST.md Section 9 and A35). "
             "ION is built without BPSec.")
    L.append("")
    L.append("## Reproduce")
    L.append("")
    L.append("`./run_demo.sh` from a clean checkout builds ION at the pinned commit into "
             "`build/`, builds both endpoints, runs S1 to S7, and rewrites `results/` and this "
             "file. `./run_demo.sh S4` runs one scenario. See README.md.")
    L.append("")
    with open(args.out, "w") as fh:
        fh.write("\n".join(L))
    print("make_report: wrote %s (%d scenarios, charts: %s)" % (
        args.out, len(runs), "yes" if charts.AVAILABLE else "no (matplotlib not available)"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
