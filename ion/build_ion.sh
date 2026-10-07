#!/usr/bin/env bash
# Build NASA/JPL ION-DTN at a pinned release into ./build/ion.
#
# Source:  https://github.com/nasa-jpl/ION-DTN
# Tag:     ion-open-source-4.2.0
# Commit:  568df887cb9f18aa8ec013b1a566d83215e499ae
#
# Build method: the documented autotools path from the ION Quick Start Guide
# (site-docs/docs/quick-start-guide.md): "autoreconf -fi", "./configure",
# "make", "make install". The only deviations are an out-of-tree install
# prefix (no system install, no sudo) and --disable-manpages.
#
# The script is idempotent: if build/ion/.ion-commit matches ION_COMMIT and
# the installed binaries are present, it exits without rebuilding.
#
# Environment overrides:
#   ION_JOBS   parallel make jobs (default: number of CPUs)
#   ION_FORCE  set to 1 to rebuild even if the stamp matches
set -euo pipefail

ION_REPO="https://github.com/nasa-jpl/ION-DTN.git"
ION_TAG="ion-open-source-4.2.0"
ION_COMMIT="568df887cb9f18aa8ec013b1a566d83215e499ae"

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SRC="$ROOT/build/src/ION-DTN"
PREFIX="$ROOT/build/ion"
STAMP="$PREFIX/.ion-commit"
LOG="$ROOT/build/ion-build.log"
JOBS="${ION_JOBS:-$(getconf _NPROCESSORS_ONLN 2>/dev/null || echo 2)}"

if [[ "${ION_FORCE:-0}" != 1 && -f "$STAMP" && "$(cat "$STAMP")" == "$ION_COMMIT" \
      && -x "$PREFIX/bin/ionadmin" && -x "$PREFIX/bin/ltpadmin" && -f "$PREFIX/lib/libbp.so" ]]; then
  echo "ION $ION_TAG ($ION_COMMIT) already installed in $PREFIX"
  exit 0
fi

for tool in git autoreconf make gcc; do
  command -v "$tool" >/dev/null || { echo "build_ion.sh: missing required tool: $tool" >&2; exit 1; }
done

mkdir -p "$ROOT/build/src"

# 1. Fetch the pinned release (shallow clone of the tag) and verify the commit.
if [[ ! -d "$SRC/.git" ]]; then
  git -c advice.detachedHead=false clone --depth 1 --branch "$ION_TAG" "$ION_REPO" "$SRC"
fi
HEAD_COMMIT="$(git -C "$SRC" rev-parse HEAD)"
if [[ "$HEAD_COMMIT" != "$ION_COMMIT" ]]; then
  echo "build_ion.sh: $SRC is at $HEAD_COMMIT, expected $ION_COMMIT ($ION_TAG)" >&2
  exit 1
fi

# 2. Documented autotools build, installed under $PREFIX.
start=$(date +%s)
{
  echo "== ION build: $ION_TAG $ION_COMMIT"
  echo "== prefix: $PREFIX  jobs: $JOBS  started: $(date -u +%FT%TZ)"
  cd "$SRC"
  git clean -fdxq            # fresh tree each build (the clone is disposable)
  autoreconf -fi
  ./configure --prefix="$PREFIX" --disable-manpages
  make -j"$JOBS"
  make install
} >"$LOG" 2>&1 || { echo "build_ion.sh: build failed, last lines of $LOG:" >&2; tail -40 "$LOG" >&2; exit 1; }
end=$(date +%s)

echo "$ION_COMMIT" >"$STAMP"
echo "ION $ION_TAG built and installed in $PREFIX in $((end - start)) s (log: $LOG)"
