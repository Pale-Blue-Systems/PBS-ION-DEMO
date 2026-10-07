#!/usr/bin/env bash
# PBS ION demo: build everything, run the scenarios, write results/ and REPORT.md.
#
#   ./run_demo.sh            build, self-checks, S1 to S7, report
#   ./run_demo.sh S2 S4      only these scenarios (other results/<S>/ are kept)
#
# Steps:
#   1. ion/build_ion.sh: NASA/JPL ION at the pinned commit into build/ion
#      (skipped when build/ion already holds that commit)
#   2. Python environment build/venv-demo (system site packages visible):
#      cryptography for Endpoint A, pytest for its unit tests, matplotlib for
#      charts (optional; without it REPORT.md has no charts)
#   3. PBS_LINK 0.1.4 for Endpoint A (endpoint-python/fetch_pbs_link.sh)
#   4. Endpoint B: make -C endpoint-c into build/endpoint-c
#   5. self-checks: emulator, scenario definitions, both endpoints' unit tests
#   6. each scenario: scenarios/runner.py (emulator + 3 ION nodes + both
#      endpoints, stopped cleanly) then scenarios/evaluate.py
#   7. report/make_report.py: REPORT.md and results/REPORT.md
#
# Environment:
#   PBS_DEMO_RESULTS     results directory (default results)
#   PBS_DEMO_WAIT_S      seconds to wait if another ION instance runs on this
#                        host (default 0: fail at once)
#   PBS_DEMO_SKIP_CHECKS 1 skips step 5
#
# Exit status: 0 when every self-check and every scenario passed.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"
RESULTS="$(mkdir -p "${PBS_DEMO_RESULTS:-results}" && cd "${PBS_DEMO_RESULTS:-results}" && pwd)"
ALL_SCENARIOS=(S1 S2 S3 S4 S5 S6 S7)
if [[ $# -gt 0 ]]; then SCENARIOS=("$@"); else SCENARIOS=("${ALL_SCENARIOS[@]}"); fi
for s in "${SCENARIOS[@]}"; do
  [[ -f "scenarios/$s.json" ]] || { echo "run_demo: no scenario scenarios/$s.json" >&2; exit 2; }
done

if [[ $# -eq 0 ]]; then
  # Full run: remove what an earlier run wrote (only that; other files in the
  # results directory are kept).
  rm -rf "$RESULTS"/S[0-9]* "$RESULTS/selfcheck" "$RESULTS/charts" "$RESULTS/summary.json" \
         "$RESULTS/all-envelopes.csv" "$RESULTS/run_demo.log" "$RESULTS/REPORT.md"
fi
LOG="$RESULTS/run_demo.log"
exec > >(tee -a "$LOG") 2>&1
say() { echo "[$(date -u +%H:%M:%S)] run_demo: $*"; }
t_start=$(date +%s)
say "repository $ROOT, results $RESULTS, scenarios ${SCENARIOS[*]}"

# --- prerequisites ---------------------------------------------------------------
missing=()
for tool in git autoreconf automake libtoolize gcc make python3 ps ipcs ipcrm; do
  command -v "$tool" >/dev/null 2>&1 || missing+=("$tool")
done
if ! echo '#include <openssl/evp.h>' | gcc -E -x c - >/dev/null 2>&1; then
  missing+=("OpenSSL headers (libssl-dev)")
fi
# ION's configure.ac uses PKG_CHECK_MODULES; autoreconf needs pkg.m4 (pkgconf).
if ! command -v aclocal >/dev/null 2>&1 || [[ ! -f "$(aclocal --print-ac-dir 2>/dev/null)/pkg.m4" ]]; then
  missing+=("pkg.m4 (pkgconf)")
fi
if [[ ${#missing[@]} -gt 0 ]]; then
  say "missing prerequisites: ${missing[*]}"
  say "on Ubuntu: apt-get install git autoconf automake libtool pkgconf gcc make python3 python3-venv libssl-dev procps"
  exit 2
fi

# --- 1. ION ------------------------------------------------------------------------
say "step 1: ION"
ion/build_ion.sh

# --- 2. Python environment ---------------------------------------------------------
say "step 2: Python environment"
VENV="$ROOT/build/venv-demo"
if [[ ! -x "$VENV/bin/python" ]]; then
  python3 -m venv --system-site-packages "$VENV" || say "python3 -m venv failed; using python3"
fi
PY=python3
[[ -x "$VENV/bin/python" ]] && PY="$VENV/bin/python"
"$PY" -c "import cryptography" 2>/dev/null || "$PY" -m pip install -q "cryptography>=41"
"$PY" -c "import pytest" 2>/dev/null || "$PY" -m pip install -q "pytest>=7" || say "pytest not installed"
"$PY" -c "import matplotlib" 2>/dev/null || "$PY" -m pip install -q matplotlib \
  || say "matplotlib not installed: REPORT.md will have no charts"
export PYTHON="$PY"
"$PY" -c "import cryptography" || { say "cryptography is required for Endpoint A"; exit 2; }

# --- 3. PBS_LINK ------------------------------------------------------------------
say "step 3: PBS_LINK"
endpoint-python/fetch_pbs_link.sh

# --- 4. Endpoint B ----------------------------------------------------------------
say "step 4: Endpoint B"
make -s -C endpoint-c BUILD="$ROOT/build/endpoint-c" all
export PBS_EARTH_BIN="$ROOT/build/endpoint-c/pbs_earth"
export PBS_EARTH_TOOL="$ROOT/build/endpoint-c/pbs_tool"

# --- 5. self-checks ---------------------------------------------------------------
checks_failed=0
if [[ "${PBS_DEMO_SKIP_CHECKS:-0}" != 1 ]]; then
  say "step 5: self-checks"
  mkdir -p "$RESULTS/selfcheck"
  : >"$RESULTS/selfcheck/checks.tsv"
  run_check() {
    local name="$1"; shift
    local t0 t1 rc
    t0=$(date +%s%N)
    if "$@" >"$RESULTS/selfcheck/$name.log" 2>&1; then rc=0; else rc=$?; fi
    t1=$(date +%s%N)
    printf '%s\t%d\t%s\t%s\n' "$name" "$rc" "$(( (t1 - t0) / 1000000 ))" "$*" \
      >>"$RESULTS/selfcheck/checks.tsv"
    if [[ $rc -eq 0 ]]; then say "  $name: pass"; else say "  $name: FAIL (exit $rc, see $RESULTS/selfcheck/$name.log)"; checks_failed=1; fi
  }
  run_check emulator-tests "$PY" emulator/test_linkemu.py
  run_check scenario-tests "$PY" scenarios/test_scenarios.py
  run_check endpoint-b-tests make -s -C endpoint-c BUILD="$ROOT/build/endpoint-c" test
  run_check endpoint-a-tests endpoint-python/run_tests.sh
  "$PY" - "$RESULTS/selfcheck" <<'PYEOF'
import json, os, sys
d = sys.argv[1]
out = []
for line in open(os.path.join(d, "checks.tsv")):
    name, rc, ms, cmd = line.rstrip("\n").split("\t", 3)
    log = [l for l in open(os.path.join(d, name + ".log"), errors="replace").read().splitlines() if l.strip()]
    out.append({"name": name, "exit": int(rc), "duration_s": int(ms) / 1000.0, "command": cmd,
                "last_line": log[-1] if log else ""})
json.dump(out, open(os.path.join(d, "checks.json"), "w"), indent=1)
PYEOF
fi

# --- 6. scenarios -----------------------------------------------------------------
say "step 6: scenarios"
scen_failed=0
for s in "${SCENARIOS[@]}"; do
  say "scenario $s"
  rc=0
  "$PY" scenarios/runner.py "scenarios/$s.json" --out "$RESULTS/$s" || rc=$?
  "$PY" scenarios/evaluate.py "$RESULTS/$s" || rc=1
  [[ $rc -eq 0 ]] || scen_failed=1
done

# --- 7. report --------------------------------------------------------------------
say "step 7: report"
"$PY" report/make_report.py --results "$RESULTS" --out "$ROOT/REPORT.md"
"$PY" report/make_report.py --results "$RESULTS" --out "$RESULTS/REPORT.md" >/dev/null

say "finished in $(( $(date +%s) - t_start )) s: self-checks $([[ $checks_failed == 0 ]] && echo passed || echo FAILED), scenarios $([[ $scen_failed == 0 ]] && echo passed || echo FAILED)"
[[ $checks_failed == 0 && $scen_failed == 0 ]]
