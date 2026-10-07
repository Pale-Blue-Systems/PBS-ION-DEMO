#!/usr/bin/env python3
"""Compare complete demo runs side by side.

  report/compare_runs.py --run NAME=DIR --run NAME=DIR [...] [--out FILE]

Each DIR is a results directory written by ./run_demo.sh: one subdirectory
per scenario with run.json and verdict.json (scenarios/evaluate.py), and
run_demo.log. For the scenarios present in every run the output lists:

  - each run: start time, demo commit, total time, self-checks, verdicts
  - each scenario: verdict, criteria passed and wall time per run
  - every criterion: the value it measured in each run, and whether the
    measured text is identical across the runs
  - latency per direction and priority: min, median and max per run, and
    the range (largest minus smallest) of the median and of the max
  - emulator, ION bundle and ION LTP counters that differ between runs

Every value is read from the run files; nothing is estimated. Without
--out the Markdown goes to standard output. make_report.py --compare uses
render() to put the same text into REPORT.md.
"""
import argparse
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
import collect  # noqa: E402

PRIO = collect.PRIO_NAME


def q(values, p):
    if not values:
        return None
    v = sorted(values)
    k = max(0, min(len(v) - 1, int(round(p * (len(v) - 1)))))
    return v[k]


def fs(v, nd=3):
    return "n/a" if v is None else ("%.*f" % (nd, v))


def table(header, rows):
    out = ["| " + " | ".join(header) + " |", "|" + "---|" * len(header)]
    for r in rows:
        out.append("| " + " | ".join(str(x).replace("|", "\\|").replace("\n", " ") for x in r) + " |")
    return "\n".join(out)


def joined(values, fmt=str):
    return " / ".join("n/a" if v is None else fmt(v) for v in values)


def spread(values):
    v = [x for x in values if x is not None]
    if len(v) < 2:
        return None
    return max(v) - min(v)


class Run:
    """One complete results directory."""

    def __init__(self, name, path):
        self.name = name
        self.dir = os.path.abspath(path)
        self.scenarios = {}
        for d in sorted(os.listdir(self.dir)):
            p = os.path.join(self.dir, d)
            if os.path.exists(os.path.join(p, "run.json")) and \
                    os.path.exists(os.path.join(p, "verdict.json")):
                self.scenarios[d] = {"data": collect.RunData(p),
                                     "verdict": json.load(open(os.path.join(p, "verdict.json")))}
        self.log = ""
        lp = os.path.join(self.dir, "run_demo.log")
        if os.path.exists(lp):
            self.log = open(lp, errors="replace").read()
        ck = os.path.join(self.dir, "selfcheck", "checks.json")
        self.checks = json.load(open(ck)) if os.path.exists(ck) else None

    def total_s(self):
        m = re.search(r"finished in (\d+) s", self.log)
        return int(m.group(1)) if m else None

    def first(self):
        return next(iter(self.scenarios.values()))["data"].run if self.scenarios else {}


def scenario_wall_s(data):
    import datetime
    r = data.run
    try:
        a = datetime.datetime.fromisoformat(r["started_utc"])
        b = datetime.datetime.fromisoformat(r["finished_utc"])
        return (b - a).total_seconds()
    except (KeyError, ValueError, TypeError):
        return None


def latency_stats(data, direction, prio):
    sel = [r for r in data.envelopes if r["dir"] == direction and r["priority"] == prio and
           r["send_result"] == "SENT"]
    lat = [r["latency_s"] for r in sel if r["delivered"]]
    return len(sel), len(lat), q(lat, 0.0), q(lat, 0.5), q(lat, 1.0)


def render(runs, heading_level=2):
    """Markdown comparing runs (list of Run). heading_level sets the top level."""
    H = "#" * heading_level
    runs = sorted(runs, key=lambda r: r.first().get("started_utc") or "")
    names = [r.name for r in runs]
    common = [s for s in runs[0].scenarios if all(s in r.scenarios for r in runs[1:])]
    L = []
    L.append("%s Repeatability" % H)
    L.append("")
    L.append("The same scenarios in %d complete `./run_demo.sh` runs, in the order they were "
             "made. Values in one cell are given in that order (%s). The range is the largest "
             "minus the smallest value across the runs." % (len(runs), " / ".join(names)))
    L.append("")
    rows = []
    for r in runs:
        f = r.first()
        git = f.get("git") or {}
        passed = sum(1 for s in r.scenarios.values() if s["verdict"]["passed"])
        checks = "n/a" if r.checks is None else "%d of %d passed" % (
            sum(1 for c in r.checks if c["exit"] == 0), len(r.checks))
        rows.append([r.name, (f.get("started_utc") or "n/a")[:19].replace("T", " "),
                     (git.get("commit") or "n/a")[:12] + (" (modified)" if git.get("worktree_modified") else ""),
                     "n/a" if r.total_s() is None else "%d s" % r.total_s(), checks,
                     "%d of %d" % (passed, len(r.scenarios))])
    L.append(table(["Run", "First scenario started (UTC)", "Demo commit", "run_demo.sh total time",
                    "Self-checks", "Scenarios passed"], rows))
    L.append("")

    L.append("%s# Verdicts" % H)
    L.append("")
    rows = []
    for s in common:
        vs = [r.scenarios[s]["verdict"] for r in runs]
        ds = [r.scenarios[s]["data"] for r in runs]
        rows.append([s, joined(["PASS" if v["passed"] else "FAIL" for v in vs]),
                     joined(["%d/%d" % (v["criteria_passed"], v["criteria_total"]) for v in vs]),
                     joined([scenario_wall_s(d) for d in ds], lambda x: "%.0f" % x),
                     joined([d.run.get("stop_clean_without_fallback") for d in ds],
                            lambda x: "yes" if x else "no")])
    L.append(table(["Scenario", "Result", "Criteria passed", "Wall time (s)",
                    "Clean ION stop"], rows))
    L.append("")

    L.append("%s# Measured value of every criterion" % H)
    L.append("")
    L.append("\"Same\" is yes when the measured text is identical in every run. Informative "
             "entries (no pass/fail) are marked (i).")
    L.append("")
    rows = []
    n_same = n_total = 0
    for s in common:
        crit = [{c["id"]: c for c in r.scenarios[s]["verdict"]["criteria"]} for r in runs]
        for cid in crit[0]:
            if not all(cid in c for c in crit[1:]):
                continue
            cs = [c[cid] for c in crit]
            same = all(c["measured"] == cs[0]["measured"] for c in cs[1:])
            n_total += 1
            n_same += same
            res = joined([("info" if c["informative"] else ("PASS" if c["passed"] else "FAIL"))
                          for c in cs])
            if same:
                rows.append([cid + (" (i)" if cs[0]["informative"] else ""), res, "yes",
                             cs[0]["measured"]])
            else:
                rows.append([cid + (" (i)" if cs[0]["informative"] else ""), res, "no",
                             "<br>".join("%s: %s" % (n, c["measured"]) for n, c in zip(names, cs))])
    L.append("%d of %d criteria measured the same text in every run." % (n_same, n_total))
    L.append("")
    L.append(table(["Criterion", "Result", "Same", "Measured"], rows))
    L.append("")

    L.append("%s# Latency per direction and priority" % H)
    L.append("")
    L.append("Latency is the receive time minus the envelope Timestamp (host clock), in seconds.")
    L.append("")
    rows = []
    for s in common:
        for direction in ("A->B", "B->A"):
            for p in range(5):
                st = [latency_stats(r.scenarios[s]["data"], direction, p) for r in runs]
                if all(x[0] == 0 for x in st):
                    continue
                rows.append([s, direction, PRIO[p], joined(["%d/%d" % (x[1], x[0]) for x in st]),
                             joined([x[2] for x in st], fs), joined([x[3] for x in st], fs),
                             joined([x[4] for x in st], fs), fs(spread([x[3] for x in st])),
                             fs(spread([x[4] for x in st]))])
    L.append(table(["Scenario", "Direction", "Priority", "Received/sent", "Min", "Median", "Max",
                    "Range of median", "Range of max"], rows))
    L.append("")

    L.append("%s# Counters that differ between runs" % H)
    L.append("")
    L.append("Emulator link counters (`emulator-stats.json`), ION bundle counters (last "
             "`bpstats`) and ION LTP span counters (`ltpstats -a`). Counters with the same "
             "value in every run are left out.")
    L.append("")
    rows = []
    for s in common:
        ds = [r.scenarios[s]["data"] for r in runs]
        links = sorted(set().union(*[set((d.emu.get("links") or {}).keys()) for d in ds]))
        for link in links:
            for key in ("rx", "drop_loss", "drop_blackout", "drop_queue_full",
                        "ltp_data_retransmissions", "max_queue_delay_s"):
                vals = [((d.emu.get("links") or {}).get(link) or {}).get(key) for d in ds]
                if len(set(json.dumps(v) for v in vals)) > 1:
                    fmt = (lambda x: "%.3f" % x) if key == "max_queue_delay_s" else str
                    rows.append([s, "emulator %s" % link, key, joined(vals, fmt)])
        for node in ("rover", "gateway", "earth"):
            for cls in collect.STAT_CLASSES:
                vals = [(d.ion_stats().get(node, {}).get(cls) or {}).get("+") for d in ds]
                if len(set(vals)) > 1:
                    rows.append([s, "bpstats %s" % node, cls, joined(vals)])
        peers = {99: "rover", 50: "gateway", 23: "earth"}
        for node in ("rover", "gateway", "earth"):
            spans = sorted(set().union(*[set(d.ltp_stats().get(node, {}).keys()) for d in ds]))
            for peer in spans:
                for stat in collect.LTP_STATS:
                    vals = [d.ltp_stats().get(node, {}).get(peer, {}).get(stat) for d in ds]
                    if len(set(vals)) > 1:
                        rows.append([s, "ltpstats %s to %s" % (node, peers.get(peer, peer)),
                                     stat, joined(vals)])
    L.append(table(["Scenario", "Source", "Counter", "Values"], rows) if rows else
             "No counter differs.")
    L.append("")
    return "\n".join(L)


def parse_run_arg(text):
    if "=" not in text:
        raise argparse.ArgumentTypeError("expected NAME=DIR, got %r" % text)
    name, path = text.split("=", 1)
    if not os.path.isdir(path):
        raise argparse.ArgumentTypeError("%s is not a directory" % path)
    return name, path


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--run", action="append", type=parse_run_arg, required=True,
                    help="NAME=DIR, a results directory of one ./run_demo.sh run")
    ap.add_argument("--out")
    args = ap.parse_args(argv)
    if len(args.run) < 2:
        ap.error("give at least two --run")
    runs = [Run(n, p) for n, p in args.run]
    text = "# PBS ION demo: run comparison\n\n" + render(runs) + "\n"
    if args.out:
        with open(args.out, "w") as fh:
            fh.write(text)
        print("compare_runs: wrote %s (%d runs)" % (args.out, len(runs)))
    else:
        sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
