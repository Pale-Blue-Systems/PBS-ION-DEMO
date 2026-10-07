#!/usr/bin/env bash
# Live run of Endpoint B (pbs_earth) against Endpoint A (endpoint-python
# rover) on three ION nodes.
#
#   endpoint-c/tests/ion_interop.sh [OUT_DIR]     (default endpoint-c/build/ion-interop)
#
# Links: ion/udp_forward.py, one-way delay 0.25 s rover<->gateway and 1.3 s
# gateway<->earth, no loss; contact plan ion/contacts/always_on.ionrc.
# Endpoint A: endpoint-python/bin/rover with endpoint-python/plans/example.json
# and --reply-ping. Endpoint B: pbs_earth run with examples/s6_auth.jsonl.
# Both run 40 s.
#
# Checks: Endpoint A's verdict for each Endpoint B command equals the note in
# the plan; one exec record per accepted command; every envelope Endpoint A
# sent and Endpoint B received is accepted by B; both crosschecks (A decodes
# B's envelopes, B decodes A's; delivered bytes identical); both logs follow
# docs/LOG-SCHEMA.md. Exit status 0 only if all pass and ION stops cleanly.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
OUT="${1:-$HERE/build/ion-interop}"
EARTH="$HERE/build/pbs_earth"
TOOL="$HERE/build/pbs_tool"
ROVER="$ROOT/endpoint-python/bin/rover"
ATOOL="$ROOT/endpoint-python/bin/tool"
DUR=40

"$ROOT/ion/build_ion.sh" >/dev/null
make -C "$HERE" -s all >/dev/null
[[ -x "$ROVER" ]] || { echo "ion_interop: $ROVER not found" >&2; exit 2; }
if [[ -z "${PYTHON:-}" ]] && ! python3 -c 'import cryptography' 2>/dev/null; then
  PYTHON="$ROOT/build/venv-endpoint-python/bin/python"
fi
export PYTHON="${PYTHON:-python3}"
# shellcheck source=../../ion/env.sh
source "$ROOT/ion/env.sh"
# shellcheck source=../../ion/lib.sh
source "$ROOT/ion/lib.sh"
if ! ion_state_clean; then
  echo "ion_interop: ION is already running on this host (only one demo instance per host); not touching it" >&2
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

"$ROVER" --log "$OUT/endpoint-a.jsonl" --scenario INTEROP --plan "$ROOT/endpoint-python/plans/example.json" \
  --reply-ping --duration "$DUR" --ready-file "$OUT/a.ready" </dev/null >"$OUT/rover.out" 2>&1 &
A_PID=$!; PIDS+=("$A_PID")
for _ in $(seq 1 100); do [[ -e "$OUT/a.ready" ]] && break; sleep 0.1; done
[[ -e "$OUT/a.ready" ]] || { echo "Endpoint A did not become ready"; exit 1; }
ION_NODE_WDNAME="$ION_RUN_DIR/earth" "$EARTH" run --plan "$HERE/examples/s6_auth.jsonl" --scenario INTEROP \
  --duration "$DUR" --log "$OUT/endpoint-b.jsonl" </dev/null >"$OUT/earth.out" 2>&1 &
B_PID=$!; PIDS+=("$B_PID")
rcA=0; wait "$A_PID" || rcA=$?
rcB=0; wait "$B_PID" || rcB=$?
echo "Endpoint A exit status $rcA, Endpoint B exit status $rcB"
[[ $rcA == 0 && $rcB == 0 ]] || fail=1

"$ATOOL" crosscheck "$OUT/endpoint-b.jsonl" --own-log "$OUT/endpoint-a.jsonl" --out "$OUT/crosscheck_by_a.json" >/dev/null || true
"$TOOL" crosscheck "$OUT/endpoint-a.jsonl" --own-log "$OUT/endpoint-b.jsonl" --out "$OUT/crosscheck_by_b.json" >/dev/null || true

python3 - "$OUT" "$HERE/examples/s6_auth.jsonl" <<'EOF' || fail=1
import json, os, sys
out, plan = sys.argv[1:3]
A = [json.loads(l) for l in open(os.path.join(out, "endpoint-a.jsonl"))]
B = [json.loads(l) for l in open(os.path.join(out, "endpoint-b.jsonl"))]
notes = [json.loads(l)["note"].split(" (")[0] for l in open(plan) if l.strip() and not l.startswith("#")]
ok = True
ident = lambda r: (r["source_id_hex"], r["sequence"], r["timestamp_us"])

# 1. Endpoint A's verdict on each Endpoint B send, in plan order (a replay
#    shares its identity with the original: take A's records in order).
b_sent = [r for r in B if r["event"] == "send"]
a_recv = [r for r in A if r["event"] == "recv"]
pool = {}
for r in a_recv:
    if r["source_id_hex"]:
        pool.setdefault(ident(r), []).append(r)
rows = []
for s, n in zip(b_sent, notes):
    hits = pool.get(ident(s), [])
    r = hits.pop(0) if hits else None
    have = f'{r["result"]} {r["reason"]}' if r else "NOT DELIVERED"
    rows.append((s["sequence"], (s["commands"] or [""])[0], s.get("x_tamper") or s.get("x_replay_of") or "",
                 have, n, (r["latency_us"] / 1e6) if r else None))
    ok &= have == n
print("1. Endpoint A verdicts on Endpoint B commands (sequence, command, modification, verdict, expected, latency s):")
for row in rows: print("   ", row)
acc = sum(1 for r in rows if r[3] == "ACCEPT OK")
ex = [r for r in A if r["event"] == "exec"]
print(f"   accepted {acc}, Endpoint A exec records {len(ex)}: {[e['command'] for e in ex]}")
ok &= len(ex) == acc

# 2. Endpoint B's verdicts on Endpoint A's envelopes.
a_sent = [r for r in A if r["event"] == "send" and r["result"] == "SENT"]
b_recv = {ident(r): r for r in B if r["event"] == "recv" and r["source_id_hex"]}
got = [b_recv.get(ident(s)) for s in a_sent]
res = {}
for g in got:
    k = f'{g["result"]} {g["reason"]}' if g else "NOT DELIVERED"
    res[k] = res.get(k, 0) + 1
lat = sorted(g["latency_us"] / 1e6 for g in got if g)
print(f"2. Endpoint A sent {len(a_sent)}; Endpoint B verdicts {res}; eid_match all true: "
      f"{all(g['eid_match'] for g in got if g)}; latency min/median/max "
      f"{lat[0]:.3f}/{lat[len(lat)//2]:.3f}/{lat[-1]:.3f} s")
ok &= res.get("ACCEPT OK", 0) == len(a_sent)

# 3. Crosschecks.
ca = json.load(open(os.path.join(out, "crosscheck_by_a.json")))
cb = json.load(open(os.path.join(out, "crosscheck_by_b.json")))
print(f"3. A decodes B: decoded {ca['decoded']}, mismatches {len(ca['decode_mismatch'])}; "
      f"A sent {ca['own_sent']}, B received {ca['delivered']}, byte-identical {ca['byte_identical']}; ok {ca['ok']}")
print(f"   B decodes A: decoded {cb['decoded']}, mismatches {cb['decode_mismatch_records']}, "
      f"B receive path on A's envelopes {cb['b_receive_reasons']}; B sent {cb['own_sent']}, "
      f"A received {cb['delivered']}, byte-identical {cb['byte_identical']}; ok {cb['ok']}")
ok &= ca["ok"] and cb["ok"]

# 4. Schema (docs/LOG-SCHEMA.md).
COMMON = ["schema", "event", "t_us", "endpoint", "node_eid", "scenario"]
ENV = ["source_id", "source_id_hex", "sequence", "priority", "flags", "timestamp_us", "ttl_s",
       "size", "crc32", "header_hex", "envelope_len", "envelope_sha256", "envelope_hex", "frame_types"]
EVENT = {"run_start": ["impl", "pbs_link_version", "ion_version", "source_id", "dest_eid", "pid", "argv", "keys"],
         "send": ENV + ["result", "reason", "dest_eid", "lifespan_s", "lifespan_rule", "class_of_service",
                        "ordinal", "bp_send_return_us", "label", "commands", "suite_id", "key_id",
                        "security_sequence"],
         "recv": ENV + ["result", "reason", "bundle_source_eid", "bundle_creation_ms", "bundle_creation_count",
                        "bundle_creation_us", "bundle_ttl_field_ms", "bundle_admin_record", "expected_eid",
                        "eid_match", "latency_us", "commands", "suite_id", "key_id", "security_sequence",
                        "authority_id", "role", "policy_epoch"],
         "exec": ["source_id_hex", "sequence", "timestamp_us", "command", "operation", "key_id",
                  "security_sequence", "role", "action"],
         "run_end": ["counts"]}
for name, L in (("A", A), ("B", B)):
    bad = [r["event"] for r in L if r["event"] not in EVENT or
           [k for k in COMMON + EVENT[r["event"]] if k not in r] or
           [k for k in r if k not in COMMON + EVENT[r["event"]] and not k.startswith("x_")]]
    print(f"4. Endpoint {name} log: {len(L)} records, {len(bad)} schema violations, "
          f"first {L[0]['event']}, last {L[-1]['event']}")
    ok &= not bad and L[0]["event"] == "run_start" and L[-1]["event"] == "run_end"
sys.exit(0 if ok else 1)
EOF

kill "$FWD_PID" 2>/dev/null || true; FWD_PID=""
if "$ROOT/ion/stop_nodes.sh" >"$OUT/stop_nodes.log" 2>&1; then STARTED=0; else STARTED=0; fail=1; fi
grep -q 'falling back' "$OUT/stop_nodes.log" && { echo "stop used the fallback cleanup"; fail=1; }
ion_state_clean || { echo "ION state left behind"; fail=1; }
echo "ion_interop: $([[ $fail == 0 ]] && echo PASS || echo FAIL) (output in $OUT)"
exit "$fail"
