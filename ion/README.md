# ION nodes

This directory builds NASA/JPL ION and runs three ION nodes on one host:

| Node | Role | ION node / LTP engine | Endpoints |
|---|---|---|---|
| ROVER | Lunar surface rover; runs its own ION node | 99 | `ipn:99.0` (admin), `ipn:99.1` (rover), `ipn:99.2` |
| LUNAR GATEWAY | Store-and-forward relay | 50 | `ipn:50.0` (admin), `ipn:50.1`, `ipn:50.2` |
| EARTH | Mission Control | 23 | `ipn:23.0` (admin), `ipn:23.1` (Mission Control), `ipn:23.2` |

Bundle Protocol version 7, LTP over UDP between neighbouring nodes. ROVER
and EARTH have no direct link; bundles between them go through the gateway,
routed by contact graph routing (CGR) over the contact plan.

## Files

| File | Purpose |
|---|---|
| `build_ion.sh` | Fetches ION at the pinned release and builds it into `build/ion`. Idempotent. |
| `env.sh` | `source` it to get `PATH`, `ION_NODE_LIST_DIR` and the `ion_node <name> <command>` helper. |
| `lib.sh` | Shell helpers: ION process / shared-memory / semaphore checks, readiness wait, last-resort cleanup. |
| `nodes/<node>/node.*` | Per-node config: `node.ionconfig`, `node.ionrc`, `node.ionsecrc`, `node.ltprc`, `node.bprc`, `node.ipnrc`. |
| `contacts/always_on.ionrc` | Contact plan with every link up for 24 h (used by `selftest.sh`). |
| `topology.json` | Nodes, legs (one-way delay, contact-plan range and rate) and the four emulator link ports. Read by `scenarios/scenario.py`, which writes each scenario's contact plan and emulator configuration from it; `scenarios/test_scenarios.py` checks it against `nodes/*/node.ltprc`. |
| `start_nodes.sh [PLAN]` | Creates fresh working directories and starts the three nodes. |
| `stop_nodes.sh` | Stops the three nodes and verifies nothing is left behind. |
| `udp_forward.py` | Fixed-delay UDP forwarder. Stands in for the link emulator in `selftest.sh`. |
| `selftest.sh [OUT]` | Starts forwarders and nodes, sends bundles, measures, stops, checks cleanup. |

## ION version and build

- Repository: <https://github.com/nasa-jpl/ION-DTN>
- Tag: `ion-open-source-4.2.0` (latest stable tag on 2026-10-07; the newer
  `ion-open-source-4.2.1-a.1` is an alpha)
- Commit: `568df887cb9f18aa8ec013b1a566d83215e499ae`
- Version string reported by `ionadmin`: `ION-OPEN-SOURCE-4.2.0`
- Crypto suite reported by `bpadmin`: `NULL Suites` (ION built without
  MbedTLS; BPSec is not used by this demo)

`build_ion.sh` runs the autotools build documented in ION's Quick Start
Guide, with an install prefix inside this repository:

```sh
git clone --depth 1 --branch ion-open-source-4.2.0 https://github.com/nasa-jpl/ION-DTN.git build/src/ION-DTN
# verifies HEAD == 568df887cb9f18aa8ec013b1a566d83215e499ae
cd build/src/ION-DTN
git clean -fdxq                # fresh tree for every build
autoreconf -fi
./configure --prefix="$PWD/../../ion" --disable-manpages
make -j"$(getconf _NPROCESSORS_ONLN)"
make install
```

Nothing is installed outside `build/`. The binaries carry a RUNPATH to
`build/ion/lib`, so `LD_LIBRARY_PATH` is not needed. The full build log is
`build/ion-build.log`. `build/ion/.ion-commit` records the installed commit;
the script skips the build when it matches. `ION_FORCE=1` forces a rebuild.

Build prerequisites: `git`, `autoconf`, `automake`, `libtool`, `gcc`, `make`.
`udp_forward.py` and `selftest.sh` also need `python3`.

Measured build (one run, 2026-10-07): 91 s wall clock (`real 1m30.8s`,
`user 3m15.3s`), `make -j4`, 4 vCPU Intel Xeon 2.80 GHz, Ubuntu 24.04.5,
gcc 13.3.0, autoconf 2.71, automake 1.16.5, libtool 2.4.7. The install
prefix is 34 MB with 162 programs in `build/ion/bin`. The log contains 24
compiler warnings and no errors.

Clean-checkout check (same host, same day): in a fresh `git clone` of this
repository, `ion/selftest.sh` cloned ION, built it (88 s), ran the
self-test (pass; `bping` RTT 3142.2 / 3152.3 / 3176.2 ms min / avg / max)
and stopped cleanly, in 2 min 38 s in total.

## Ports and links

Every inter-node link direction has its own UDP port on 127.0.0.1, owned
by the link emulator (or `udp_forward.py`). Each node's LTP link service
output (`udplso`) sends to the emulator port for that direction; the
emulator forwards to the peer's LTP link service input (`udplsi`).

| Node | `udplsi` listens on |
|---|---|
| ROVER (99) | 127.0.0.1:19913 |
| LUNAR GATEWAY (50) | 127.0.0.1:15013 |
| EARTH (23) | 127.0.0.1:12313 |

| Direction | `udplso` sends to (emulator port) | Emulator forwards to |
|---|---|---|
| rover -> gateway | 127.0.0.1:29950 | 127.0.0.1:15013 |
| gateway -> rover | 127.0.0.1:25099 | 127.0.0.1:19913 |
| gateway -> earth | 127.0.0.1:25023 | 127.0.0.1:12313 |
| earth -> gateway | 127.0.0.1:22350 | 127.0.0.1:15013 |

Emulator ports follow `2FFTT` (FF = sending node, TT = receiving node).
`udplsi` ports follow `1NN13`. All addresses are explicit IPv4 literals, not
host names (ION's Quick Start Guide warns that a host name resolving to IPv6
first breaks LTP over UDP on hosts not configured for it).

LTP spans (`node.ltprc`): `a span <peer> 32 32 1024 1 1 '<udplso>' 1`
- 32 export and 32 import sessions per span, 1024-byte maximum segment;
- aggregation size threshold 1 byte, so every bundle is sent as its own LTP
  block as soon as `ltpclo` dequeues it (see gotcha 9);
- queuing latency 1 s (ION default);
- `m maxbacklog` 32 at the rover and Earth, 64 at the Gateway: the blocks
  the node's peers can have open towards it (32 import sessions per span).
  With ION's default of 10, Earth discarded checkpoints after a scheduled
  outage (gotcha 15).

BP (`node.bprc`): scheme `ipn`, endpoints `ipn:N.0`, `ipn:N.1`, `ipn:N.2`
with rule `q` (bundles for an endpoint that no application has open are
queued, not discarded), protocol `ltp`, one induct (`ltpcli`), one outduct
(`ltpclo`) per neighbour. `node.ipnrc` declares one egress plan per
neighbour (`a plan 50 ltp/50` and so on).

## Shared memory and working directories

Several ION nodes on one host need (ION Deployment Guide, "Multi-node
Operation"):

- `ION_NODE_LIST_DIR` set to the same string in every ION process of every
  node. `env.sh` sets it to `build/run/ion`; ION writes the node registry
  `build/run/ion/ion_nodes` there.
- one working directory per node: `build/run/ion/rover`,
  `build/run/ion/gateway`, `build/run/ion/earth`. A process attaches to the
  node whose directory is its current directory, or `ION_NODE_WDNAME` if
  set. `ion_node <name> <command>` sets `ION_NODE_WDNAME`.
- distinct SVR4 shared-memory keys and SDR names per node (`node.ionconfig`):

| Node | `sdrName` | `wmKey` | `sdrWmKey` | `heapKey` |
|---|---|---|---|---|
| ROVER | `ion99` | 0x20063 | 0xff63 | 0x30063 |
| LUNAR GATEWAY | `ion50` | 0x20032 | 0xff32 | 0x30032 |
| EARTH | `ion23` | 0x20017 | 0xff17 | 0x30017 |

Shared by all nodes: the semaphore table, key 0xee08, and ION's POSIX named
semaphores `/dev/shm/sem.ion:GLOBAL:*` (179 files with the three nodes
running). Each node: `wmSize` 20,000,000 bytes, `heapWords` 4,000,000
(32 MB SDR heap in shared memory), `sdrWmSize` 1,000,000, `configFlags 13`
(SDR in DRAM, reversible transactions logged to a file in the node
directory, bounded).

## Contact plan

`start_nodes.sh` writes `contacts.ionrc` into each node directory: one line
`@ <UTC time>` followed by the chosen plan file. The `@` line sets the
reference time for the `+N` relative times, so all three nodes load the
same absolute contact windows. Without it, `+N` is relative to the moment
each `ionadmin` starts, which differs per node. The reference time used is
saved in `build/run/ion/t0`; `ION_T0=yyyy/mm/dd-hh:mm:ss` (UTC) overrides it.

`contacts/always_on.ionrc`:

```
a contact +0 +86400 99 50 125000
a contact +0 +86400 50 99 125000
a contact +0 +86400 50 23 125000
a contact +0 +86400 23 50 125000
a range +0 +86400 50 99 1
a range +0 +86400 23 50 2
```

Rates are bytes per second. Ranges are one-way light time in whole seconds
(ION accepts integers only). The 1.3 s Earth-Moon light time is entered as
2 s and the rover-gateway leg (0.25 s in `selftest.sh`) as 1 s: rounded up,
so the LTP retransmission timers, which ION computes from the range, do not
expire before the emulated round trip completes. A range whose first node
number is lower than the second also sets the reverse direction.

What the rover node loaded (`ionadmin`, `l contact` / `l range`, from a
`selftest.sh` run):

```
From  2026/10/07-00:58:14 to  2026/10/08-00:58:14 the xmit rate from node 23 to node 50 is     125000 bytes/sec, confidence 1.000000.
From  2026/10/07-00:58:14 to  2026/10/08-00:58:14 the xmit rate from node 50 to node 23 is     125000 bytes/sec, confidence 1.000000.
From  2026/10/07-00:58:14 to  2026/10/08-00:58:14 the xmit rate from node 50 to node 99 is     125000 bytes/sec, confidence 1.000000.
From  2026/10/07-00:58:14 to  2026/10/08-00:58:14 the xmit rate from node 99 to node 50 is     125000 bytes/sec, confidence 1.000000.
From  2038/01/19-03:14:07 to  2038/01/19-03:14:07 the xmit rate from node 99 to node 99 is          0 bytes/sec, confidence 1.000000.
From  2026/10/07-00:58:14 to  2026/10/08-00:58:14 the OWLT from node 23 to node 50 is          2 seconds.
From  2026/10/07-00:58:14 to  2026/10/08-00:58:14 the OWLT from node 50 to node 23 is          2 seconds.
From  2026/10/07-00:58:14 to  2026/10/08-00:58:14 the OWLT from node 50 to node 99 is          1 seconds.
From  2026/10/07-00:58:14 to  2026/10/08-00:58:14 the OWLT from node 99 to node 50 is          1 seconds.
```

The last contact line is the registration contact ION adds for the local
node.

## Start and stop

```sh
ion/build_ion.sh                       # once
python3 ion/udp_forward.py \
  --link rover-gateway:29950:15013:0.25 --link gateway-rover:25099:19913:0.25 \
  --link gateway-earth:25023:12313:1.3  --link earth-gateway:22350:15013:1.3 &
ion/start_nodes.sh                     # or: ion/start_nodes.sh <plan.ionrc>
source ion/env.sh
ion_node earth bpsink ipn:23.1 &       # prints: 'hello from the rover'
ion_node rover bpsource ipn:23.1 "hello from the rover"
kill %1 %2                             # forwarder, bpsink
ion/stop_nodes.sh
```

`start_nodes.sh` refuses to run if any ION process, any of the shared-memory
keys above or any ION POSIX semaphore already exists. It starts the gateway,
then the rover, then Earth. For each node it runs, in the node directory,
`ionadmin node.ionrc`, `ionadmin contacts.ionrc`, `ionsecadmin
node.ionsecrc`, `ltpadmin node.ltprc`, `bpadmin node.bprc`, then waits until
`ion.log` reports `ltpcli`, `ipnfw`, `ipnadminep`, `bptransit` and `bpclock`
running. The nodes start whether or not the emulator is up.

`stop_nodes.sh` follows the ION Shutdown Guide for nodes with per-node
`sdrWmKey` ("Layout B"): `ionexit n` for ROVER and LUNAR GATEWAY (stops the
node and destroys its SDR and working memory, keeps the host-global
semaphore table), then bare `ionexit` for EARTH, the last node (also
removes 0xee08 and the POSIX semaphores). It then checks that no ION
process (names from ION's `ionprocesses.txt`), none of the ten
shared-memory keys and no `/dev/shm/sem.ion:GLOBAL:*` file remains. If
something remains, it falls back to `ion_force_clean` (`lib.sh`): SIGTERM
then SIGKILL of ION processes, `ipcrm` of only the ten keys, removal of the
semaphore files. The fallback is reported in the output.

Applications attached to a node (`bpsink`, the demo endpoints) should be
stopped before `stop_nodes.sh`.

## Self-test

`ion/selftest.sh [OUT_DIR]` (default `build/run/selftest`):

1. runs `build_ion.sh` (no-op if built) and stops any stale ION state;
2. starts `udp_forward.py` with one-way delays rover<->gateway 0.25 s and
   gateway<->earth 1.3 s, no loss, no rate limit;
3. starts the three nodes with `contacts/always_on.ionrc`;
4. sends three files (100, 3000 and 60000 random bytes) with
   `bpsendfile ipn:99.1 ipn:23.1` on the rover and receives each with
   `bprecvfile ipn:23.1 1` on Earth; compares the bytes;
5. runs `bpecho ipn:99.2` on the rover and `bping -c 5 -i 1 ipn:23.2
   ipn:99.2` on Earth;
6. runs `bpstats` on the gateway and reads its forwarded and delivered
   counts from `ion.log`;
7. stops the forwarder and the nodes and checks that nothing is left.

It passes only if all files arrive byte-identical, 5 of 5 echoes return,
the gateway forwarded at least 13 bundles (3 files + 5 pings + 5 echoes)
and delivered none locally, and the stop is clean without the fallback.
Results are written to `OUT_DIR/selftest.json`, with per-datagram forwarder
records (`forwarder.jsonl`), forwarder counters, each node's `ion.log`, and
the contact plan as loaded by the rover.

Results of five runs on 2026-10-07 (host as in "ION version and build";
delays as above; contact rate 125,000 B/s each direction). Run 5 used the
committed files. Runs 1-4 used versions that differed only in comment lines
of the node config files, output-path handling in `selftest.sh` and one
message text in `stop_nodes.sh`.

| Measurement | Run 1 | Run 2 | Run 3 | Run 4 | Run 5 |
|---|---|---|---|---|---|
| File 100 B, `bpsendfile` start to `bprecvfile` exit | 1.578 s | 1.580 s | 1.579 s | 1.580 s | 1.582 s |
| File 3000 B | 1.588 s | 1.582 s | 1.584 s | 1.587 s | 1.587 s |
| File 60000 B | 1.655 s | 1.635 s | 1.623 s | 1.641 s | 1.624 s |
| Files byte-identical | 3/3 | 3/3 | 3/3 | 3/3 | 3/3 |
| `bping` echoes received | 5/5 | 5/5 | 5/5 | 5/5 | 5/5 |
| `bping` RTT min / avg / max | 3139.9 / 3149.3 / 3156.7 ms | 3144.8 / 3147.6 / 3150.3 ms | 3141.5 / 3151.0 / 3159.2 ms | 3146.8 / 3152.5 / 3161.1 ms | 3146.1 / 3149.5 / 3155.2 ms |
| Bundles forwarded by gateway / delivered at gateway | 13 / 0 | 13 / 0 | 13 / 0 | 13 / 0 | 13 / 0 |
| `start_nodes.sh` duration | 3.74 s | 3.77 s | 3.76 s | 3.79 s | 3.76 s |
| `stop_nodes.sh` duration | 27.5 s | 27.2 s | 27.2 s | 26.8 s | 27.1 s |
| Stop clean, no fallback | yes | yes | yes | yes | yes |

The emulated propagation alone is 1.55 s one way (0.25 + 1.3) and 3.10 s
for the round trip. In runs 1, 4 and 5 the forwarder counted 83 datagrams
(64,904-64,905 bytes) rover->gateway and 83 (64,921 bytes) gateway->earth,
and 18 each way in the reverse directions, earth->gateway and
gateway->rover (the 5 ping bundles plus LTP report and
report-acknowledgement segments). The forwarder records show the first 195-byte data segment from the rover being passed
to the gateway 0.250 s after it left the rover, and the gateway's 197-byte
segment for Earth reaching the forwarder 0.259 s (run 1) and 0.261 s
(runs 4 and 5) after that departure: 9-11 ms in the gateway (LTP
reception, BP forwarding, LTP transmission).

Stop takes about 9 s per node: `ionexit` includes a 3 s grace period and
waits about 2 s for `rfxclock` to end (both visible in `stop.log` and
`ion.log`).

## Gotchas found

1. **`ION_NODE_LIST_DIR` must be identical everywhere.** Every ION process
   of every node, including tools such as `bpsink`, must see the same
   string. A process outside a node directory must also set
   `ION_NODE_WDNAME`, or it attaches to whichever node is registered for
   its current directory (none, if it is elsewhere). The registry
   `ion_nodes` has five columns in 4.2.0: node number, `wmKey`, `sdrName`,
   working directory, `sdrWmKey`.
2. **Default SDR working memory is shared.** Without `sdrWmKey`, every node
   uses key 0xff00 ("Layout A"), and `ionexit n` on one node destroys
   memory the others still use (ION Shutdown Guide). Each node here sets
   its own `sdrWmKey`, so nodes can be stopped one at a time.
3. **The SDR heap key is chosen at run time** unless `heapKey` is set. In
   the first runs, ION allocated 0x1002, 0x1007 and 0x100c. Setting
   `heapKey` makes every key known, so leftover state can be checked and
   removed exactly.
4. **Admin programs pass their stdout/stderr to the daemons they start.**
   `bpadmin node.bprc | tail` never finishes, because `tail` waits for
   end-of-file and the daemons keep the pipe open. Redirect to a file.
5. **`ionexit` has no help option.** `ionexit -h` ran a shutdown; with no
   node running it printed attach errors and left shared-memory segment
   0xee08 and two POSIX semaphores (`sem.ion:GLOBAL:0`,
   `sem.ion:GLOBAL:ipcSem`) behind, which had to be removed by hand.
6. **`killm f` is host-wide.** It kills every ION process and runs `ipcrm`
   on every SVR4 message queue, shared-memory segment and semaphore set
   owned by the user, ION or not. With `ION_NODE_LIST_DIR` set, `ionstop`
   does not call `killm` at all and plain `killm` only stops the node of
   the current directory. `stop_nodes.sh` therefore uses `ionexit` and a
   cleanup limited to ION's own keys.
7. **ION on Linux uses POSIX named semaphores** (`/dev/shm/sem.ion:GLOBAL:*`)
   as well as SVR4 shared memory. Checking `ipcs` alone misses them.
8. **Ranges are whole seconds.** `a range` takes an unsigned integer number
   of light seconds; 1.3 s cannot be entered.
9. **LTP aggregation adds delay to small bundles.** A block is sent when
   its size reaches the span's aggregation size threshold or when it has
   waited the aggregation time limit; `ltpclock` checks the limit once per
   second. With threshold 1024 bytes and limit 1 s, 64-byte `bping`
   bundles had an RTT of 5576-5581 ms (5 pings, same delays as the
   self-test). With threshold 1 byte the RTT was 3140-3162 ms (runs 1-5).
10. **`udplso` paces to the contact-plan rate with a 131,070-byte burst.**
    Its token bucket uses the current contact's transmit rate and holds up
    to 2 x 65,535 bytes. In runs 1, 4 and 5 the 60 data segments of the
    60,000-byte bundle (60,670 bytes on the wire) left the rover within
    18-27 ms although the contact rate was 125,000 B/s (0.48 s for that
    volume). A link emulator rate limit below the contact rate will queue
    or drop such bursts.
11. **Absolute times in ionrc files are UTC.** `@ 2026/10/07-00:58:14`
    produced contacts listed from `2026/10/07-00:58:14`, matching `date -u`.
12. **Status reports are off by default in 4.2.0** (`m srmode`, release
    notes): tools that request reports (`bptrace`, `bping -r`) get none
    unless the node's `.bprc` sets `m srmode traditional` before `s`.
13. **`bping`'s default priority is 1 (standard)** in `bpv7/test/bping.c`;
    its man page says 0 (bulk). The gateway's `bpstats` counted the
    `bping`/`bpecho` bundles in priority class 1.
14. **LTP screening** (on by default, `ltprc(5)`): segments from a peer
    engine arriving while the contact plan gives that engine no transmit
    rate to the local node are discarded. Contact windows, ranges and
    clocks must agree. All three nodes share one host clock here.
15. **LTP discards red segments while the delivery backlog is full**
    (`m maxbacklog`, `ltprc(5)`, default 10; `ltp/library/libltpP.c`
    about line 6779; counted by `ltpstats` as `in_seg_too_fast`). The
    limit counts blocks waiting for delivery to BP on the whole engine.
    Checkpoints are red segments, so a discarded checkpoint costs the
    sender a checkpoint timeout (5.0 s on the Gateway-Earth leg in these
    runs). In a fresh-clone run on 2026-10-07 (04:37 UTC, commit
    `9dfb41f`, default limit), Earth screened the Gateway's burst of 24
    blocks after the S2 Gateway-Earth outage. The Gateway resent the 24
    checkpoints within 21 ms; Earth answered exactly 10 of them and
    discarded the others, 19 segments in all over two checkpoint rounds,
    and two BULK envelopes reached Earth only after the traffic phase (S2
    failed). With the limit raised to the peers' open blocks (the change
    committed as `de92b9c`), an S2 run with the same screening (33
    segments) discarded no segment, and Earth sent 24 reports between
    92.58 and 92.78 s in the corresponding checkpoint round. None of the five S2 runs with the
    raised limit discarded a segment for backlog; two S2 runs with the
    `m maxbacklog` lines removed (`results/control/`) discarded 1 and 5
    (`scenarios/README.md`, observation 6). Run A's files are not in the
    repository; the figures above were read from them.

## Open observation

Before the scripts were final, two manual runs with the same configuration
and delays (threshold 1 byte; in the second, the forwarder had been
restarted twice while the nodes ran) gave `bping` RTTs of 3379-4111 ms
(5 pings) and 3765-4853 ms (3 pings). Per-datagram forwarder timestamps
from the second showed 0.31 s at the gateway and 0.33 s at the rover
between a data segment's arrival and the node's next data transmission,
versus 9-11 ms at the gateway in scripted runs 1, 4 and 5. The cause was not
identified. Two later runs did not reproduce it: starting the forwarder 8 s
after the nodes (RTT 3143-3159 ms, 5 pings), and a 5 s forwarder outage
during a run (RTT 3142-3171 ms, 5 pings after it came back).
