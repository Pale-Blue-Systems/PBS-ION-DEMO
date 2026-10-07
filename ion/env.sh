# Source this file to operate the three demo ION nodes from a shell.
#
#   source ion/env.sh
#   ion_node earth bpsink ipn:23.1     # run any ION tool attached to a node
#
# ION_NODE_LIST_DIR must be the same string for every process of every node
# on the host (ION multi-node rule), so it is derived once here.

_ion_env_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

export ION_PREFIX="${ION_PREFIX:-$_ion_env_root/build/ion}"
export ION_RUN_DIR="${ION_RUN_DIR:-$_ion_env_root/build/run/ion}"
export ION_NODE_LIST_DIR="$ION_RUN_DIR"
case ":$PATH:" in
  *":$ION_PREFIX/bin:"*) ;;
  *) export PATH="$ION_PREFIX/bin:$PATH" ;;
esac

# Node table: name -> node number (also the LTP engine number).
ION_NODES="rover gateway earth"
ion_node_nbr() {
  case "$1" in
    rover) echo 99 ;; gateway) echo 50 ;; earth) echo 23 ;;
    *) echo "unknown node: $1" >&2; return 1 ;;
  esac
}

# Run a command attached to the named node. ION_NODE_WDNAME tells ionAttach()
# which node to join, so the command can run from any directory.
ion_node() {
  local name="$1"; shift
  ion_node_nbr "$name" >/dev/null || return 1
  ION_NODE_WDNAME="$ION_RUN_DIR/$name" "$@"
}

unset _ion_env_root
