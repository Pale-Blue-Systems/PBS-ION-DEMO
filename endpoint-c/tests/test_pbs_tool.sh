#!/usr/bin/env bash
# Command-line checks of pbs_tool against docs/test-vectors.json.
#   tests/test_pbs_tool.sh <pbs_tool> <test-vectors.json>
set -euo pipefail

tool="$1"
vectors="$2"
tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT
fail=0

# 1. Every vector with inputs builds byte-identical.
"$tool" build --vectors "$vectors" > "$tmp/built.jsonl" || fail=1
python3 - "$tmp/built.jsonl" <<'EOF' || fail=1
import json, sys
rows = [json.loads(l) for l in open(sys.argv[1])]
bad = [r["id"] for r in rows if not r.get("matches_vector")]
print(f"  pbs_tool build --vectors: {len(rows) - len(bad)}/{len(rows)} byte-identical")
sys.exit(1 if bad or len(rows) != 34 else 0)
EOF

# 2. The same inputs fed as JSON lines on stdin give the same bytes.
python3 - "$vectors" > "$tmp/inputs.jsonl" <<'EOF'
import json, sys
v = json.load(open(sys.argv[1]))
for e in v["envelopes"] + v["sec_b"]["messages"]:
    if "inputs" in e:
        print(json.dumps({"id": e["id"], "inputs": e["inputs"], "security": e.get("security")}
                         if e.get("security") else {"id": e["id"], "inputs": e["inputs"]}))
EOF
"$tool" build < "$tmp/inputs.jsonl" > "$tmp/built2.jsonl" || fail=1
cmp -s <(python3 -c 'import json,sys;[print(json.loads(l)["id"],json.loads(l)["envelope_hex"]) for l in open(sys.argv[1])]' "$tmp/built.jsonl") \
       <(python3 -c 'import json,sys;[print(json.loads(l)["id"],json.loads(l)["envelope_hex"]) for l in open(sys.argv[1])]' "$tmp/built2.jsonl") \
  && echo "  pbs_tool build (stdin): same bytes as --vectors" || { echo "  pbs_tool build (stdin): MISMATCH"; fail=1; }

# 3. Receiver cases and SEC-B cases through pbs_tool receive.
python3 - "$vectors" "$tmp" <<'EOF'
import json, sys, os
v = json.load(open(sys.argv[1])); d = sys.argv[2]
with open(os.path.join(d, "rx.jsonl"), "w") as f:
    for c in v["receiver_cases"]:
        f.write(json.dumps({"id": c["id"], "hex": c["input_hex"], "now_us": c["now_us"]}) + "\n")
msgs = {m["id"]: m for m in v["sec_b"]["messages"]}
for c in v["sec_b"]["cases"]:
    with open(os.path.join(d, c["id"] + ".jsonl"), "w") as f:
        for i, dl in enumerate(c["deliveries"]):
            f.write(json.dumps({"id": f'{c["id"]}#{i+1}', "hex": msgs[dl["message"]]["envelope_hex"],
                                "now_us": dl["now_us"]}) + "\n")
EOF
"$tool" receive --fresh < "$tmp/rx.jsonl" > "$tmp/rx.out"
for f in "$tmp"/SB-*.jsonl; do "$tool" receive < "$f"; done > "$tmp/sb.out"
python3 - "$vectors" "$tmp/rx.out" "$tmp/sb.out" <<'EOF' || fail=1
import json, sys
v = json.load(open(sys.argv[1]))
exp = {c["id"]: c["expected"] for c in v["receiver_cases"]}
for c in v["sec_b"]["cases"]:
    for i, dl in enumerate(c["deliveries"]):
        exp[f'{c["id"]}#{i+1}'] = dl["expected"]
got = {}
for path in sys.argv[2:]:
    for l in open(path):
        r = json.loads(l); got[r["id"]] = (r["result"], r["reason"])
bad = [k for k in exp if got.get(k) != (exp[k]["result"], exp[k]["reason"])]
for k in bad: print("  MISMATCH", k, got.get(k), exp[k])
print(f"  pbs_tool receive: {len(exp) - len(bad)}/{len(exp)} receiver and SEC-B deliveries as expected")
sys.exit(1 if bad else 0)
EOF

# 4. Lifetime cases.
python3 - "$vectors" "$tool" <<'EOF' || fail=1
import json, subprocess, sys
v = json.load(open(sys.argv[1])); bad = 0
for c in v["lifetime_cases"]:
    out = json.loads(subprocess.check_output([sys.argv[2], "lifetime", str(c["ttl_s"]),
                                              str(c["timestamp_us"]), str(c["clock_us"])]))
    if out["outcome"] != c["outcome"] or out.get("lifespan_s") != c["ion_lifespan_s"] \
       or out.get("bound_ms") != c["spec_bound_ms"]:
        print("  MISMATCH", c["id"], out); bad += 1
print(f"  pbs_tool lifetime: {len(v['lifetime_cases']) - bad}/{len(v['lifetime_cases'])} as expected")
sys.exit(1 if bad else 0)
EOF

# 5. crosscheck: a peer log whose send records carry envelopes built here
#    decodes with no mismatch; a record with a wrong field is reported; with
#    --own-log, delivered envelopes are matched by identity and compared.
"$tool" plan --now-us 1767225600000000 < "$(dirname "$0")/../examples/s6_auth.jsonl" > "$tmp/plan6.jsonl"
python3 - "$tmp" <<'EOF2'
import json, os, sys
d = sys.argv[1]
recs = [json.loads(l) for l in open(os.path.join(d, "plan6.jsonl"))]
with open(os.path.join(d, "peer_ok.jsonl"), "w") as f:
    for r in recs:
        r = dict(r, event="send", result="SENT" if r["result"] == "TO_SEND" else r["result"])
        f.write(json.dumps(r) + "\n")
bad = [dict(r, event="send") for r in recs]
bad[3]["sequence"] += 1
bad[5]["frame_types"] = [2]
with open(os.path.join(d, "peer_bad.jsonl"), "w") as f:
    for r in bad: f.write(json.dumps(r) + "\n")
# Peer recv records for the first 10 envelopes; the 4th with a changed digest.
with open(os.path.join(d, "peer_recv.jsonl"), "w") as f:
    for i, r in enumerate(recs[:10]):
        f.write(json.dumps({"event": "recv", "source_id_hex": r["source_id_hex"],
                            "sequence": r["sequence"], "timestamp_us": r["timestamp_us"],
                            "envelope_sha256": r["envelope_sha256"] if i != 3 else "00" * 32,
                            "header_hex": r["header_hex"]}) + "\n")
EOF2
"$tool" crosscheck "$tmp/peer_ok.jsonl" > "$tmp/cc_ok.json" && \
  python3 -c 'import json,sys; r=json.load(open(sys.argv[1])); sys.exit(0 if r["ok"] and r["decoded"]==16 and r["decode_mismatch_records"]==0 and r["b_receive_accept"]+r["b_receive_reject"]==16 else 1)' "$tmp/cc_ok.json" \
  && echo "  pbs_tool crosscheck: 16/16 peer envelopes decoded with no field mismatch" \
  || { echo "  pbs_tool crosscheck (clean log): FAILED"; fail=1; }
if "$tool" crosscheck "$tmp/peer_bad.jsonl" > "$tmp/cc_bad.json"; then
  echo "  pbs_tool crosscheck: altered records NOT detected"; fail=1
else
  python3 -c 'import json,sys; r=json.load(open(sys.argv[1])); m=sorted(x["field"] for x in r["decode_mismatch"]); sys.exit(0 if r["decode_mismatch_records"]==2 and m==["frame_types","sequence"] else 1)' "$tmp/cc_bad.json" \
    && echo "  pbs_tool crosscheck: altered sequence and frame_types reported" \
    || { echo "  pbs_tool crosscheck (altered log): wrong report"; fail=1; }
fi
cat "$tmp/peer_ok.jsonl" "$tmp/peer_recv.jsonl" > "$tmp/peer_mix.jsonl"
"$tool" crosscheck "$tmp/peer_mix.jsonl" --own-log "$tmp/peer_ok.jsonl" --out "$tmp/cc_own.json" > /dev/null || true
python3 -c 'import json,sys; r=json.load(open(sys.argv[1])); sys.exit(0 if r["own_sent"]==16 and r["delivered"]==10 and r["byte_identical"]==9 and len(r["byte_mismatch"])==1 and not r["ok"] else 1)' "$tmp/cc_own.json" \
  && echo "  pbs_tool crosscheck --own-log: 10 delivered, 9 identical, 1 digest mismatch reported" \
  || { echo "  pbs_tool crosscheck --own-log: wrong report"; fail=1; }

# 6. Plan dry run: every example plan builds.
here="$(cd "$(dirname "$0")/.." && pwd)"
for p in "$here"/examples/*.jsonl; do
  [[ -e "$p" ]] || continue
  if "$tool" plan --now-us 1767225600000000 < "$p" > "$tmp/plan.out"; then
    echo "  pbs_tool plan $(basename "$p"): $(wc -l < "$tmp/plan.out") items"
  else
    echo "  pbs_tool plan $(basename "$p"): FAILED"; fail=1
  fi
done

exit "$fail"
