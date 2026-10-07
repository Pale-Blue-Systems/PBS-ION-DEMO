#!/usr/bin/env bash
# Stop the three demo ION nodes and verify that no ION process, SVR4 shared
# memory segment or POSIX named semaphore is left behind.
#
# Order (ION Shutdown Guide, "Layout B": every node has its own sdrWmKey):
#   rover, gateway:  ionexit n   (stops the node, destroys its SDR and
#                                 working memory, keeps host-global IPC)
#   earth (last):    ionexit     (also releases the host-global semaphore
#                                 table 0xee08 and the POSIX semaphores)
# If anything is left after that, the script falls back to ion_force_clean
# (ion/lib.sh): SIGTERM/SIGKILL of ION programs and removal of only the
# demo's shared-memory keys and ION's POSIX semaphores. It does not use
# "killm f", because killm f removes every SVR4 IPC object owned by the
# user, not only ION's. Any fallback is reported.
#
# Applications attached to a node (bpsink, endpoint programs, ...) should be
# stopped first; ionexit ends their semaphores, so they normally exit too.
#
# Exit status: 0 clean; 1 state left behind even after the fallback.
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=env.sh
source "$HERE/env.sh"
# shellcheck source=lib.sh
source "$HERE/lib.sh"

stop_order="rover gateway earth"
last="earth"

if [[ -s "$ION_NODE_LIST_DIR/ion_nodes" ]]; then
  for name in $stop_order; do
    dir="$ION_RUN_DIR/$name"
    grep -q " $dir " "$ION_NODE_LIST_DIR/ion_nodes" 2>/dev/null || grep -q " $dir\$" "$ION_NODE_LIST_DIR/ion_nodes" 2>/dev/null || continue
    if [[ "$name" == "$last" ]]; then args=(); else args=(n); fi
    echo "stop_nodes: $name: ionexit ${args[*]:-}"
    ( cd "$dir" && ION_NODE_WDNAME="$dir" timeout 60 ionexit "${args[@]}" ) \
      >>"$dir/stop.log" 2>&1 </dev/null || echo "stop_nodes: $name: ionexit exit status $?"
  done
fi

# Allow up to 10 s for any remaining daemon to exit after ionexit.
for _ in $(seq 1 20); do
  ion_state_clean && break
  sleep 0.5
done

if ion_state_clean; then
  rm -f "$ION_NODE_LIST_DIR/ion_nodes"
  echo "stop_nodes: clean (no ION processes, no ION shared memory, no ION semaphores)"
  exit 0
fi

echo "stop_nodes: ION state still present:"
ion_state_report | sed 's/^/  /'
echo "stop_nodes: falling back to ion_force_clean"
ion_force_clean
rm -f "$ION_NODE_LIST_DIR/ion_nodes"
if ion_state_clean; then
  echo "stop_nodes: clean after ion_force_clean"
  exit 0
fi
echo "stop_nodes: NOT clean after ion_force_clean:"
ion_state_report | sed 's/^/  /'
exit 1
