#!/usr/bin/env python3
"""Evaluate a scenario run against its pass/fail criteria.

  scenarios/evaluate.py results/S1 [results/S2 ...]

Reads the run through report/collect.py, applies each entry of the
scenario's "criteria" list and writes results/<S>/verdict.json. Exit status
0 when every criterion of every given run passed.

A criterion is {"id", "check", "text", ...parameters}. Most checks select
envelope rows (one per send record, joined with the receipt; see
report/collect.py) with "select":
  dir            "A->B" or "B->A"
  label          exact label, or list of labels
  label_prefix   label prefix
  priority       int or list
  ttl_s          int or list
  send_result    "SENT" (default for delivery checks) or "NOT_SENT"
  sent_between   [from_s, to_s] in scenario time (send record time)

Checks:
  delivery          ratio of selected SENT envelopes received >= min_ratio
                    (default 1.0) and at least min_count rows (default 1)
  not_delivered     no selected SENT envelope was received
  byte_identical    every selected received envelope has the SHA-256 and the
                    header bytes of the sent envelope
  recv_result       every selected envelope was received with result/reason
                    "expect" ("ACCEPT OK", "REJECT SEC_REPLAY", ...)
  expect_by_label   {"expect": {label: "ACCEPT OK" | "NOT_DELIVERED" | ...}}
  send_result       every selected row has send result/reason "expect"
  latency           received rows: min_s <= latency <= max_s
  delivered_after   received rows: recv_t_s >= t_s
  eid_match         received rows have eid_match true
  cos_table         every send row: class_of_service == SPEC-DIGEST.md 7.5 table
  lifespan          selected rows have lifespan_s == value
  exec              accepted commands at the receiver: operations executed
                    equal "operations" (list, in order)
  no_unmatched      every recv record matches a send record
  ttl_respected     no receiver accepted an envelope older than its TTL
  probe_holds       bplist probe "label": at least min_count bundles of the
                    selected envelopes held at the node; with
                    "max_count": at most that many
  ion_expired       final bpstats of "node": expired bundles >= min_count
  link_counter      emulator counter "counter" on links "links": sum >= min,
                    sum <= max, each link >= each_min
  ltp_counter       ION ltpstats counter "stat" of "node" on its span to
                    "peer" >= min (and <= max)
  overtakes         every selected row was received before at least min
                    rows of "other" that were sent before it
  no_overtake       no selected row was received before a row of "other" that
                    was sent before it
  latency_compare   statistic stat_a of select <= statistic stat_b of other
  post_exit         post-run command "name" exited 0
  vectors_identical vector-comparison.json reports all items identical
"""
import json
import os
import statistics
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "report"))
import collect  # noqa: E402

COS_TABLE = collect.COS_TABLE


def as_list(v):
    return v if isinstance(v, list) else [v]


def select(rows, sel, default_sent=True):
    sel = dict(sel or {})
    if default_sent and "send_result" not in sel:
        sel["send_result"] = "SENT"
    out = []
    for r in rows:
        if "dir" in sel and r["dir"] != sel["dir"]:
            continue
        if "label" in sel and r["label"] not in as_list(sel["label"]):
            continue
        if "label_prefix" in sel and not (r["label"] or "").startswith(sel["label_prefix"]):
            continue
        if "priority" in sel and r["priority"] not in as_list(sel["priority"]):
            continue
        if "ttl_s" in sel and r["ttl_s"] not in as_list(sel["ttl_s"]):
            continue
        if sel.get("send_result") not in (None, "*") and r["send_result"] != sel["send_result"]:
            continue
        if "sent_between" in sel:
            lo, hi = sel["sent_between"]
            if r["send_t_s"] is None or not lo <= r["send_t_s"] < hi:
                continue
        out.append(r)
    return out


def fmt_s(v):
    return "n/a" if v is None else "%.3f s" % v


def stat(values, name):
    if not values:
        return None
    values = sorted(values)
    if name == "min":
        return values[0]
    if name == "max":
        return values[-1]
    if name == "median":
        return statistics.median(values)
    if name.startswith("p"):
        q = float(name[1:]) / 100.0
        k = max(0, min(len(values) - 1, int(round(q * (len(values) - 1)))))
        return values[k]
    raise ValueError(name)


def describe(r):
    return "%s %s seq %s t=%s" % (r["dir"], r["label"], r["sequence"],
                                  fmt_s(r["send_t_s"]).replace(" s", "s"))


class Evaluator:
    def __init__(self, outdir):
        self.d = collect.RunData(outdir)
        self.scn = self.d.scn
        self.rows = self.d.envelopes
        self._probes = None

    def probes(self):
        if self._probes is None:
            self._probes = self.d.probes()
        return self._probes

    # Each check returns (passed, measured text, details list).

    def c_delivery(self, c):
        rows = select(self.rows, c.get("select"))
        n = len(rows)
        got = [r for r in rows if r["delivered"]]
        ratio = len(got) / n if n else None
        ok = n >= c.get("min_count", 1) and ratio is not None and ratio >= c.get("min_ratio", 1.0)
        miss = [describe(r) for r in rows if not r["delivered"]]
        return ok, "%d of %d delivered (ratio %s)" % (len(got), n,
                                                       "n/a" if ratio is None else "%.3f" % ratio), miss

    def c_not_delivered(self, c):
        rows = select(self.rows, c.get("select"))
        got = [r for r in rows if r["delivered"]]
        ok = len(rows) >= c.get("min_count", 1) and not got
        return ok, "%d of %d delivered" % (len(got), len(rows)), [describe(r) for r in got]

    def c_byte_identical(self, c):
        rows = [r for r in select(self.rows, c.get("select")) if r["delivered"]]
        bad = [r for r in rows if not (r["sha256_match"] and r["header_match"])]
        ok = rows and not bad
        return bool(ok), "%d of %d received envelopes byte-identical (SHA-256 and header)" % (
            len(rows) - len(bad), len(rows)), [describe(r) for r in bad]

    def c_recv_result(self, c):
        rows = select(self.rows, c.get("select"))
        want = c["expect"]
        bad = []
        for r in rows:
            have = "%s %s" % (r["recv_result"], r["recv_reason"]) if r["delivered"] else "NOT_DELIVERED"
            if have != want:
                bad.append("%s: %s" % (describe(r), have))
        ok = len(rows) >= c.get("min_count", 1) and not bad
        return ok, "%d of %d: %s" % (len(rows) - len(bad), len(rows), want), bad

    def c_expect_by_label(self, c):
        details = []
        ok = True
        for label, want in c["expect"].items():
            rows = select(self.rows, {"label": label}, default_sent=False)
            if not rows:
                ok = False
                details.append("%s: no envelope with this label" % label)
                continue
            for r in rows:
                if r["send_result"] != "SENT":
                    have = "NOT_SENT %s" % r["send_reason"]
                elif r["delivered"]:
                    have = "%s %s" % (r["recv_result"], r["recv_reason"])
                else:
                    have = "NOT_DELIVERED"
                good = have == want
                ok &= good
                details.append("%s %-24s expected %-26s observed %-26s %s" % (
                    "PASS" if good else "FAIL", label, want, have, fmt_s(r["latency_s"])))
        n = len(c["expect"])
        return ok, "%d labels checked" % n, details

    def c_send_result(self, c):
        rows = select(self.rows, c.get("select"), default_sent=False)
        want = c["expect"]
        bad = ["%s: %s %s" % (describe(r), r["send_result"], r["send_reason"]) for r in rows
               if "%s %s" % (r["send_result"], r["send_reason"]) != want]
        ok = len(rows) >= c.get("min_count", 1) and not bad
        return ok, "%d of %d: %s" % (len(rows) - len(bad), len(rows), want), bad

    def c_latency(self, c):
        rows = [r for r in select(self.rows, c.get("select")) if r["delivered"]]
        lat = [r["latency_s"] for r in rows]
        lo, hi = c.get("min_s"), c.get("max_s")
        bad = [describe(r) + " latency %.3f s" % r["latency_s"] for r in rows
               if (lo is not None and r["latency_s"] < lo) or (hi is not None and r["latency_s"] > hi)]
        ok = rows and not bad
        return bool(ok), "n=%d min %s median %s max %s" % (
            len(rows), fmt_s(stat(lat, "min")), fmt_s(stat(lat, "median")),
            fmt_s(stat(lat, "max"))), bad

    def c_delivered_after(self, c):
        rows = [r for r in select(self.rows, c.get("select")) if r["delivered"]]
        bad = [describe(r) + " received at %.3f s" % r["recv_t_s"] for r in rows
               if r["recv_t_s"] < c["t_s"]]
        first = min((r["recv_t_s"] for r in rows), default=None)
        ok = len(rows) >= c.get("min_count", 1) and not bad
        return ok, "%d received; earliest receipt at %s" % (len(rows), fmt_s(first)), bad

    def c_eid_match(self, c):
        rows = [r for r in select(self.rows, c.get("select")) if r["delivered"]]
        bad = [describe(r) + " from %s" % r["bundle_source_eid"] for r in rows
               if r["eid_match"] is not True]
        return bool(rows) and not bad, "%d of %d with the expected bundle source EID" % (
            len(rows) - len(bad), len(rows)), bad

    def c_cos_table(self, c):
        rows = [r for r in select(self.rows, c.get("select"), default_sent=True)]
        bad = [describe(r) + " class %s" % r["class_of_service"] for r in rows
               if r["priority"] in COS_TABLE and r["class_of_service"] != COS_TABLE[r["priority"]]]
        return bool(rows) and not bad, "%d of %d sends used the 7.5 class of service" % (
            len(rows) - len(bad), len(rows)), bad

    def c_lifespan(self, c):
        rows = select(self.rows, c.get("select"))
        bad = [describe(r) + " lifespan %s" % r["lifespan_s"] for r in rows
               if r["lifespan_s"] != c["value"]]
        return bool(rows) and not bad, "%d of %d sent with lifetime %s s" % (
            len(rows) - len(bad), len(rows), c["value"]), bad

    def c_exec(self, c):
        ep = c.get("endpoint", "A")
        ops = [r["operation"] for r in sorted(self.d.records(ep, "exec"), key=lambda r: r["t_us"])]
        ok = ops == c["operations"]
        return ok, "executed: %s" % (", ".join(ops) or "none"), \
            [] if ok else ["expected: %s" % ", ".join(c["operations"])]

    def c_no_unmatched(self, c):
        bad = []
        for ep in ("A", "B"):
            for r in self.d.unmatched[ep]:
                bad.append("receiver %s: %s seq %s %s %s" % (ep, r.get("source_id"), r.get("sequence"),
                                                           r.get("result"), r.get("reason")))
        n = sum(len(self.d.records(ep, "recv")) for ep in ("A", "B"))
        return not bad, "%d recv records, %d without a matching send record" % (n, len(bad)), bad

    def c_ttl_respected(self, c):
        bad = []
        n = 0
        for ep in ("A", "B"):
            for r in self.d.records(ep, "recv"):
                if r.get("result") != "ACCEPT" or not r.get("ttl_s"):
                    continue
                n += 1
                age = r["t_us"] - r["timestamp_us"]
                if age > r["ttl_s"] * 1000000:
                    bad.append("receiver %s accepted seq %s age %.3f s TTL %d s" % (
                        ep, r["sequence"], age / 1e6, r["ttl_s"]))
        return not bad, "%d accepted envelopes with TTL > 0; %d older than their TTL" % (
            n, len(bad)), bad

    def _probe(self, label):
        for p in self.probes():
            if p.get("label") == label:
                return p
        return None

    def c_probe_holds(self, c):
        p = self._probe(c["probe"])
        if p is None or "bundles" not in p:
            return False, "probe %r not found" % c["probe"], []
        sel = c.get("select", {})
        held = [b for b in p["bundles"] if b.get("label") is not None and select(
            [r for r in self.rows if r["sequence"] == b["sequence"] and
             r["timestamp_us"] == b["timestamp_us"]], sel, default_sent=False)]
        n = len(held)
        ok = n >= c.get("min_count", 0) and ("max_count" not in c or n <= c["max_count"])
        labels = {}
        for b in held:
            labels[b["label"]] = labels.get(b["label"], 0) + 1
        return ok, "%s node held %d matching bundles at %s (all bundles: %d)" % (
            p["node"], n, fmt_s(p.get("scenario_t_s")), p["count"]), \
            ["%s x%d" % kv for kv in sorted(labels.items())]

    def c_ion_expired(self, c):
        st = self.d.ion_stats().get(c["node"], {})
        n = st.get("exp", {}).get("+")
        ok = n is not None and n >= c.get("min_count", 1) and (
            "max_count" not in c or n <= c["max_count"])
        return ok, "%s node bpstats: %s bundles deleted at lifetime expiry" % (c["node"], n), []

    def c_link_counter(self, c):
        links = (self.d.emu.get("links") or {})
        total = 0
        parts = []
        for name in as_list(c["links"]):
            v = links.get(name, {}).get(c["counter"])
            if v is None and c["counter"].startswith("ltp_"):
                v = links.get(name, {}).get("ltp_segments", {}).get(c["counter"][4:], 0)
            v = v or 0
            total += v
            parts.append("%s=%s" % (name, ("%.3f" % v) if isinstance(v, float) else v))
        ok = True
        if "each_min" in c:
            ok &= all(float(x.split("=")[1]) >= c["each_min"] for x in parts)
        if "min" in c:
            ok &= total >= c["min"]
        if "max" in c:
            ok &= total <= c["max"]
        total_txt = ("%.3f" % total) if isinstance(total, float) else str(total)
        return ok, "%s total %s (%s)" % (c["counter"], total_txt, ", ".join(parts)), []

    def c_ltp_counter(self, c):
        names = {"rover": 99, "gateway": 50, "earth": 23}
        st = self.d.ltp_stats().get(c["node"], {}).get(names[c["peer"]], {})
        v = st.get(c["stat"])
        ok = v is not None and v >= c.get("min", 0) and ("max" not in c or v <= c["max"])
        return ok, "%s node, span to %s: %s = %s (ltpstats -a)" % (
            c["node"], c["peer"], c["stat"], v), []

    def c_overtakes(self, c):
        rows = [r for r in select(self.rows, c.get("select")) if r["delivered"]]
        others = select(self.rows, c["other"])
        bad = []
        counts = []
        for r in rows:
            k = sum(1 for o in others if o["send_t_s"] < r["send_t_s"] and
                    (not o["delivered"] or o["recv_t_s"] > r["recv_t_s"]))
            counts.append(k)
            if k < c.get("min", 1):
                bad.append(describe(r) + " overtook %d" % k)
        ok = rows and not bad
        return bool(ok), "%d envelopes; each overtook min %s / max %s earlier-sent envelopes of the other group" % (
            len(rows), min(counts) if counts else "n/a", max(counts) if counts else "n/a"), bad

    def c_no_overtake(self, c):
        rows = [r for r in select(self.rows, c.get("select")) if r["delivered"]]
        others = [o for o in select(self.rows, c["other"]) if o["delivered"]]
        bad = []
        for r in rows:
            k = [o for o in others if o["send_t_s"] < r["send_t_s"] and o["recv_t_s"] > r["recv_t_s"]]
            if k:
                bad.append(describe(r) + " overtook %d" % len(k))
        return bool(rows) and not bad, "%d envelopes; %d overtook an earlier-sent envelope of the other group" % (
            len(rows), len(bad)), bad

    def c_latency_compare(self, c):
        a = [r["latency_s"] for r in select(self.rows, c.get("select")) if r["delivered"]]
        b = [r["latency_s"] for r in select(self.rows, c["other"]) if r["delivered"]]
        va, vb = stat(a, c["stat_a"]), stat(b, c["stat_b"])
        ok = va is not None and vb is not None and va <= vb
        return ok, "%s %s = %s; %s %s = %s" % (c.get("name_a", "selected"), c["stat_a"], fmt_s(va),
                                                c.get("name_b", "other"), c["stat_b"], fmt_s(vb)), []

    def c_post_exit(self, c):
        for p in self.d.run.get("post", []):
            if p["name"] == c["name"]:
                out = os.path.join(self.d.dir, p["file"])
                tail = open(out).read().strip().splitlines()[-3:] if os.path.exists(out) else []
                return p["exit"] == 0, "%s exit status %s" % (c["name"], p["exit"]), tail
        return False, "post-run command %s did not run" % c["name"], []

    def c_vectors_identical(self, c):
        p = os.path.join(self.d.dir, "vector-comparison.json")
        if not os.path.exists(p):
            return False, "vector-comparison.json missing", []
        v = json.load(open(p))
        s = v["summary"]
        txt = "; ".join("%s: %d items, A==B %d, A==expected %d, B==expected %d" % (
            k, x["items"], x["a_eq_b"], x["a_ok"], x["b_ok"]) for k, x in s.items())
        bad = [r["id"] for r in v["rows"] if not (r["a_eq_b"] and r["a_ok"] and r["b_ok"])]
        return v["all_identical"], txt, bad

    def evaluate(self):
        results = []
        run_ok = "error" not in self.d.run
        for c in self.scn.get("criteria", []):
            fn = getattr(self, "c_" + c["check"], None)
            if fn is None:
                passed, measured, details = False, "unknown check %r" % c["check"], []
            else:
                try:
                    passed, measured, details = fn(c)
                except Exception as ex:          # a broken criterion is a failure
                    passed, measured, details = False, "error: %r" % ex, []
            results.append({"id": c["id"], "text": c.get("text", ""), "check": c["check"],
                            "passed": bool(passed), "measured": measured,
                            "details": details[:60], "informative": bool(c.get("informative"))})
        required = [r for r in results if not r["informative"]]
        verdict = {
            "scenario": self.d.id, "title": self.scn.get("title"),
            "run_completed": run_ok, "run_error": self.d.run.get("error"),
            "stop_clean_without_fallback": self.d.run.get("stop_clean_without_fallback"),
            "passed": run_ok and bool(required) and all(r["passed"] for r in required),
            "criteria_passed": sum(r["passed"] for r in required), "criteria_total": len(required),
            "criteria": results,
        }
        with open(os.path.join(self.d.dir, "verdict.json"), "w") as fh:
            json.dump(verdict, fh, indent=1)
        return verdict


def main(argv):
    if not argv:
        print(__doc__, file=sys.stderr)
        return 2
    rc = 0
    for p in argv:
        collect.collect(p)
        v = Evaluator(p).evaluate()
        print("%s: %s (%d/%d criteria)%s" % (v["scenario"], "PASS" if v["passed"] else "FAIL",
                                             v["criteria_passed"], v["criteria_total"],
                                             "" if v["run_completed"] else " run error: %s" % v["run_error"]))
        for r in v["criteria"]:
            print("  %s %-6s %s: %s" % ("info" if r["informative"] else ("PASS" if r["passed"] else "FAIL"),
                                        r["id"], r["text"], r["measured"]))
            if not r["passed"] and not r["informative"]:
                for d in r["details"][:10]:
                    print("           %s" % d)
        rc |= 0 if v["passed"] else 1
    return rc


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
