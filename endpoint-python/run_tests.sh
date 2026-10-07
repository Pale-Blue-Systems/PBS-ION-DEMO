#!/usr/bin/env bash
# Run Endpoint A's tests.
#
#   endpoint-python/run_tests.sh [pytest arguments]
#   PBS_ION_LIVE=1 endpoint-python/run_tests.sh      # also the live ION test (~50 s)
#
# Fetches PBS_LINK 0.1.4 (fetch_pbs_link.sh). If python3 cannot import
# cryptography and pytest, creates build/venv-endpoint-python (with the
# system site packages visible) and installs requirements.txt into it.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"

"$HERE/fetch_pbs_link.sh" >/dev/null

PY="${PYTHON:-python3}"
if ! "$PY" -c "import cryptography, pytest" 2>/dev/null; then
  VENV="$ROOT/build/venv-endpoint-python"
  if [[ ! -x "$VENV/bin/python" ]]; then
    "$PY" -m venv --system-site-packages "$VENV"
  fi
  "$VENV/bin/python" -m pip install -q -r "$HERE/requirements.txt"
  PY="$VENV/bin/python"
fi

cd "$HERE"
exec "$PY" -m pytest -q tests "$@"
