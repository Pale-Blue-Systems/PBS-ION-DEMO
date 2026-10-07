# Helpers shared by ion/start_nodes.sh, ion/stop_nodes.sh and ion/selftest.sh.
# Requires ion/env.sh to be sourced first.

_ion_lib_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# SVR4 shared-memory keys used by the demo nodes, as 0x%08x (ipcs format):
# the host-global semaphore table 0xee08 plus wmKey, sdrWmKey and heapKey of
# every node (read from ion/nodes/*/node.ionconfig).
ion_shm_keys() {
  printf '0x%08x\n' $((0xee08))
  awk '$1 == "wmKey" || $1 == "sdrWmKey" || $1 == "heapKey" { printf "0x%08x\n", $2 }' \
    "$_ion_lib_dir"/nodes/*/node.ionconfig
}

# PIDs of running ION programs (names from ION's own ionprocesses.txt).
ion_pids() {
  local list="$ION_PREFIX/bin/ionprocesses.txt"
  [[ -f "$list" ]] || return 0
  ps -eo pid=,comm= | awk -v list="$list" '
    BEGIN { while ((getline n < list) > 0) if (n !~ /^#/ && n != "") names[substr(n, 1, 15)] = 1 }
    ($2 in names) { print $1 }'
}

# ION shared-memory segments still present (key and shmid).
ion_shm_present() {
  local keys
  keys="$(ion_shm_keys | tr '\n' ' ')"
  ipcs -m 2>/dev/null | awk -v keys="$keys" '
    BEGIN { n = split(keys, k, " "); for (i = 1; i <= n; i++) want[k[i]] = 1 }
    ($1 in want) { print $1, $2 }'
}

# POSIX named semaphores created by ION (Linux keeps them in /dev/shm).
ion_sem_files() {
  compgen -G '/dev/shm/sem.ion:GLOBAL:*' || true
}

ion_state_clean() {
  [[ -z "$(ion_pids)" && -z "$(ion_shm_present)" && -z "$(ion_sem_files)" ]]
}

ion_state_report() {
  local p s f
  p="$(ion_pids)"; s="$(ion_shm_present)"; f="$(ion_sem_files)"
  echo "ION processes: $( [[ -n "$p" ]] && ps -o pid=,comm= -p $(echo $p | tr ' ' ',') | tr '\n' ';' || echo none)"
  echo "ION shared memory (key shmid): $( [[ -n "$s" ]] && echo "$s" | tr '\n' ';' || echo none)"
  echo "ION POSIX semaphores: $( [[ -n "$f" ]] && echo "$f" | wc -l || echo 0)"
}

# Last-resort cleanup limited to ION: SIGTERM then SIGKILL ION programs,
# remove only the shared-memory keys listed by ion_shm_keys, and remove
# ION's POSIX semaphores. (ION's "killm f" instead removes every SVR4 IPC
# object owned by the user, ION or not.)
ion_force_clean() {
  local p
  p="$(ion_pids)"
  if [[ -n "$p" ]]; then
    kill -TERM $p 2>/dev/null || true
    for _ in 1 2 3 4 5 6 7 8 9 10; do [[ -z "$(ion_pids)" ]] && break; sleep 0.5; done
    p="$(ion_pids)"; [[ -n "$p" ]] && kill -KILL $p 2>/dev/null || true
    sleep 0.5
  fi
  ion_shm_present | while read -r _key id; do ipcrm -m "$id" 2>/dev/null || true; done
  ion_sem_files | while read -r f; do rm -f "$f"; done
}

# Wait until ion.log in a node directory shows that all BP and LTP daemons
# are running. Returns 1 on timeout.
ion_wait_ready() {
  local dir="$1" timeout="${2:-30}" waited=0
  local need=("ltpcli is running" "ipnfw is running" "ipnadminep is running" "bptransit is running" "bpclock is running")
  while (( waited < timeout * 10 )); do
    local ok=1
    for m in "${need[@]}"; do grep -q "$m" "$dir/ion.log" 2>/dev/null || { ok=0; break; }; done
    (( ok )) && return 0
    sleep 0.1; waited=$((waited + 1))
  done
  return 1
}
