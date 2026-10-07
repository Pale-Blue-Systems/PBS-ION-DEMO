#!/usr/bin/env bash
# Fetch PBS_LINK 0.1.4 at a pinned commit into ./build/pbs-link.
#
# Source:  https://github.com/Pale-Blue-Systems/PBS_LINK
# Commit:  d3b81d32f91c10d30b5a834b7fb64a353285fd27 ("Release PBS_LINK 0.1.4",
#          head of branch v1.5.0-update and of pull request #5)
# Package: PBS_LINK/ tree fa7b29276da8cb406b3aff6f93f038c4e8386798
#
# docs/test-vectors.json was generated with commit 54ae20e of the same
# branch; commits after d3b81d3 change documentation only, and the
# PBS_LINK/ package tree is identical (same git tree hash).
#
# The script verifies the SHA-256 of PBS_LINK/__init__.py and
# PBS_LINK/core.py, so a checkout supplied with PBS_LINK_DIR is checked
# the same way as a fresh clone.
#
# Environment:
#   PBS_LINK_DIR  use this existing checkout instead of cloning (verified only)
#
# Prints the directory to put on PYTHONPATH.
set -euo pipefail

REPO_URL="https://github.com/Pale-Blue-Systems/PBS_LINK.git"
COMMIT="d3b81d32f91c10d30b5a834b7fb64a353285fd27"
SHA_INIT="5cd058eb3a33ff13293027242460bd5d95dcecdb46c87e3ee889d0b446c3cfef"
SHA_CORE="8862a4685cd8cac2b2ba100c197e042a61aa5e04c430405894094891df038185"

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DEST="${PBS_LINK_DIR:-$ROOT/build/pbs-link}"

verify() {
  local dir="$1" got
  got="$(sha256sum "$dir/PBS_LINK/__init__.py" | cut -d' ' -f1)"
  [[ "$got" == "$SHA_INIT" ]] || { echo "fetch_pbs_link: $dir/PBS_LINK/__init__.py sha256 $got, expected $SHA_INIT" >&2; return 1; }
  got="$(sha256sum "$dir/PBS_LINK/core.py" | cut -d' ' -f1)"
  [[ "$got" == "$SHA_CORE" ]] || { echo "fetch_pbs_link: $dir/PBS_LINK/core.py sha256 $got, expected $SHA_CORE" >&2; return 1; }
}

if [[ -n "${PBS_LINK_DIR:-}" ]]; then
  [[ -f "$DEST/PBS_LINK/core.py" ]] || { echo "fetch_pbs_link: PBS_LINK_DIR=$DEST has no PBS_LINK/core.py" >&2; exit 1; }
  verify "$DEST"
  echo "$DEST"
  exit 0
fi

if [[ -f "$DEST/PBS_LINK/core.py" ]] && verify "$DEST" 2>/dev/null; then
  echo "$DEST"
  exit 0
fi

command -v git >/dev/null || { echo "fetch_pbs_link: git is required" >&2; exit 1; }
rm -rf "$DEST"
mkdir -p "$DEST"
git -C "$DEST" init -q
git -C "$DEST" remote add origin "$REPO_URL"
git -C "$DEST" fetch -q --depth 1 origin "$COMMIT"
git -C "$DEST" -c advice.detachedHead=false checkout -q FETCH_HEAD
HEAD_COMMIT="$(git -C "$DEST" rev-parse HEAD)"
if [[ "$HEAD_COMMIT" != "$COMMIT" ]]; then
  echo "fetch_pbs_link: fetched $HEAD_COMMIT, expected $COMMIT" >&2
  exit 1
fi
verify "$DEST"
echo "$DEST"
