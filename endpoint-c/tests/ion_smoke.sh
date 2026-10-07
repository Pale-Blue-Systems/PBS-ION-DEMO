#!/usr/bin/env bash
# Live check of pbs_earth on three ION nodes (ion/start_nodes.sh), with ION's
# own tools (bprecvfile, bpsendfile) on the rover node instead of Endpoint A.
#
#   endpoint-c/tests/ion_smoke.sh [OUT_DIR]     (default endpoint-c/build/ion-smoke)
#
# Links: ion/udp_forward.py, one-way delay 0.25 s rover<->gateway and 1.3 s
# gateway<->earth, no loss; contact plan ion/contacts/always_on.ionrc.
#
# A. Earth -> rover: pbs_earth send with examples/s6_auth.jsonl; the rover
#    runs bprecvfile ipn:99.1. Checks: every envelope handed to ION arrives
#    byte-identical, and pbs_tool receive (in arrival order, at the arrival
#    time) gives the verdict noted in the plan.
# B. Rover -> Earth: pbs_earth run (receiving, and sending two envelopes to
#    ipn:99.1 that wait in the rover's endpoint queue); the rover sends
#    envelopes built by pbs_tool with bpsendfile ipn:99.1. Checks: verdicts,
#    bundle source EID, byte identity (SHA-256), class of service accepted.
# C. The rover collects the two queued envelopes with bprecvfile.
#
# Exit status 0 only if every check passes and the nodes stop cleanly.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
OUT="${1:-$HERE/build/ion-smoke}"
EARTH="$HERE/build/pbs_earth"
TOOL="$HERE/build/pbs_tool"

"$ROOT/ion/build_ion.sh" >/dev/null
make -C "$HERE" -s all >/dev/null
# shellcheck source=../../ion/env.sh
source "$ROOT/ion/env.sh"
# shellcheck source=../../ion/lib.sh
source "$ROOT/ion/lib.sh"

if ! ion_state_clean; then
  echo "ion_smoke: ION is already running on this host (only one demo instance per host); not touching it" >&2
  exit 2
fi

rm -rf "$OUT"
mkdir -p "$OUT"
OUT="$(cd "$OUT" && pwd)"
FWD_PID=""
PIDS=()
STARTED=0
cleanup() {
  for p in "${PIDS[@]:-}"; do [[ -n "$p" ]] && kill "$p" 2>/dev/null || true; done
  [[ -n "$FWD_PID" ]] && kill "$FWD_PID" 2>/dev/null || true
  if [[ "$STARTED" == 1 ]]; then "$ROOT/ion/stop_nodes.sh" >"$OUT/stop_nodes.log" 2>&1 || true; fi
}
trap cleanup EXIT

python3 "$ROOT/ion/udp_forward.py" \
  --link "rover-gateway:29950:15013:0.25" --link "gateway-rover:25099:19913:0.25" \
  --link "gateway-earth:25023:12313:1.3" --link "earth-gateway:22350:15013:1.3" \
  --stats "$OUT/forwarder_stats.json" --log "$OUT/forwarder.jsonl" \
  >"$OUT/forwarder.log" 2>&1 </dev/null &
FWD_PID=$!
"$ROOT/ion/start_nodes.sh" "$ROOT/ion/contacts/always_on.ionrc" >"$OUT/start_nodes.log" 2>&1
STARTED=1
fail=0

# ---- A. Earth -> rover ---------------------------------------------------------------
mkdir -p "$OUT/rover_rx"
nA=$(grep -c '"kind"' "$HERE/examples/s6_auth.jsonl")
( cd "$OUT/rover_rx" && ION_NODE_WDNAME="$ION_RUN_DIR/rover" exec timeout 90 bprecvfile ipn:99.1 "$nA" ) \
  >"$OUT/rover_bprecvfile.log" 2>&1 </dev/null &
RX_PID=$!; PIDS+=("$RX_PID")
sleep 1
ion_node earth "$EARTH" send --plan "$HERE/examples/s6_auth.jsonl" --log "$OUT/earth_send.jsonl" \
  </dev/null >"$OUT/earth_send.out" 2>&1
wait "$RX_PID" || true
# Arrival time of each file: bprecvfile writes testfileN when the bundle is
# delivered; mtime in microseconds.
for f in "$OUT"/rover_rx/testfile*; do
  [[ -e "$f" ]] || continue
  printf '%s %s\n' "$(basename "$f")" "$(stat -c %.6Y "$f" | tr -d .)"
done > "$OUT/rover_rx/arrivals.txt"
python3 - "$OUT" "$TOOL" "$HERE/examples/s6_auth.jsonl" <<'EOF' || fail=1
import hashlib, json, os, subprocess, sys
out, tool, plan = sys.argv[1:4]
sent = [json.loads(l) for l in open(os.path.join(out, "earth_send.jsonl")) if '"event":"send"' in l]
notes = [json.loads(l).get("note") for l in open(plan) if l.strip() and not l.startswith("#")]
arr = [l.split() for l in open(os.path.join(out, "rover_rx", "arrivals.txt"))]
arr.sort(key=lambda a: int(a[0][len("testfile"):]))
files = {a[0]: open(os.path.join(out, "rover_rx", a[0]), "rb").read() for a in arr}
handed = [i for i, s in enumerate(sent) if s["result"] == "SENT"]
# Match each arrival to the earliest unmatched envelope with the same bytes
# (a replay is byte-identical to its original by design).
unmatched = list(handed)
match = []
for a in arr:
    h = hashlib.sha256(files[a[0]]).hexdigest()
    j = next((i for i in unmatched if sent[i]["envelope_sha256"] == h), None)
    if j is not None:
        unmatched.remove(j)
    match.append(j)
ok = None not in match and not unmatched
print(f"A: {len(handed)} envelopes handed to ION, {len(arr)} delivered at ipn:99.1, "
      f"{sum(1 for j in match if j is not None)} byte-identical to a handed-over envelope, "
      f"{len(unmatched)} missing")
# Receive path at the arrival time, in arrival order, one replay state.
lines = "".join(json.dumps({"id": a[0], "hex": files[a[0]].hex(), "now_us": int(a[1])}) + "\n" for a in arr)
res = subprocess.run([tool, "receive"], input=lines, capture_output=True, text=True, check=True)
recs = [json.loads(l) for l in res.stdout.splitlines()]
rows = []
for a, r, j in zip(arr, recs, match):
    s = sent[j] if j is not None else {}
    want = (notes[j] or "").split(" (")[0] if j is not None else "?"
    have = f'{r["result"]} {r["reason"]}'
    lat = (int(a[1]) - s.get("timestamp_us", 0)) / 1e6
    how = s.get("x_tamper") or (("replay of " + s["x_replay_of"]) if s.get("x_replay_of") else "") or \
        (("key_id " + s["key_id"]) if s.get("key_id") and s.get("key_id") != "TEST-ED25519-01" and
         s.get("x_signing_key") == s.get("key_id") else "") or \
        (("signed with " + s["x_signing_key"]) if s.get("x_signing_key") and s.get("x_signing_key") != s.get("key_id") else "")
    rows.append((a[0], (s.get("commands") or [None])[0] or s.get("x_kind"), how, have, want, lat))
    ok &= have == want
for row in rows:
    print("  %-10s %-20s %-30s %-24s expected %-24s %.3f s" % row)
with open(os.path.join(out, "A_summary.json"), "w") as f:
    json.dump({"handed_to_ion": len(handed), "delivered": len(arr), "missing": len(unmatched),
               "rows": rows}, f, indent=1)
sys.exit(0 if ok else 1)
EOF

# ---- B. Rover -> Earth ---------------------------------------------------------------
cat > "$OUT/earth_plan_B.jsonl" <<'EOF'
{"at_s": 0.0, "kind": "telemetry", "text": "TLM QUEUED-1 for the rover endpoint queue", "label": "q1"}
{"at_s": 0.1, "kind": "command", "command": "PING QUEUED-2", "label": "q2"}
{"at_s": 0.2, "kind": "heartbeat", "priority": 0, "ttl": 0, "label": "q3"}
EOF
cat > "$OUT/rover_plan_B.jsonl" <<'EOF'
{"kind": "telemetry", "text": "TLM P0 CRITICAL", "priority": 0, "ttl": 0, "note": "ACCEPT OK"}
{"kind": "telemetry", "text": "TLM P1 HIGH", "priority": 1, "note": "ACCEPT OK"}
{"kind": "telemetry", "text": "TLM P2 NORMAL", "priority": 2, "flags": 1, "note": "ACCEPT OK"}
{"kind": "status", "text": "STATUS P3 LOW", "priority": 3, "ttl": 3600, "note": "ACCEPT OK"}
{"kind": "telemetry", "text": "TLM P4 BULK 60000 BYTES", "priority": 4, "ttl": 86400, "pad_to": 60000, "note": "ACCEPT OK"}
{"kind": "heartbeat", "priority": 2, "note": "ACCEPT OK"}
{"kind": "telemetry", "text": "TLM SIGNED BY EARTH KEY", "sign": true, "note": "ACCEPT OK"}
{"kind": "telemetry", "text": "TLM BAD CRC", "tamper": "crc", "note": "REJECT CRC"}
{"kind": "telemetry", "text": "TLM EXPIRES IN TRANSIT", "ttl": 1, "lifetime_s": 60, "note": "REJECT TTL_EXPIRED"}
{"kind": "frames", "frames": [{"type": 2, "text": "DRIVE 99"}], "note": "REJECT SEC_MISSING"}
EOF
nB=$(grep -c '"kind"' "$OUT/rover_plan_B.jsonl")
# Started as a simple command (not through the ion_node shell function) so
# that $! is the pbs_earth process itself.
ION_NODE_WDNAME="$ION_RUN_DIR/earth" "$EARTH" run --plan "$OUT/earth_plan_B.jsonl" --expect "$nB" \
  --duration 60 --log "$OUT/earth_run.jsonl" </dev/null >"$OUT/earth_run.out" 2>&1 &
EARTH_PID=$!; PIDS+=("$EARTH_PID")
sleep 1
"$TOOL" plan --source-id Rover-Alpha --dest ipn:23.1 --now-us "$(date +%s%6N)" \
  < "$OUT/rover_plan_B.jsonl" > "$OUT/rover_built_B.jsonl"
mkdir -p "$OUT/rover_tx"
i=0
while IFS= read -r line; do
  i=$((i + 1))
  hex=$(python3 -c 'import json,sys;print(json.loads(sys.argv[1])["x_envelope_hex"])' "$line")
  life=$(python3 -c 'import json,sys;print(json.loads(sys.argv[1])["lifespan_s"])' "$line")
  cls=$(python3 -c 'import json,sys;print(json.loads(sys.argv[1])["class_of_service"])' "$line")
  python3 -c 'import sys;open(sys.argv[1],"wb").write(bytes.fromhex(sys.argv[2]))' "$OUT/rover_tx/env$i.bin" "$hex"
  ion_node rover bpsendfile ipn:99.1 ipn:23.1 "$OUT/rover_tx/env$i.bin" "0.$cls" "$life" \
    >>"$OUT/rover_bpsendfile.log" 2>&1 </dev/null
done < "$OUT/rover_built_B.jsonl"
rc=0; wait "$EARTH_PID" || rc=$?
[[ $rc == 0 ]] || { echo "B: pbs_earth run exit status $rc"; fail=1; }
python3 - "$OUT" <<'EOF' || fail=1
import json, os, sys
out = sys.argv[1]
built = [json.loads(l) for l in open(os.path.join(out, "rover_built_B.jsonl"))]
notes = [json.loads(l)["note"] for l in open(os.path.join(out, "rover_plan_B.jsonl"))]
log = [json.loads(l) for l in open(os.path.join(out, "earth_run.jsonl"))]
recv = {r["envelope_sha256"]: r for r in log if r["event"] == "recv"}
sent = [r for r in log if r["event"] == "send"]
end = [r for r in log if r["event"] == "run_end"][-1]
ok = True
acc = sum(v for k, v in end["counts"].items() if k.startswith("recv:ACCEPT"))
rej = sum(v for k, v in end["counts"].items() if k.startswith("recv:REJECT"))
print(f"B: pbs_earth sent {sum(1 for s in sent if s['result']=='SENT')} to ipn:99.1; "
      f"received {acc + rej} (accepted {acc}, rejected {rej}); run_end counts {end['counts']}")
items = [json.loads(l) for l in open(os.path.join(out, "rover_plan_B.jsonl"))]
for b, note, item in zip(built, notes, items):
    r = recv.get(b["envelope_sha256"])
    have = f'{r["result"]} {r["reason"]}' if r else "NOT RECEIVED"
    good = r is not None and have == note and r.get("bundle_source_eid") == "ipn:99.1" \
        and r.get("eid_match") is True
    ok &= good
    lat = r["latency_us"] / 1e6 if r else float("nan")
    label = (item.get("text") or item["kind"])[:26]
    print("  %-26s P%s size %-6s class %s lifetime %-10s -> %-18s expected %-18s src %s latency %.3f s %s" % (
        label, b["priority"], b["size"], b["class_of_service"], b["lifespan_s"], have, note,
        r.get("bundle_source_eid") if r else "-", lat, "" if good else "<-- FAIL"))
with open(os.path.join(out, "B_summary.json"), "w") as f:
    json.dump({"run_end": end}, f, indent=1)
sys.exit(0 if ok else 1)
EOF

# ---- C. Rover collects the two envelopes queued at ipn:99.1 ---------------------------
mkdir -p "$OUT/rover_rx_C"
( cd "$OUT/rover_rx_C" && ION_NODE_WDNAME="$ION_RUN_DIR/rover" exec timeout 30 bprecvfile ipn:99.1 3 ) \
  >"$OUT/rover_bprecvfile_C.log" 2>&1 </dev/null || true
python3 - "$OUT" <<'EOF' || fail=1
import glob, hashlib, json, os, sys
out = sys.argv[1]
sent = {r["envelope_sha256"]: r for r in map(json.loads, open(os.path.join(out, "earth_run.jsonl")))
        if r["event"] == "send" and r["result"] == "SENT"}
got = [hashlib.sha256(open(f, "rb").read()).hexdigest() for f in sorted(glob.glob(os.path.join(out, "rover_rx_C", "testfile*")))]
match = sum(1 for h in got if h in sent)
ttl0 = [r for r in sent.values() if r["ttl_s"] == 0]
print(f"C: {len(sent)} sent by pbs_earth run, {len(got)} collected from the rover endpoint queue, "
      f"{match} byte-identical; TTL 0 envelope sent with bundle lifetime "
      f"{ttl0[0]['lifespan_s'] if ttl0 else None} s delivered: "
      f"{bool(ttl0) and ttl0[0]['envelope_sha256'] in got}")
sys.exit(0 if len(sent) == 3 and match == 3 and ttl0 and ttl0[0]["envelope_sha256"] in got else 1)
EOF

# ---- D. SIGTERM while blocked in bp_receive ---------------------------------------------
ION_NODE_WDNAME="$ION_RUN_DIR/earth" "$EARTH" recv --log "$OUT/earth_sigterm.jsonl" \
  </dev/null >"$OUT/earth_sigterm.out" 2>&1 &
EARTH_PID=$!; PIDS+=("$EARTH_PID")
sleep 2
kill -TERM "$EARTH_PID"
if timeout 10 tail --pid="$EARTH_PID" -f /dev/null; then
  rc=0; wait "$EARTH_PID" || rc=$?
  if [[ $rc == 0 ]] && grep -q '"event":"run_end"' "$OUT/earth_sigterm.jsonl"; then
    echo "D: pbs_earth recv stopped on SIGTERM with exit status 0 and a run_end record"
  else
    echo "D: pbs_earth recv exit status $rc after SIGTERM"; fail=1
  fi
else
  echo "D: pbs_earth recv did not stop within 10 s of SIGTERM"; fail=1
fi

# ---- E. Every pbs_earth record against docs/LOG-SCHEMA.md ------------------------------
python3 - "$OUT"/earth_send.jsonl "$OUT"/earth_run.jsonl "$OUT"/earth_sigterm.jsonl <<'EOF' || fail=1
import json, sys
COMMON = ["schema", "event", "t_us", "endpoint", "node_eid", "scenario"]
ENV = ["source_id", "source_id_hex", "sequence", "priority", "flags", "timestamp_us", "ttl_s",
       "size", "crc32", "header_hex", "envelope_len", "envelope_sha256", "envelope_hex",
       "frame_types"]
EVENT = {
    "run_start": ["impl", "pbs_link_version", "ion_version", "source_id", "dest_eid", "pid",
                  "argv", "keys"],
    "send": ENV + ["result", "reason", "dest_eid", "lifespan_s", "lifespan_rule",
                   "class_of_service", "ordinal", "bp_send_return_us", "label", "commands",
                   "suite_id", "key_id", "security_sequence"],
    "recv": ENV + ["result", "reason", "bundle_source_eid", "bundle_creation_ms",
                   "bundle_creation_count", "bundle_creation_us", "bundle_ttl_field_ms",
                   "bundle_admin_record", "expected_eid", "eid_match", "latency_us", "commands",
                   "suite_id", "key_id", "security_sequence", "authority_id", "role",
                   "policy_epoch"],
    "exec": ["source_id_hex", "sequence", "timestamp_us", "command", "operation", "key_id",
             "security_sequence", "role", "action"],
    "run_end": ["counts"],
}
bad = 0; n = 0
for path in sys.argv[1:]:
    recs = [json.loads(l) for l in open(path).read().splitlines()]
    if recs[0]["event"] != "run_start" or recs[-1]["event"] != "run_end":
        print("  ", path, "does not start with run_start and end with run_end"); bad += 1
    for r in recs:
        n += 1
        want = COMMON + EVENT.get(r["event"], [])
        missing = [k for k in want if k not in r]
        extra = [k for k in r if k not in want and not k.startswith("x_")]
        if r["event"] not in EVENT or missing or extra or r["schema"] != "pbs-ion-demo-log/1" \
           or r["endpoint"] != "B" or r["node_eid"] != "ipn:23.1":
            print("  schema violation:", path, r["event"], missing, extra); bad += 1
print(f"E: {n} pbs_earth records checked against docs/LOG-SCHEMA.md, {bad} violations")
sys.exit(1 if bad else 0)
EOF

# ---- Stop ------------------------------------------------------------------------------
kill "$FWD_PID" 2>/dev/null || true; FWD_PID=""
if "$ROOT/ion/stop_nodes.sh" >"$OUT/stop_nodes.log" 2>&1; then STARTED=0; else STARTED=0; fail=1; fi
grep -q 'falling back' "$OUT/stop_nodes.log" && { echo "stop used the fallback cleanup"; fail=1; }
ion_state_clean || { echo "ION state left behind"; fail=1; }
echo "ion_smoke: $([[ $fail == 0 ]] && echo PASS || echo FAIL) (output in $OUT)"
exit "$fail"
