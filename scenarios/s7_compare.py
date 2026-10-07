#!/usr/bin/env python3
"""S7 offline cross-implementation comparison.

Builds every vector of docs/test-vectors.json that has inputs, and runs
every receiver case, with both endpoints, and compares the results with
each other and with the expected values in the vector file:

  - Endpoint A: the output of `endpoint-python/bin/tool vectors --out FILE`
    (field "produced": envelope bytes built from the inputs; field
    "results": one record per check, receive checks as "RESULT/REASON");
  - Endpoint B: `pbs_tool build --vectors FILE` (built bytes) and
    `pbs_tool receive` (receive path; RX cases each from a fresh state, each
    SB case from a fresh state with its deliveries in order).

  s7_compare.py --vectors docs/test-vectors.json --a-vectors a-vectors.json \
                --earth-tool build/endpoint-c/pbs_tool --out vector-comparison.json

Exit status 0 when, for every item, A's result equals B's result and both
equal the expected value.
"""
import argparse
import hashlib
import json
import subprocess
import sys


def run_tool(argv, stdin_text=""):
    r = subprocess.run(argv, input=stdin_text, capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError("%s exited %d: %s" % (" ".join(argv), r.returncode, r.stderr[-400:]))
    return [json.loads(l) for l in r.stdout.splitlines() if l.strip()]


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--vectors", required=True)
    ap.add_argument("--a-vectors", required=True, help="output of endpoint-python/bin/tool vectors")
    ap.add_argument("--earth-tool", required=True, help="Endpoint B pbs_tool")
    ap.add_argument("--out", required=True)
    args = ap.parse_args(argv)

    raw = open(args.vectors, "rb").read()
    vec = json.loads(raw)
    a = json.load(open(args.a_vectors))
    rows = []

    # 1. Build: identical inputs -> identical bytes.
    expected_bytes = {}
    for e in vec["envelopes"]:
        expected_bytes[e["id"]] = e["envelope_hex"]
    for m in vec["sec_b"]["messages"]:
        if "inputs" in m:
            expected_bytes[m["id"]] = m["envelope_hex"]
    b_built = {r["id"]: r["envelope_hex"] for r in
               run_tool([args.earth_tool, "build", "--vectors", args.vectors])}
    a_built = a.get("produced", {})
    for vid in sorted(expected_bytes):
        exp = expected_bytes[vid]
        av, bv = a_built.get(vid), b_built.get(vid)
        rows.append({"id": vid, "kind": "build", "expected_sha256": hashlib.sha256(
            bytes.fromhex(exp)).hexdigest(), "length": len(exp) // 2,
            "a": None if av is None else hashlib.sha256(bytes.fromhex(av)).hexdigest(),
            "b": None if bv is None else hashlib.sha256(bytes.fromhex(bv)).hexdigest(),
            "a_eq_b": av is not None and av == bv, "a_ok": av == exp, "b_ok": bv == exp})

    # 2. Receive: receiver cases (each from a fresh state).
    a_recv = {}
    for r in a.get("results", []):
        if r.get("check") == "receive":
            a_recv.setdefault(r["id"], []).append(r["detail"])
    lines = "".join(json.dumps({"id": c["id"], "hex": c["input_hex"], "now_us": c["now_us"]}) + "\n"
                    for c in vec["receiver_cases"])
    b_rx = {r["id"]: "%s/%s" % (r["result"], r["reason"]) for r in
            run_tool([args.earth_tool, "receive", "--fresh"], lines)}
    for c in vec["receiver_cases"]:
        exp = "%s/%s" % (c["expected"]["result"], c["expected"]["reason"])
        av = (a_recv.get(c["id"]) or [None])[0]
        bv = b_rx.get(c["id"])
        rows.append({"id": c["id"], "kind": "receive", "expected": exp, "a": av, "b": bv,
                     "a_eq_b": av == bv and av is not None, "a_ok": av == exp, "b_ok": bv == exp})

    # 3. SEC-B cases: ordered deliveries, each case from a fresh receiver state.
    msgs = {m["id"]: m["envelope_hex"] for m in vec["sec_b"]["messages"]}
    for case in vec["sec_b"]["cases"]:
        lines = "".join(json.dumps({"id": "%s/%s" % (case["id"], d["message"]),
                                    "hex": msgs[d["message"]], "now_us": d["now_us"]}) + "\n"
                        for d in case["deliveries"])
        b_out = run_tool([args.earth_tool, "receive"], lines)
        for i, d in enumerate(case["deliveries"]):
            key = "%s/%s" % (case["id"], d["message"])
            exp = "%s/%s" % (d["expected"]["result"], d["expected"]["reason"])
            # A lists repeated keys (same message delivered twice) in order.
            prior = sum(1 for dd in case["deliveries"][:i] if dd["message"] == d["message"])
            alist = a_recv.get(key, [])
            av = alist[prior] if prior < len(alist) else None
            bv = "%s/%s" % (b_out[i]["result"], b_out[i]["reason"]) if i < len(b_out) else None
            rows.append({"id": "%s#%d" % (key, i + 1), "kind": "sec_b", "expected": exp,
                         "a": av, "b": bv, "a_eq_b": av == bv and av is not None,
                         "a_ok": av == exp, "b_ok": bv == exp})

    summary = {}
    for kind in ("build", "receive", "sec_b"):
        sel = [r for r in rows if r["kind"] == kind]
        summary[kind] = {"items": len(sel), "a_eq_b": sum(r["a_eq_b"] for r in sel),
                         "a_ok": sum(r["a_ok"] for r in sel), "b_ok": sum(r["b_ok"] for r in sel)}
    ok = all(r["a_eq_b"] and r["a_ok"] and r["b_ok"] for r in rows)
    out = {"vectors": args.vectors, "vectors_sha256": hashlib.sha256(raw).hexdigest(),
           "summary": summary, "all_identical": ok, "rows": rows}
    with open(args.out, "w") as fh:
        json.dump(out, fh, indent=1)
    for kind, s in summary.items():
        print("%-8s %3d items: A == B %3d, A == expected %3d, B == expected %3d"
              % (kind, s["items"], s["a_eq_b"], s["a_ok"], s["b_ok"]))
    print("all identical" if ok else "DIFFERENCES: see %s" % args.out)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
