#!/usr/bin/env bash
# ION three-node self-test: three ION nodes on one host, LTP over UDP between
# them through fixed-delay UDP forwarders (ion/udp_forward.py), a bundle
# from ipn:99.1 (ROVER) to ipn:23.1 (EARTH) relayed by ipn:50 (LUNAR
# GATEWAY), a round trip in the other direction, and a clean stop.
#
#   ion/selftest.sh [OUT_DIR]        (default: build/run/selftest)
#
# Forwarder delays: rover<->gateway 0.25 s, gateway<->earth 1.3 s one way.
# Writes OUT_DIR/selftest.json (all measurements) and prints a summary.
# Exit status 0 only if every check passes and the stop leaves no ION
# processes, shared memory or semaphores.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
OUT="${1:-$ROOT/build/run/selftest}"
DELAY_RG="0.25"
DELAY_GE="1.3"

"$HERE/build_ion.sh"
# shellcheck source=env.sh
source "$HERE/env.sh"
# shellcheck source=lib.sh
source "$HERE/lib.sh"

if ! ion_state_clean; then
  echo "selftest: stale ION state found; stopping it first"
  "$HERE/stop_nodes.sh"
fi

rm -rf "$OUT"
mkdir -p "$OUT"
OUT="$(cd "$OUT" && pwd)"
FWD_PID=""
APP_PIDS=()
cleanup() {
  for p in "${APP_PIDS[@]:-}"; do [[ -n "$p" ]] && kill "$p" 2>/dev/null || true; done
  [[ -n "$FWD_PID" ]] && kill "$FWD_PID" 2>/dev/null || true
}
trap cleanup EXIT

python3 "$HERE/udp_forward.py" \
  --link "rover-gateway:29950:15013:$DELAY_RG" \
  --link "gateway-rover:25099:19913:$DELAY_RG" \
  --link "gateway-earth:25023:12313:$DELAY_GE" \
  --link "earth-gateway:22350:15013:$DELAY_GE" \
  --stats "$OUT/forwarder_stats.json" --log "$OUT/forwarder.jsonl" \
  >"$OUT/forwarder.log" 2>&1 </dev/null &
FWD_PID=$!

t_start=$(date +%s.%N)
"$HERE/start_nodes.sh" "$HERE/contacts/always_on.ionrc" | tee "$OUT/start_nodes.log"
t_started=$(date +%s.%N)

ion_version="$(printf 'v\nq\n' | ion_node earth ionadmin 2>/dev/null | sed -n 's/^: \(ION-[^ ]*\).*/\1/p' | head -1)"
printf 'l contact\nl range\nq\n' | ion_node rover ionadmin >"$OUT/rover_contact_plan.txt" 2>&1 || true

results=()
fail=0

# --- Check 1: files from ipn:99.1 to ipn:23.1 (relayed by ipn:50) ----------
for size in 100 3000 60000; do
  head -c "$size" /dev/urandom >"$OUT/payload_$size.bin"
  mkdir -p "$OUT/recv_$size"
  ( cd "$OUT/recv_$size" && ION_NODE_WDNAME="$ION_RUN_DIR/earth" exec timeout 60 bprecvfile ipn:23.1 1 ) \
    >"$OUT/recv_$size.log" 2>&1 </dev/null &
  rpid=$!
  sleep 1                               # let bprecvfile open ipn:23.1
  t0=$(date +%s.%N)
  ion_node rover bpsendfile ipn:99.1 ipn:23.1 "$OUT/payload_$size.bin" >"$OUT/send_$size.log" 2>&1 </dev/null
  if wait "$rpid"; then rc=0; else rc=$?; fi
  t1=$(date +%s.%N)
  ok=false
  if [[ $rc -eq 0 && -f "$OUT/recv_$size/testfile1" ]] &&
     cmp -s "$OUT/payload_$size.bin" "$OUT/recv_$size/testfile1"; then ok=true; else fail=1; fi
  elapsed=$(python3 -c "print(round($t1 - $t0, 3))")
  echo "selftest: file $size B ipn:99.1 -> ipn:23.1: identical=$ok, send-to-receiver-exit ${elapsed} s"
  results+=("{\"check\":\"file\",\"bytes\":$size,\"src\":\"ipn:99.1\",\"dst\":\"ipn:23.1\",\"identical\":$ok,\"elapsed_s\":$elapsed}")
done

# --- Check 2: round trip ipn:23.2 -> ipn:99.2 (bpecho) -> ipn:23.2 ----------
( ION_NODE_WDNAME="$ION_RUN_DIR/rover" exec bpecho ipn:99.2 ) >"$OUT/bpecho.log" 2>&1 </dev/null &
APP_PIDS+=($!)
sleep 1
ion_node earth timeout 90 bping -c 5 -i 1 -q 20 ipn:23.2 ipn:99.2 >"$OUT/bping.log" 2>&1 </dev/null || true
read -r rtt_min rtt_avg rtt_max < <(sed -n 's#^rtt min/avg/max/sdev = \([0-9.]*\)/\([0-9.]*\)/\([0-9.]*\)/.*#\1 \2 \3#p' "$OUT/bping.log") || true
received=$(sed -n 's/.* \([0-9]*\) bundles received.*/\1/p' "$OUT/bping.log")
ping_ok=false; [[ "${received:-0}" == 5 ]] && ping_ok=true || fail=1
echo "selftest: bping ipn:23.2 <-> ipn:99.2: received ${received:-0}/5, rtt min/avg/max ${rtt_min:-?}/${rtt_avg:-?}/${rtt_max:-?} ms"
results+=("{\"check\":\"bping\",\"sent\":5,\"received\":${received:-0},\"rtt_ms\":{\"min\":${rtt_min:-null},\"avg\":${rtt_avg:-null},\"max\":${rtt_max:-null}},\"ok\":$ping_ok}")

# --- Check 3: the gateway forwarded the bundles (bpstats) -------------------
ion_node gateway bpstats >/dev/null 2>&1 </dev/null || true
sleep 1
gw_fwd=$(grep '\[x\] fwd from' "$ION_RUN_DIR/gateway/ion.log" | tail -1 | sed -n 's/.*(+) \([0-9]*\) .*/\1/p')
gw_dlv=$(grep '\[x\] dlv from' "$ION_RUN_DIR/gateway/ion.log" | tail -1 | sed -n 's/.*(+) \([0-9]*\) .*/\1/p')
# 3 files + 5 pings + 5 echoes pass through the gateway; none is for it.
relay_ok=false; [[ "${gw_fwd:-0}" -ge 13 && "${gw_dlv:-1}" -eq 0 ]] && relay_ok=true || fail=1
echo "selftest: gateway ipn:50 forwarded ${gw_fwd:-0} bundles, delivered ${gw_dlv:-?} locally"
results+=("{\"check\":\"relay\",\"gateway_forwarded\":${gw_fwd:-0},\"gateway_delivered\":${gw_dlv:-null},\"ok\":$relay_ok}")

# --- Stop and verify nothing is left ----------------------------------------
for p in "${APP_PIDS[@]}"; do kill "$p" 2>/dev/null || true; done
APP_PIDS=()
kill "$FWD_PID" 2>/dev/null || true
wait "$FWD_PID" 2>/dev/null || true
FWD_PID=""
t_stop0=$(date +%s.%N)
if "$HERE/stop_nodes.sh" | tee "$OUT/stop_nodes.log"; then stop_rc=0; else stop_rc=$?; fi
t_stop1=$(date +%s.%N)
fallback=false; grep -q 'falling back' "$OUT/stop_nodes.log" && fallback=true
clean=false; [[ $stop_rc -eq 0 ]] && ion_state_clean && clean=true || fail=1
[[ "$fallback" == true ]] && fail=1
results+=("{\"check\":\"stop\",\"clean\":$clean,\"fallback_used\":$fallback}")
for n in $ION_NODES; do cp "$ION_RUN_DIR/$n/ion.log" "$OUT/ion_$n.log" 2>/dev/null || true; done

python3 - "$OUT/selftest.json" "$ion_version" "$(cat "$ION_RUN_DIR/t0")" \
  "$t_start" "$t_started" "$t_stop0" "$t_stop1" "$DELAY_RG" "$DELAY_GE" "$fail" \
  "${results[@]}" <<'EOF'
import json, sys, platform
(path, ver, t0, ts, tsd, tp0, tp1, d_rg, d_ge, fail), checks = sys.argv[1:11], sys.argv[11:]
doc = {
    "ion_version": ver,
    "ion_tag": "ion-open-source-4.2.0",
    "ion_commit": "568df887cb9f18aa8ec013b1a566d83215e499ae",
    "host": {"kernel": platform.release(), "machine": platform.machine()},
    "contact_plan": "ion/contacts/always_on.ionrc",
    "contact_plan_reference_time_utc": t0,
    "forwarder_one_way_delay_s": {"rover-gateway": float(d_rg), "gateway-earth": float(d_ge)},
    "start_s": round(float(tsd) - float(ts), 3),
    "stop_s": round(float(tp1) - float(tp0), 3),
    "checks": [json.loads(c) for c in checks],
    "pass": fail == "0",
}
json.dump(doc, open(path, "w"), indent=2)
print(json.dumps({k: doc[k] for k in ("ion_version", "start_s", "stop_s", "pass")}))
EOF

if [[ $fail -ne 0 ]]; then echo "selftest: FAIL (details in $OUT)"; exit 1; fi
echo "selftest: PASS (details in $OUT/selftest.json)"
